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
    python send_test.py --cases C201,C203-C210 --no-send
    # 只发指定的几条，逐条打印
    python send_test.py --cases C001 --quiet 0

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

import config as cfgmod
import excel_loader as XL
import perf_stats as PS
import protocol as P
import safety
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

        # ---- 回包明细：用于「create 造完单 -> 落盘 refs.json -> 再生成 modify/remove」
        # rid -> 用例编号 / 账号（发送时登记），以及成功回包里的 Ref
        self._rid_meta = {}
        self._rid_meta_lock = threading.Lock()
        self.refs = []          # [{"account":..., "case_no":..., "ref":...}, ...]
        self._refs_lock = threading.Lock()

        # 全局限速用
        self._rate_lock = threading.Lock()
        self._next_allow = 0.0

    # ------------------------------------------------------------ 工具
    def _next_req_id(self):
        """请求号 = STTEST_<epoch毫秒>_<序号>。

        ★★ 不要改这个格式 ★★
        策略平台会【解析】request_id。真 DataHub 发的是 ST_<ip>_<epoch>_<n>
        （见 protocol.py），即"下划线分隔、后两段是数字"。
        曾把用例编号塞进来（STTEST_C201#1），结果平台读到第 1 条就再也不动了：
            ST-51  entries-read=1  lag=95
            ST-55  entries-read=1  lag=96
        想关联用例，用本地 _rid_meta（rid -> 用例编号），别动这个字段。
        """
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

    # ------------------------------------------------------------ 回包明细
    def _remember_rid(self, rid, case_no, payload):
        """登记 rid -> (用例编号, 账号)，供回包回来时关联出 Ref。

        只对 create 有意义（它才产生条件单号），但登记很便宜，不做区分，
        这样任何接口想扩展"从回包抓字段"都能直接用。
        """
        acct = ""
        try:
            obj = json.loads(payload)
            body = obj.get("create") if isinstance(obj, dict) else None
            if isinstance(body, dict):
                a = body.get("Account")
                if isinstance(a, dict):
                    acct = str(a.get("FAccount") or "")
        except Exception:
            pass
        with self._rid_meta_lock:
            self._rid_meta[rid] = (str(case_no), acct)
            # 防内存无限增长（压了几十万条时没回包的 rid 会堆积）
            if len(self._rid_meta) > 500000:
                for k in list(self._rid_meta)[:100000]:
                    self._rid_meta.pop(k, None)

    def _on_reply_body(self, rid, task):
        """回包里若带 Ref，就记成 (账号, 用例号, Ref)。

        ⚠ 策略平台方向的回包格式与 datahub_test(WT 方向)【不同】：
          ST 方向是平的   {"Ref":"2026...","Errmsg":"insert success","ErrID":0}
          WT 方向是嵌套的 {"Err":0,"results":[{"Ref":"..."}]}
        这里两种都兼容，取到第一个 Ref 就记下。
        """
        if not task:
            return
        try:
            obj = json.loads(task)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        ref = obj.get("Ref")
        if not ref:
            for key in ("results", "Results"):
                lst = obj.get(key)
                if isinstance(lst, list):
                    for it in lst:
                        if isinstance(it, dict) and it.get("Ref"):
                            ref = it["Ref"]
                            break
                if ref:
                    break
        if not ref:
            return
        with self._rid_meta_lock:
            meta = self._rid_meta.pop(rid, None)
        if not meta:
            return
        case_no, acct = meta
        with self._refs_lock:
            self.refs.append({"account": acct, "case_no": case_no,
                              "ref": str(ref)})

    def save_refs(self, path):
        """把 (账号, 用例号, Ref) 落盘成 JSON。返回路径或 None（没抓到）。"""
        with self._refs_lock:
            rows = list(self.refs)
        if not rows:
            return None
        # 去重（同一个 Ref 只留一条，压测循环复用时同一用例会重复出现）
        seen = {}
        for r in rows:
            seen[r["ref"]] = r
        rows = sorted(seen.values(), key=lambda x: x["case_no"])
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        return path

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
            no, typ, desc, payload = item[:4]
            if self.max_count and self._sent >= self.max_count:
                break
            try:
                if conn is None or not conn.connected:
                    conn = RespClient(self.kw["host"], self.kw["port"],
                                      self.kw["password"], self.kw["db"]).connect()
                rid = self._next_req_id()
                self._pace()
                # 关键：必须在 XADD 之前登记，否则回包可能在 track() 之前就回来了
                # （实测 mock 回包只要 ~1ms），那条回包就会被当成"无主回包"丢掉，
                # 同时 pending 里留下一个永远不消失的孤儿，把"在途"和响应时间都算错。
                if not self.no_reply:
                    self.perf.track(rid)
                    self._remember_rid(rid, no, payload)
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
                        # 从回包里抓 Ref（create 造单后落盘，供 modify/remove 用）
                        self._on_reply_body(rid, task)
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


