# -*- coding: utf-8 -*-
"""
send_test.py —— 手动 XADD 发送器（数据中台 → 策略平台）
=======================================================
背景：以前是靠 MQ 插件发的，现在插件没了，全部改手动 XADD。
      本脚本就是那个"手动"，负责把报文塞进 ST-<id> 流，并可选地收策略平台回包。

做的事情：
  1. 从 cases.py 取用例（normal / destroy），展开 token，生成 task 字符串
  2. 多线程 XADD 到 ST-<assign-id>（可用 --stream 直接指定）
  3. 可选：起一个读取线程 XREAD 回包流，按 request_id 配回发送时刻算响应时间
  4. 统计吞吐 / 字节 / CPU / 延迟分位，落盘 JSON + Excel + 日志

典型用法：
    # 预览报文（不发）
    python send_test.py --interface create --no-send
    # 正常压测：4 线程发 10000 条，等回包
    python send_test.py --interface create --type normal --workers 4 --max 10000
    # 破坏测试
    python send_test.py --type destroy --workers 8 --max 5000
    # 指定用例发送（定位问题时很有用）
    python send_test.py --cases D001,D003-D010 --no-send
    # 只发指定的几条，逐条打印
    python send_test.py --cases N001 --quiet 0

注意：--max 大于用例数时会循环复用用例（压测数据不够时的常规做法）。
"""
import argparse
import json
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cases as C
import config as cfgmod
import perf_stats as PS
import protocol as P
from resp_min import RespClient, RespError

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
LOG_DIR = os.path.join(OUT_DIR, "logs")
PERF_DIR = os.path.join(OUT_DIR, "performance")


def ts():
    return time.strftime("%H:%M:%S")


def log(msg, quiet=False, force=False):
    if quiet and not force:
        return
    print("[%s] %s" % (ts(), msg), flush=True)


