# -*- coding: utf-8 -*-
"""
perf_stats.py —— 性能统计（吞吐 / 字节 / 延迟分位 / CPU）
=======================================================
沿用 datahub_test 的指标口径，但换成策略平台这套：
    * 每秒发送条数、每秒回包条数
    * 请求/回包字节数，按秒统计
    * 客户端 CPU 利用率
    * 平均响应时间（微秒）+ P50/P95/P99/Max

响应时间怎么来：发出去的每条报文都带 request_id，起一个读取线程
XREAD 回包流（DataHub_reply_stream 或 ST-<id>-reply），按 request_id
配回发送时刻，差值就是 RTT。所以本模块只负责记账，不碰 Redis。

用法：
    pc = PerfCollector(interval=1.0)
    pc.start()
    pc.record_send(1, nbytes)
    pc.record_reply(req_id, latency_ms, nbytes)
    pc.stop()
    pc.print_report()
    pc.write_json("out/performance/xxx_stats.json")
    pc.write_excel("out/performance/xxx.xlsx")
"""
import json
import os
import platform
import threading
import time

try:
    import psutil
    _HAS_PSUTIL = True
except Exception:
    _HAS_PSUTIL = False


def now():
    return time.time()


def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


class CpuSampler(object):
    """CPU 利用率采样器。

    优先用 psutil（拿整机+本进程），没有就退化成 os.times 的进程 CPU 时间，
    再不行就用 time.process_time()。压测时用它回答"客户端 CPU 打满了没"。
    """

    def __init__(self, pid=None):
        self.pid = pid or os.getpid()
        self.proc = None
        self._wall0 = None
        self._cpu0 = None
        self._sys_cpu0 = None
        if _HAS_PSUTIL:
            try:
                self.proc = psutil.Process(self.pid)
                self.proc.cpu_percent(None)      # 先调一次做基线
                psutil.cpu_percent(None)
            except Exception:
                self.proc = None
        self.reset()

    def reset(self):
        self._wall0 = now()
        self._cpu0 = self._proc_cpu()
        self._sys_cpu0 = self._sys_cpu()

    def _proc_cpu(self):
        if self.proc is not None:
            try:
                t = self.proc.cpu_times()
                return t.user + t.system
            except Exception:
                pass
        try:
            import resource
            r = resource.getrusage(resource.RUSAGE_SELF)
            return r.ru_utime + r.ru_stime
        except Exception:
            return time.process_time()

    def _sys_cpu(self):
        if _HAS_PSUTIL:
            try:
                return psutil.cpu_percent(None)
            except Exception:
                return None
        return None

    def sample(self):
        """返回 {'proc_percent':.., 'proc_cores':.., 'sys_percent':..}"""
        wall = now() - self._wall0
        cpu = self._proc_cpu() - self._cpu0
        out = {"proc_percent": 0.0, "proc_cores": 0.0, "sys_percent": None}
        if wall > 0:
            out["proc_cores"] = cpu / wall
            ncpu = os.cpu_count() or 1
            out["proc_percent"] = 100.0 * cpu / wall / ncpu
        if self.proc is not None:
            try:
                out["sys_percent"] = psutil.cpu_percent(None)
            except Exception:
                pass
        return out