# ================================================================ 未回包归因
def report_unreplied(sender, logger, stream="", kw=None):
    """把「发了但没等到回包」的请求按用例编号汇总打印。

    用例编号从【本地】_rid_meta（rid -> 用例编号）里取，不靠 request_id 的内容。
    ★ 这是刻意的：request_id 的形态不能动（平台会解析它，见 _next_req_id），
      所以"rid 对应哪条用例"只能在本进程里记，不能写进报文。

    同时读服务端 PEL（平台读了但没 XACK），两边一对照就能区分：
      本地没回包 + PEL 里有   -> 平台收到了但卡住/崩了（最像"挂了"）
      本地没回包 + PEL 里没有 -> 平台压根没读（消费者不在/流名不对）
      全都回了               -> 平台正常
    """
    pend = sender.perf.pending_ids() if hasattr(sender.perf, "pending_ids") else []
    with sender._rid_meta_lock:
        meta = dict(sender._rid_meta)
    cases = [str(meta[r][0]) for r in pend if r in meta]
    unknown = len(pend) - len(cases)
    uniq = sorted(set(cases))

    logger.write("-" * 60, force=True)
    if not pend:
        logger.write("★ 回包核对：没有未回包的请求（发出的都收到了回包）",
                     force=True)
    else:
        from collections import Counter
        cnt = Counter(cases)
        detail = " ".join("%s×%d" % (c, n) if n > 1 else c
                          for c, n in cnt.most_common(40))
        logger.write("★ 未回包 %d 条，涉及 %d 个用例：%s"
                     % (len(pend), len(uniq),
                        detail or "（用例编号未知）"), force=True)
        if len(cnt) > 40:
            logger.write("  （只列了前 40 个）", force=True)
        if unknown:
            logger.write("  （其中 %d 条查不到用例编号）" % unknown, force=True)
        logger.write("  说明：这些用例发出去后没等到回包，"
                     "很可能就是让平台卡住的那几条。", force=True)

    # ---- 服务端视角：目标流 PEL 里还有多少没被 XACK ----
    if kw and stream:
        try:
            c = RespClient(kw["host"], kw["port"], kw["password"],
                           kw["db"]).connect()
            try:
                g = next((x for x in c.xinfo_groups(stream)
                          if x.get("name") == P.GROUP), None)
                if not g:
                    logger.write("  服务端：%s 上没有消费者组 %s —— "
                                 "平台可能根本没在消费这条流" % (stream, P.GROUP),
                                 force=True)
                else:
                    logger.write("  服务端：%s 组 %s  已读=%s 未ACK=%s lag=%s"
                                 % (stream, P.GROUP, g.get("entries-read"),
                                    int(g.get("pending") or 0), g.get("lag")),
                                 force=True)
                    # 未 ACK 的条目逆查本地 meta，给出用例编号
                    ids = c.cmd("XPENDING", stream, P.GROUP, "-", "+",
                                "20") or []
                    got = []
                    for row in ids:
                        eid = row[0]
                        try:
                            ent = c.xrange(stream, eid, eid, 1)
                            if not ent:
                                continue
                            rid = dict(ent[0][1]).get("request_id", "")
                            got.append(str(meta[rid][0]) if rid in meta
                                       else "?")
                        except Exception:
                            pass
                    if got:
                        logger.write("  未ACK 前 %d 条的用例编号：%s"
                                     % (len(got), " ".join(got)), force=True)
                        if "?" in got:
                            logger.write("  （? = 该条不是本次运行发的，"
                                         "本地没有它的用例映射）", force=True)
            finally:
                c.close()
        except Exception as e:
            logger.write("  服务端核对失败（不影响结论）：%s" % e, force=True)
    logger.write("-" * 60, force=True)
    return uniq


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
            payload = cases[i % len(cases)][3]
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
        warn = ("（这 %d 条是额外真实写入 %s 的报文，"
                "会计入该流 XLEN 但不计入上面的发送/回包统计）" % (m, stream))
        if logger:
            logger.write("同步RTT探测（单发单收，%d 次）: 平均 %.3f ms  "
                         "P50 %.3f  P95 %.3f  最大 %.3f %s"
                         % (m, out["avg_ms"], out["p50_ms"],
                            out["p95_ms"], out["max_ms"], warn), force=True)
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
    ap.add_argument("--excel", default="",
                    help="Excel 路径（默认 data/{接口}.xlsx）")
    ap.add_argument("--type", default=None,
                    help="用例类型：normal/destroy/all")
    ap.add_argument("--cases", default="",
                    help="按编号筛选，如 C201,C203-C210。前缀=接口首字母：C=create M=modify R=remove P=pwdUpdate A=account")
    ap.add_argument("--list-cases", action="store_true", help="只列出用例，不发送")

    # 压测参数
    ap.add_argument("--workers", type=int, default=None, help="并发线程数")
    ap.add_argument("--max", type=int, default=None,
                    help="发送总条数；0=不限（循环复用用例直到手动停止或 --seconds 到点）")
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

    # 回填：create 造完单后把 (账号, Ref) 落盘，供 modify/remove 生成用例
    ap.add_argument("--refs-out", default="",
                    help="把回包里抓到的条件单号写成 JSON（默认 <stats-out>/<标签>_refs.json）。"
                         "发 create 时用，之后配合 make_excel --ref-map 生成 modify/remove 数据")
    ap.add_argument("--no-refs-out", action="store_true",
                    help="不写 refs JSON（默认发 create 会自动写）")

    # 输出
    ap.add_argument("--no-send", action="store_true", help="只生成/预览报文，不发送")
    ap.add_argument("--quiet", type=int, default=1, help="1=安静（默认）")
    ap.add_argument("--no-stats", action="store_true", help="不落盘统计")
    ap.add_argument("--stats-out", default=PERF_DIR, help="统计输出目录")
    ap.add_argument("--stats-interval", type=float, default=1.0, help="按秒统计间隔")
    ap.add_argument("--dump", default="",
                    help="把生成的报文写到这个 jsonl（便于复核）")
    ap.add_argument("--label", default="", help="统计标签")
    ap.add_argument("--force-live", action="store_true",
                    help="关掉「目标流有外来消费者」的安全闸，强行发送（危险："
                         "可能给真实策略平台下假单。确认过 check_env.py 再用）")
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
    # 否则 --cases C201（destroy 用例）会被默认的 type=normal 过滤成 0 条，
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

    # ---- 从 Excel 读用例 ----
    excel = a.excel or XL.default_excel(interface)
    want = None
    if type_tag and type_tag != "all":
        want = {x.strip().lower() for x in str(type_tag).split(",") if x.strip()}
    # max_cases 提前终止：只要 --max 小且没按编号筛，就没必要读完整表
    early = max_count if (max_count and not a.cases) else 0
    pool = XL.load_cases(excel, want_types=want, cases_spec=a.cases,
                         max_cases=early, quiet=bool(a.quiet))

    if not pool:
        msg = ("没有匹配的用例：excel=%s type=%s interface=%s cases=%r\n"
               "提示：--cases 支持编号与区间，如 C001,C003-C010；"
               "写 --cases 时会自动把 --type 放宽为 all。\n"
               "若 Excel 不存在，先生成：python make_excel.py --interface %s"
               % (excel, type_tag, interface, a.cases, interface))
        if a.list_cases or a.no_send:
            print(msg)
            return 1
        raise SystemExit(msg)

    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(PERF_DIR, exist_ok=True)

    # ---- 只列用例 ----
    if a.list_cases:
        print("用例总数: %d（excel=%s type=%s cases=%r）"
              % (len(pool), os.path.basename(excel), type_tag, a.cases))
        for no, typ, desc, txt, _rn in pool:
            print("  [%-6s] %-8s %-46s len=%d" % (no, typ, desc, nbytes_len(txt)))
        return 0

    # ---- 预览模式 ----
    if a.no_send:
        print("=" * 78)
        print("预览模式（不发送） 目标流 = %s @ %s:%s db%s"
              % (stream, kw["host"], kw["port"], kw["db"]))
        print("Excel = %s" % excel)
        print("用例数 %d  type=%s" % (len(pool), type_tag))
        print("=" * 78)
        dump = open(a.dump, "w", encoding="utf-8") if a.dump else None
        try:
            for no, typ, desc, txt, _rn in pool:
                print("\n--- [%s] %s  %s  (len=%d)" % (no, typ, desc, nbytes_len(txt)))
                print("XADD %s * request_id <rid> task %s" % (stream, txt[:1500]))
                if dump:
                    dump.write(json.dumps({"case": no, "type": typ,
                                           "desc": desc, "stream": stream,
                                           "task": txt}, ensure_ascii=False) + "\n")
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
                 % (workers, max_count or "不限(循环发到停止)", rate or "不限",
                    wait, a.seconds),
                 force=True)
    if not max_count and not (a.seconds and a.seconds > 0):
        logger.write("★ 提示：--max 0 且未设 --seconds = 不限量，会一直循环发送，"
                     "直到手动停止（不是「每种发一次」）。", force=True)

    # 确保流和消费组存在（策略平台 Mock 会建；这里兜底，且保证 XLEN 可查）
    try:
        c = RespClient(kw["host"], kw["port"], kw["password"], kw["db"]).connect()

        # ---------------------------------------------------------------
        # 安全闸：目标流若已被"不是我们建的"消费者占着，说明那可能是
        # 【真实策略平台】的流。往它里面 XADD = 给真平台下假单，
        # 可能触发真实交易。判定逻辑见 safety.py（与 mock 共用）。
        # ---------------------------------------------------------------
        if not safety.guard_stream(c, stream, force_live=a.force_live,
                                   tool="send_test", what="发送"):
            c.close()
            return 2
        if c.cmd("EXISTS", stream):
            logger.write("安全闸：%s 上未发现外来消费者，可安全发送" % stream,
                         force=True)

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

    # ---- 「到底哪几条没回包」：按用例编号汇总（用例编号取自本地映射）----
    if not a.no_reply:
        report_unreplied(sender, logger, stream, kw)

    # ---- 回填文件：create 造完单，把 (账号, Ref) 落盘 ----
    # 有了它，modify/remove 就能用【真实存在的单号】生成用例（--ref-map），
    # 而不是靠行序猜。见 README §5.2。
    if not a.no_refs_out and not a.no_reply and sender.refs:
        rp = a.refs_out or os.path.join(a.stats_out,
                                        "%s_refs.json" % label)
        try:
            saved = sender.save_refs(rp)
            if saved:
                with sender._refs_lock:
                    n = len({r["ref"] for r in sender.refs})
                logger.write("★ 抓到 %d 个条件单号，已写入: %s" % (n, saved),
                             force=True)
                logger.write("  下一步可用它生成 modify/remove 用例：", force=True)
                logger.write("    python make_excel.py --interface remove "
                             "--bulk-normal %d --ref-map %s" % (n, saved),
                             force=True)
        except Exception as e:
            logger.write("写 refs 失败: %s" % e, force=True)

    logger.write("日志: %s" % logfile, force=True)
    logger.close()
    return 0


def nbytes_len(s):
    return len(s.encode("utf-8"))


if __name__ == "__main__":
    sys.exit(main())