class Logger(object):
    """同时往控制台和文件写。"""

    def __init__(self, path, quiet=False, echo=True):
        self.quiet = quiet
        self.echo = echo
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.f = open(path, "a", encoding="utf-8")
        self.lock = threading.Lock()
        self.write("=" * 78)
        self.write("运行开始 %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        self.write("=" * 78)

    def write(self, msg, force=False):
        line = "[%s] %s" % (ts(), msg)
        with self.lock:
            self.f.write(line + "\n")
            self.f.flush()
        if (not self.quiet or force) and self.echo:
            print(line, flush=True)

    def close(self):
        try:
            self.write("运行结束 %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
            self.f.close()
        except Exception:
            pass


# ================================================================ 发送引擎
class Sender(object):
    def __init__(self, kw, stream, cases, workers=4, max_count=0, rate=0,
                 quiet=False, logger=None, wait=5.0, no_reply=False,
                 reply_stream="", auto_reply_stream=True, timeout_expire=30.0,
                 stats_interval=1.0, label=""):
        self.kw = kw
        self.stream = stream
        self.cases = cases
        self.workers = max(1, int(workers))
        self.max_count = int(max_count or 0)
        self.rate = float(rate or 0)          # 全局限速（条/秒），0=不限
        self.quiet = quiet
        self.logger = logger
        self.wait = float(wait)
        self.no_reply = no_reply
        self.reply_stream = reply_stream
        self.auto_reply_stream = auto_reply_stream
        self.timeout_expire = float(timeout_expire)
        self.label = label

        self.perf = PS.PerfCollector(interval=stats_interval, label=label)
        self.stop = threading.Event()
        self._q = queue.Queue()
        self._sent_lock = threading.Lock()
        self._sent = 0
        self.req_seq = 0
        self._req_lock = threading.Lock()
        self._threads = []
        self._reader = None
        self.reply_seen = 0

        # 全局限速用
        self._rate_lock = threading.Lock()
        self._next_allow = 0.0

    # ------------------------------------------------------------ 工具
    def _next_req_id(self):
        with self._req_lock:
            self.req_seq += 1
            return "STTEST_%d_%d" % (int(time.time() * 1000), self.req_seq)

    def _pace(self):
        """按 --rate 限速。"""
        if self.rate <= 0:
            return
        with self._rate_lock:
            now = time.time()
            if self._next_allow < now:
                self._next_allow = now
            self._next_allow += 1.0 / self.rate
            delay = self._next_allow - now
        if delay > 0:
            time.sleep(min(delay, 1.0))

    # ------------------------------------------------------------ 发送
    def _producer(self):
        """按需循环复用用例，塞进队列。"""
        n = 0
        total = self.max_count
        closed = 0
        while not self.stop.is_set():
            if total and n >= total:
                break
            try:
                self._q.put(self.cases[n % len(self.cases)], timeout=0.5)
                n += 1
            except queue.Full:
                continue
        # 放毒丸
        for _ in range(self.workers):
            while not self.stop.is_set():
                try:
                    self._q.put(None, timeout=0.5)
                    break
                except queue.Full:
                    continue

    def _worker(self, wid):
        conn = None
        while not self.stop.is_set():
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            no, iface, desc, fn = item
            if self.max_count and self._sent >= self.max_count:
                break
            try:
                if conn is None or not conn.connected:
                    conn = RespClient(self.kw["host"], self.kw["port"],
                                      self.kw["password"], self.kw["db"]).connect()
                payload = C.payload_text(fn())
                rid = self._next_req_id()
                self._pace()
                # 关键：必须在 XADD 之前登记，否则回包可能在 track() 之前就回来了
                # （实测 mock 回包只要 ~1ms），那条回包就会被当成"无主回包"丢掉，
                # 同时 pending 里留下一个永远不消失的孤儿，把"在途"和响应时间都算错。
                if not self.no_reply:
                    self.perf.track(rid)
                conn.cmd("XADD", self.stream, "*", "request_id", rid, "task", payload)
                nbytes = len(payload.encode("utf-8"))
                with self._sent_lock:
                    self._sent += 1
                self.perf.record_send(1, nbytes)
                if self.logger and not self.quiet:
                    self.logger.write("→ [%s] w%d #%s %s rid=%s len=%d"
                                      % (self.stream, wid, no, desc, rid, nbytes))
            except Exception as e:
                self.perf.record_send_fail("%s: %s" % (type(e).__name__, e))
                if self.logger:
                    self.logger.write("发送失败 [%s] %s: %s" % (no, desc, e), force=True)
                try:
                    if conn:
                        conn.close()
                except Exception:
                    pass
                conn = None
                time.sleep(0.2)

        try:
            if conn:
                conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------ 收包
    def _resolve_reply_stream(self):
        if self.reply_stream:
            return self.reply_stream
        if self.auto_reply_stream:
            return P.STREAM_REPLY
        return None

    def _reader_loop(self):
        """XREAD 回包流，按 request_id 配回发送时刻算 RTT。"""
        stream = self._resolve_reply_stream()
        if not stream:
            return
        last = "$"
        conn = None
        idle = 0
        while not self.stop.is_set():
            try:
                if conn is None or not conn.connected:
                    conn = RespClient(self.kw["host"], self.kw["port"],
                                      self.kw["password"], self.kw["db"]).connect()
                    # 首次从"当前末尾"开始读，避免把历史回包算进来
                    raw = conn.cmd("XREVRANGE", stream, "+", "-", "COUNT", "1")
                    if raw:
                        last = raw[0][0]
                    else:
                        last = "$"
                    if self.logger:
                        self.logger.write("回包读取线程就绪: XREAD BLOCK ... STREAMS %s %s"
                                          % (stream, last))
                old = conn.sock.gettimeout()
                conn.sock.settimeout(2.5)
                try:
                    raw = conn.cmd("XREAD", "BLOCK", "500", "COUNT", "2000",
                                   "STREAMS", stream, last)
                finally:
                    try:
                        conn.sock.settimeout(old)
                    except Exception:
                        pass
                if not raw:
                    idle += 1
                    if idle % 20 == 0:
                        self.perf.expire_pending(self.timeout_expire)
                    continue
                idle = 0
                for st, entries in raw:
                    for eid, fields in RespClient._entries(entries):
                        last = eid
                        rid = fields.get("request_id", "")
                        task = fields.get("task", "")
                        self.reply_seen += 1
                        self.perf.record_reply(rid, len(str(task).encode("utf-8")))
                        if self.logger and not self.quiet:
                            self.logger.write("← [%s] #%s rid=%s task=%s"
                                              % (st, eid, rid, str(task)[:200]))
            except Exception as e:
                if self.stop.is_set():
                    return
                self.perf.record_send_fail("reader: %s" % e)
                try:
                    if conn:
                        conn.close()
                except Exception:
                    pass
                conn = None
                time.sleep(1.0)

    # ------------------------------------------------------------ 运行
    def run(self):
        if not self.cases:
            raise SystemExit("没有可用用例，检查 --interface/--type/--cases")

        self.perf.start()
        if not self.no_reply:
            self._reader = threading.Thread(target=self._reader_loop,
                                            name="reader", daemon=True)
            self._reader.start()
            time.sleep(0.5)     # 让读取线程先定位到流末尾

        self._threads.append(threading.Thread(target=self._producer, name="producer"))
        for i in range(self.workers):
            self._threads.append(
                threading.Thread(target=self._worker, args=(i,), name="w%d" % i))
        for t in self._threads:
            t.start()
        for t in self._threads:
            t.join()

        # 发完了，等回包
        if not self.no_reply and self.wait > 0:
            log("发送完成，等待回包 %.1fs（在途 %d）..."
                % (self.wait, self.perf.outstanding),
                self.quiet, force=True)
            end = time.time() + self.wait
            last_out = self.perf.outstanding
            stable = 0
            while time.time() < end and not self.stop.is_set():
                time.sleep(0.3)
                out = self.perf.outstanding
                if out == 0:
                    break
                if out == last_out:
                    stable += 1
                    if stable >= 8:      # 约 2.4s 没有新回包就不再等
                        break
                else:
                    stable = 0
                last_out = out
        self.perf.stop()
        return self.perf


# ================================================================ 同步 RTT 探测
def probe_sync_latency(kw, stream, reply_stream, cases, n=30, logger=None,
                       quiet=False, timeout=5.0):
    """发一条、立刻等这条的回包，测【链路真实 RTT】。

    为什么需要它：send_test 主流程是"多线程狂发 + 一个读取线程收"，
    读到的延迟里含读取线程的批量等待（BLOCK/COUNT 攒批），
    压测并发高时会明显大于单条真实往返。这个函数给出干净的对照值。

    返回 dict 或 None（没有回包流/失败）。
    """
    if not reply_stream:
        return None
    try:
        send = RespClient(kw["host"], kw["port"], kw["password"], kw["db"]).connect()
        read = RespClient(kw["host"], kw["port"], kw["password"], kw["db"]).connect()
    except Exception as e:
        if logger:
            logger.write("同步RTT探测：连接失败 %s" % e, force=True)
        return None

    lat = []
    try:
        raw = read.cmd("XREVRANGE", reply_stream, "+", "-", "COUNT", "1")
        last = raw[0][0] if raw else "$"
        for i in range(int(n)):
            no, iface, desc, fn = cases[i % len(cases)]
            payload = C.payload_text(fn())
            rid = "SYNC_%d_%d" % (int(time.time() * 1000), i)
            t0 = time.perf_counter()
            send.cmd("XADD", stream, "*", "request_id", rid, "task", payload)
            got = False
            deadline = time.time() + timeout
            while time.time() < deadline:
                r = read.cmd("XREAD", "BLOCK", "200", "COUNT", "200",
                             "STREAMS", reply_stream, last)
                if not r:
                    continue
                for st, entries in r:
                    for eid, f in RespClient._entries(entries):
                        last = eid
                        if f.get("request_id") == rid:
                            got = True
                if got:
                    break
            lat.append((time.perf_counter() - t0) * 1000.0)
        lat.sort()
        m = len(lat)
        out = {
            "count": m,
            "avg_ms": sum(lat) / m,
            "min_ms": lat[0],
            "p50_ms": PS.percentile(lat, 0.50),
            "p95_ms": PS.percentile(lat, 0.95),
            "max_ms": lat[-1],
        }
        if logger:
            logger.write("同步RTT探测（单发单收，%d 次）: 平均 %.3f ms  "
                         "P50 %.3f  P95 %.3f  最大 %.3f"
                         % (m, out["avg_ms"], out["p50_ms"],
                            out["p95_ms"], out["max_ms"]), force=True)
        if not quiet:
            print("[同步RTT] %d 次  平均 %.3f ms  P50 %.3f  P95 %.3f  最大 %.3f"
                  % (m, out["avg_ms"], out["p50_ms"], out["p95_ms"], out["max_ms"]),
                  flush=True)
        return out
    except Exception as e:
        if logger:
            logger.write("同步RTT探测失败: %s" % e, force=True)
        return None
    finally:
        try:
            send.close()
        except Exception:
            pass
        try:
            read.close()
        except Exception:
            pass


# ================================================================ CLI
def build_parser(cp):
    ap = argparse.ArgumentParser(
        description="手动 XADD 发送器（数据中台 → 策略平台）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    # Redis
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--pwd", default=None)
    ap.add_argument("--db", type=int, default=None)

    # 目标
    ap.add_argument("--assign-id", type=int, default=None,
                    help="数据中台分配给策略平台的编号，发送目标 = ST-<id>")
    ap.add_argument("--stream", default="",
                    help="直接指定目标流名（覆盖 --assign-id）")

    # 用例
    ap.add_argument("--interface", default=None,
                    help="接口：create/modify/remove/pwdUpdate/account/all")
    ap.add_argument("--type", default=None,
                    help="用例类型：normal/destroy/all")
    ap.add_argument("--cases", default="",
                    help="按编号筛选，如 D001,D003-D010,N001")
    ap.add_argument("--list-cases", action="store_true", help="只列出用例，不发送")

    # 压测参数
    ap.add_argument("--workers", type=int, default=None, help="并发线程数")
    ap.add_argument("--max", type=int, default=None, help="发送总条数，0=每种一次")
    ap.add_argument("--rate", type=float, default=None, help="全局限速 条/秒，0=不限")
    ap.add_argument("--wait", type=float, default=None, help="发完等回包秒数")
    ap.add_argument("--seconds", type=float, default=0,
                    help="按时间跑：跑够这么多秒就停（优先于 --max）")

    # 回包
    ap.add_argument("--no-reply", action="store_true", help="不读回包（纯发）")
    ap.add_argument("--reply-stream", default="",
                    help="回包流名，默认 DataHub_reply_stream")
    ap.add_argument("--reply-timeout", type=float, default=30.0,
                    help="超过这么久没回就算超时（秒）")
    ap.add_argument("--sync-probe", type=int, default=0,
                    help="压测后额外跑 N 次单发单收，测链路真实 RTT（对比批量值）")

    # 输出
    ap.add_argument("--no-send", action="store_true", help="只生成/预览报文，不发送")
    ap.add_argument("--quiet", type=int, default=1, help="1=安静（默认）")
    ap.add_argument("--no-stats", action="store_true", help="不落盘统计")
    ap.add_argument("--stats-out", default=PERF_DIR, help="统计输出目录")
    ap.add_argument("--stats-interval", type=float, default=1.0, help="按秒统计间隔")
    ap.add_argument("--dump", default="",
                    help="把生成的报文写到这个 jsonl（便于复核）")
    ap.add_argument("--label", default="", help="统计标签")
    return ap


def main():
    cp = cfgmod.load()
    ap = build_parser(cp)
    a = ap.parse_args()

    def g(attr, sec, key):
        v = getattr(a, attr, None)
        return v if v not in (None, "") else cfgmod.get(cp, sec, key)

    kw = cfgmod.redis_kwargs(cp, a)
    interface = g("interface", "test", "interface")
    type_tag = g("type", "test", "type")
    # 用户显式给了 --cases 但没给 --type 时，自动放宽到 all：
    # 否则 --cases D001（destroy 用例）会被默认的 type=normal 过滤成 0 条，
    # 看起来像"用例不存在"，很坑。
    if a.cases and a.type is None:
        type_tag = "all"
    workers = int(g("workers", "test", "workers"))
    max_count = int(g("max", "test", "max"))
    rate = float(g("rate", "test", "rate"))
    wait = float(g("wait", "test", "wait"))

    assign_id = a.assign_id if a.assign_id is not None else int(
        cfgmod.get(cp, "strategy", "assign_id"))
    stream = a.stream or P.stream_for(assign_id)

    pool = C.filter_cases(type_tag=type_tag, interface=interface, cases=a.cases)

    if not pool:
        msg = ("没有匹配的用例：type=%s interface=%s cases=%r\n"
               "提示：--cases 支持编号与区间，如 D001,D003-D010,N001；"
               "写 --cases 时会自动把 --type 放宽为 all。"
               % (type_tag, interface, a.cases))
        if a.list_cases or a.no_send:
            print(msg)
            return 1
        raise SystemExit(msg)

    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(PERF_DIR, exist_ok=True)

    # ---- 只列用例 ----
    if a.list_cases:
        print("用例总数: %d（type=%s interface=%s cases=%r）"
              % (len(pool), type_tag, interface, a.cases))
        for no, iface, desc, fn in pool:
            t = C.payload_text(fn())
            print("  [%s] %-9s %-46s len=%d" % (no, iface, desc, nbytes_len(t)))
        return 0

    # ---- 预览模式 ----
    if a.no_send:
        print("=" * 78)
        print("预览模式（不发送） 目标流 = %s @ %s:%s db%s"
              % (stream, kw["host"], kw["port"], kw["db"]))
        print("用例数 %d  type=%s interface=%s" % (len(pool), type_tag, interface))
        print("=" * 78)
        dump = open(a.dump, "w", encoding="utf-8") if a.dump else None
        try:
            for i, (no, iface, desc, fn) in enumerate(pool):
                t = C.payload_text(fn())
                print("\n--- [%s] %s  %s  (len=%d)" % (no, iface, desc, nbytes_len(t)))
                print("XADD %s * request_id <rid> task %s" % (stream, t[:1500]))
                if dump:
                    dump.write(json.dumps({"case": no, "interface": iface,
                                           "desc": desc, "stream": stream,
                                           "task": t}, ensure_ascii=False) + "\n")
        finally:
            if dump:
                dump.close()
                print("\n报文已写入 %s" % a.dump)
        return 0

    # ---- 真正发送 ----
    stamp = time.strftime("%Y%m%d_%H%M%S")
    label = a.label or ("%s_%s_%s" % (interface, type_tag, stamp))
    logfile = os.path.join(LOG_DIR, "%s.log" % label)
    logger = Logger(logfile, quiet=bool(a.quiet))

    logger.write("发送器启动：目标 %s:%s db%s  流=%s"
                 % (kw["host"], kw["port"], kw["db"], stream), force=True)
    logger.write("用例: type=%s interface=%s cases=%r  共 %d 条"
                 % (type_tag, interface, a.cases, len(pool)), force=True)
    logger.write("并发=%d  总数=%s  限速=%s 条/秒  等回包=%.1fs  seconds=%.1f"
                 % (workers, max_count or "每种一次", rate or "不限", wait, a.seconds),
                 force=True)

    # 确保流和消费组存在（策略平台 Mock 会建；这里兜底，且保证 XLEN 可查）
    try:
        c = RespClient(kw["host"], kw["port"], kw["password"], kw["db"]).connect()
        r = c.xgroup_create(stream, P.GROUP, "0", mkstream=True)
        logger.write("XGROUP CREATE %s %s -> %s" % (stream, P.GROUP, r), force=True)
        logger.write("目标流发送前 XLEN = %s" % c.xlen(stream), force=True)
        c.close()
    except Exception as e:
        logger.write("准备目标流失败: %s" % e, force=True)

    sender = Sender(
        kw=kw, stream=stream, cases=pool, workers=workers, max_count=max_count,
        rate=rate, quiet=bool(a.quiet), logger=logger, wait=wait,
        no_reply=a.no_reply, reply_stream=a.reply_stream,
        timeout_expire=a.reply_timeout, stats_interval=a.stats_interval,
        label=label)

    t0 = time.time()
    if a.seconds and a.seconds > 0:
        # 按时间跑：把 max 设成一个很大的数，用计时线程喊停
        sender.max_count = 0
        timer = threading.Timer(a.seconds, sender.stop.set)
        timer.daemon = True
        timer.start()
    perf = sender.run()
    dur = time.time() - t0

    result = perf.print_report(title="发送统计")

    # 同步 RTT 对照（可选）：给出不含读取线程攒批开销的真实往返
    if a.sync_probe > 0 and not a.no_reply:
        rs = a.reply_stream or P.STREAM_REPLY
        probe_sync_latency(kw, stream, rs, pool, n=a.sync_probe,
                           logger=logger, quiet=bool(a.quiet))

    logger.write("实际耗时 %.3f s" % dur, force=True)
    logger.write("发送 %d 条 / 回包 %d 条 / 失败 %d"
                 % (result["sent"], result["reply"], result["send_fail"]), force=True)

    if not a.no_stats:
        jp = os.path.join(a.stats_out, "%s_stats.json" % label)
        xp = os.path.join(a.stats_out, "%s.xlsx" % label)
        try:
            perf.write_json(jp)
            logger.write("统计JSON: %s" % jp, force=True)
        except Exception as e:
            logger.write("写JSON失败: %s" % e, force=True)
        try:
            p = perf.write_excel(xp)
            logger.write("统计Excel: %s" % p, force=True)
        except Exception as e:
            logger.write("写Excel失败: %s" % e, force=True)
        try:
            c = RespClient(kw["host"], kw["port"], kw["password"], kw["db"]).connect()
            logger.write("目标流发送后 XLEN = %s" % c.xlen(stream), force=True)
            c.close()
        except Exception:
            pass

    logger.write("日志: %s" % logfile, force=True)
    logger.close()
    return 0


def nbytes_len(s):
    return len(s.encode("utf-8"))


if __name__ == "__main__":
    sys.exit(main())