class PerfCollector(object):
    def __init__(self, interval=1.0, label=""):
        self.interval = float(interval)
        self.label = label
        self.lock = threading.Lock()
        self.t0 = None
        self.t_end = None

        # 累计
        self.sent = 0
        self.sent_bytes = 0
        self.reply = 0
        self.reply_bytes = 0
        self.send_fail = 0
        self.timeout_reply = 0

        # 延迟（毫秒）
        self.latencies = []

        # 按秒序列
        self.series = []          # [{'sec':n,'sent':..,'sent_bytes':..,'reply':..,'reply_bytes':..}]
        self._cur = None

        # 待回包：request_id -> 发送时刻
        self.pending = {}
        self.pending_lock = threading.Lock()

        self.cpu = CpuSampler()
        self._thread = None
        self._stop = threading.Event()

        # 错误分类
        self.errors = {}

    # ------------------------------------------------------------ 记账
    def start(self):
        self.t0 = now()
        self.cpu.reset()
        self._stop.clear()
        self._thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self.t_end = now()
        self._flush_cur()
        return self

    def _cur_sec(self):
        return int(now() - self.t0)

    def _flush_cur(self):
        if self._cur is not None:
            self._merge(self._cur)
            self._cur = None

    def _tick_loop(self):
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            with self.lock:
                self._flush_cur()

    def _bucket(self):
        """取当前秒的桶。

        注意：tick 线程也会 _flush_cur()，所以同一个 sec 可能被 flush 两次，
        于是 series 里会出现两个相同 sec 的桶（实测踩过：0s/1s/1s）。
        这里 flush 时做一次合并，保证每秒只有一个桶。
        """
        sec = self._cur_sec()
        if self._cur is None or self._cur["sec"] != sec:
            self._flush_cur()
            self._cur = {"sec": sec, "sent": 0, "sent_bytes": 0,
                         "reply": 0, "reply_bytes": 0}
        return self._cur

    def _merge(self, b):
        """把桶并入 series，同 sec 就累加。"""
        for x in reversed(self.series):
            if x["sec"] == b["sec"]:
                for k in ("sent", "sent_bytes", "reply", "reply_bytes"):
                    x[k] += b[k]
                return
            if x["sec"] < b["sec"]:
                break
        self.series.append(b)
        self.series.sort(key=lambda x: x["sec"])

    def record_send(self, n=1, nbytes=0):
        with self.lock:
            self.sent += n
            self.sent_bytes += nbytes
            b = self._bucket()
            b["sent"] += n
            b["sent_bytes"] += nbytes

    def record_send_fail(self, err=None):
        with self.lock:
            self.send_fail += 1
            if err:
                k = str(err)[:120]
                self.errors[k] = self.errors.get(k, 0) + 1

    def track(self, request_id):
        """登记一条待回包的请求（记住发送时刻）。"""
        with self.pending_lock:
            self.pending[request_id] = now()

    def record_reply(self, request_id, nbytes=0):
        """收到回包：算 RTT。request_id 不在 pending 里说明是超时后才到的，忽略。"""
        with self.pending_lock:
            t_send = self.pending.pop(request_id, None)
        lat = (now() - t_send) * 1000.0 if t_send else None
        with self.lock:
            self.reply += 1
            self.reply_bytes += nbytes
            b = self._bucket()
            b["reply"] += 1
            b["reply_bytes"] += nbytes
            if lat is not None:
                self.latencies.append(lat)
                if len(self.latencies) > 2_000_000:
                    self.latencies = self.latencies[-1_000_000:]

    def expire_pending(self, older_than=0.0):
        """把迟迟没回的请求清掉，返回清掉的条数（计入 timeout_reply）。"""
        cut = now() - older_than
        n = 0
        with self.pending_lock:
            for k in [k for k, v in self.pending.items() if v < cut]:
                self.pending.pop(k, None)
                n += 1
        with self.lock:
            self.timeout_reply += n
        return n

    @property
    def outstanding(self):
        with self.pending_lock:
            return len(self.pending)

    def pending_ids(self):
        """当前还在等回包的 request_id 列表（用于"到底哪几条挂了"）。"""
        with self.pending_lock:
            return list(self.pending.keys())

    # ------------------------------------------------------------ 报告
    def snapshot(self):
        with self.lock:
            s = dict(
                label=self.label,
                start=self.t0,
                end=self.t_end or now(),
                duration=(self.t_end or now()) - (self.t0 or now()),
                sent=self.sent, sent_bytes=self.sent_bytes,
                reply=self.reply, reply_bytes=self.reply_bytes,
                send_fail=self.send_fail, timeout_reply=self.timeout_reply,
                outstanding=self.outstanding,
                series=list(self.series),
                errors=dict(self.errors),
            )
            lat = sorted(self.latencies)
        s["cpu"] = self.cpu.sample()
        s["host"] = platform.node()
        s["platform"] = platform.platform()
        d = s["duration"] or 1e-9
        s["send_per_sec"] = s["sent"] / d
        s["reply_per_sec"] = s["reply"] / d
        s["sent_bytes_per_sec"] = s["sent_bytes"] / d
        s["reply_bytes_per_sec"] = s["reply_bytes"] / d
        if s["sent"]:
            s["avg_sent_bytes"] = s["sent_bytes"] / float(s["sent"])
        if s["reply"]:
            s["avg_reply_bytes"] = s["reply_bytes"] / float(s["reply"])
        if lat:
            s["lat_count"] = len(lat)
            s["lat_avg_ms"] = sum(lat) / len(lat)
            s["lat_avg_us"] = s["lat_avg_ms"] * 1000.0
            s["lat_p50_ms"] = percentile(lat, 0.50)
            s["lat_p90_ms"] = percentile(lat, 0.90)
            s["lat_p95_ms"] = percentile(lat, 0.95)
            s["lat_p99_ms"] = percentile(lat, 0.99)
            s["lat_max_ms"] = lat[-1]
            s["lat_min_ms"] = lat[0]
        else:
            s["lat_count"] = 0
        return s

    def print_report(self, title="性能统计"):
        s = self.snapshot()
        W = 74
        print()
        print("=" * W)
        print("%s%s" % (title, ("  " + s["label"]) if s["label"] else ""))
        print("=" * W)
        print("  运行时长          : %.3f s" % s["duration"])
        print("  发送条数          : %d  (%.1f 条/秒, 失败 %d)"
              % (s["sent"], s["send_per_sec"], s["send_fail"]))
        print("  回包条数          : %d  (%.1f 条/秒, 超时未回 %d, 在途 %d)"
              % (s["reply"], s["reply_per_sec"], s["timeout_reply"], s["outstanding"]))
        print("  发送字节          : %d  (%.3f MB, %.1f MB/s, 均 %.0f B/条)"
              % (s["sent_bytes"], s["sent_bytes"] / 1048576.0,
                 s["sent_bytes_per_sec"] / 1048576.0, s.get("avg_sent_bytes", 0)))
        print("  回包字节          : %d  (%.3f MB, %.1f MB/s, 均 %.0f B/条)"
              % (s["reply_bytes"], s["reply_bytes"] / 1048576.0,
                 s["reply_bytes_per_sec"] / 1048576.0, s.get("avg_reply_bytes", 0)))
        if s["lat_count"]:
            print("  响应时间(ms)      : 均 %.3f  最小 %.3f  P50 %.3f  P90 %.3f  P95 %.3f  P99 %.3f  最大 %.3f"
                  % (s["lat_avg_ms"], s["lat_min_ms"], s["lat_p50_ms"],
                     s["lat_p90_ms"], s["lat_p95_ms"], s["lat_p99_ms"], s["lat_max_ms"]))
            print("  平均响应时间      : %.0f 微秒 (样本 %d)"
                  % (s["lat_avg_us"], s["lat_count"]))
        cpu = s["cpu"]
        print("  客户端 CPU        : 进程 %.2f%% (%.2f 核)%s"
              % (cpu["proc_percent"], cpu["proc_cores"],
                 ("  整机 %.1f%%" % cpu["sys_percent"]) if cpu.get("sys_percent") is not None else ""))
        if s["errors"]:
            print("  错误分布          :")
            for k, v in sorted(s["errors"].items(), key=lambda x: -x[1])[:10]:
                print("      x%-6d %s" % (v, k))
        # 每秒曲线（最多 20 行，太长就抽样）
        ser = [x for x in s["series"] if x["sent"] or x["reply"]]
        if ser:
            print("-" * W)
            print("  按秒统计 (秒: 发送 / 回包 / 发送字节 / 回包字节)")
            step = max(1, len(ser) // 20)
            for x in ser[::step]:
                print("    %4ds : %6d / %6d / %9d / %9d"
                      % (x["sec"], x["sent"], x["reply"],
                         x["sent_bytes"], x["reply_bytes"]))
        print("=" * W)
        return s

    # ------------------------------------------------------------ 落盘
    def write_json(self, path):
        s = self.snapshot()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        return path

    def write_excel(self, path):
        """写 Excel；没装 openpyxl 就退化成 CSV。"""
        s = self.snapshot()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, Alignment
        except Exception:
            csv_path = os.path.splitext(path)[0] + ".csv"
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                import csv
                w = csv.writer(f)
                w.writerow(["指标", "值"])
                for k, v in s.items():
                    if not isinstance(v, (list, dict)):
                        w.writerow([k, v])
                w.writerow([])
                w.writerow(["秒", "发送", "回包", "发送字节", "回包字节"])
                for x in s["series"]:
                    w.writerow([x["sec"], x["sent"], x["reply"],
                                x["sent_bytes"], x["reply_bytes"]])
            return csv_path

        wb = Workbook()
        bold = Font(bold=True)
        ws = wb.active
        ws.title = "汇总"
        ws.append(["指标", "值"])
        for c in ws[1]:
            c.font = bold
        rows = [
            ("标签", s["label"]), ("主机", s["host"]), ("平台", s["platform"]),
            ("开始", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s["start"]))),
            ("时长(s)", round(s["duration"], 3)),
            ("发送条数", s["sent"]), ("发送失败", s["send_fail"]),
            ("发送速率(条/秒)", round(s["send_per_sec"], 2)),
            ("回包条数", s["reply"]), ("回包超时", s["timeout_reply"]),
            ("在途未回", s["outstanding"]),
            ("回包速率(条/秒)", round(s["reply_per_sec"], 2)),
            ("发送字节", s["sent_bytes"]),
            ("发送字节速率(MB/s)", round(s["sent_bytes_per_sec"] / 1048576.0, 4)),
            ("均发送字节/条", round(s.get("avg_sent_bytes", 0), 1)),
            ("回包字节", s["reply_bytes"]),
            ("回包字节速率(MB/s)", round(s["reply_bytes_per_sec"] / 1048576.0, 4)),
            ("均回包字节/条", round(s.get("avg_reply_bytes", 0), 1)),
            ("延迟样本数", s["lat_count"]),
            ("平均响应时间(us)", round(s.get("lat_avg_us", 0), 1)),
            ("平均响应时间(ms)", round(s.get("lat_avg_ms", 0), 4)),
            ("P50(ms)", round(s.get("lat_p50_ms", 0), 4)),
            ("P90(ms)", round(s.get("lat_p90_ms", 0), 4)),
            ("P95(ms)", round(s.get("lat_p95_ms", 0), 4)),
            ("P99(ms)", round(s.get("lat_p99_ms", 0), 4)),
            ("最大(ms)", round(s.get("lat_max_ms", 0), 4)),
            ("最小(ms)", round(s.get("lat_min_ms", 0), 4)),
            ("客户端CPU(进程%)", round(s["cpu"]["proc_percent"], 2)),
            ("客户端CPU(核)", round(s["cpu"]["proc_cores"], 3)),
            ("整机CPU(%)", s["cpu"].get("sys_percent")),
        ]
        for r in rows:
            ws.append(list(r))
        ws.column_dimensions["A"].width = 24
        ws.column_dimensions["B"].width = 32

        ws2 = wb.create_sheet("按秒")
        ws2.append(["秒", "发送", "回包", "发送字节", "回包字节"])
        for c in ws2[1]:
            c.font = bold
        for x in s["series"]:
            ws2.append([x["sec"], x["sent"], x["reply"],
                        x["sent_bytes"], x["reply_bytes"]])
        for col, w in zip("ABCDE", (8, 10, 10, 14, 14)):
            ws2.column_dimensions[col].width = w

        if s.get("latencies_sample"):
            pass
        ws3 = wb.create_sheet("错误")
        ws3.append(["错误", "次数"])
        for c in ws3[1]:
            c.font = bold
        for k, v in sorted(s["errors"].items(), key=lambda x: -x[1]):
            ws3.append([k, v])
        ws3.column_dimensions["A"].width = 60

        wb.save(path)
        return path


class LatencyTracker(object):
    """可选：把每条的延迟单独存下来，方便事后画分布。"""

    def __init__(self, limit=200000):
        self.limit = limit
        self.rows = []
        self.lock = threading.Lock()

    def add(self, req_id, ms):
        with self.lock:
            if len(self.rows) < self.limit:
                self.rows.append((req_id, ms))

    def dump(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("request_id,latency_ms\n")
            for a, b in self.rows:
                f.write("%s,%.4f\n" % (a, b))
        return path
