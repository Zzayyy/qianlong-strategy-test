# -*- coding: utf-8 -*-
"""
mock_strategy.py —— 模拟策略平台（Mock Strategy Platform）
==========================================================
背景：这次要测「数据中台 → 策略平台」，但策略平台暂未开放，
      所以用本脚本顶替它，把策略平台该做的动作全做一遍。

它严格按 protocol.py 里实测出来的时序工作：
  1. 每 2 秒 PUBLISH strategyserver_online {"id":-1,...}  直到拿到编号
  2. SUBSCRIBE 自己的 unique_string，收到数据中台分配的编号
  3. XGROUP CREATE ST-<id> / ST-<id>-reply（MKSTREAM，已存在则忽略）
  4. 循环 XREADGROUP ... STREAMS ST-<id> ST-<id>-reply > >
  5. 每收到一条报文：回调打印，然后 XADD DataHub_reply_stream
     request_id <原样带回> task <reply_data>，再 XACK
  6. 每 5 秒 PUBLISH strategyserver_beat {"id":<id>,...}
  7. 退出时 PUBLISH strategyserver_offline {"id":<id>}

用法：
    # 最常用：连 137 db0，分配编号 1（即下发流 ST-1）
    python mock_strategy.py --host 192.168.1.137 --db 0 --assign-id 1

    # 只看不发回包（观察真数据中台到底往 ST-1 发什么）
    python mock_strategy.py --assign-id 1 --no-reply

    # 不自己应答上线，等真数据中台应答（真中台在跑时用）
    python mock_strategy.py --no-assign --unique ST-761-2cea7fd9d5c0test

    # 压测：只回包不打印
    python mock_strategy.py --assign-id 1 --quiet --workers 4
"""
import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as cfgmod
import protocol as P
from resp_min import RespClient, RespError


def ts():
    return time.strftime("%H:%M:%S")


def log(msg, quiet=False, force=False):
    if quiet and not force:
        return
    print("[%s] %s" % (ts(), msg), flush=True)


class MockStrategy(object):
    """策略平台模拟器。

    线程模型：
      _online_thread  上线重发（拿到编号后转为"确认上线"再停止）
      _beat_thread    心跳
      _reply_thread   消费 ST-<id>，回包 DataHub_reply_stream
      _sub_thread     订阅 unique_string，收数据中台分配的编号 / 中台心跳
    """

    def __init__(self, kw, unique, assign_id, usecount=1,
                 reply_data=P.REPLY_OK, do_reply=True, do_assign=True,
                 online_interval=2.0, beat_interval=5.0, reply_mode=P.REPLY_AUTO,
                 workers=1, read_count=100, quiet=False, listener=None,
                 max_messages=0, channel_suffix="", verbose=False,
                 log_first=20, log_every=200):
        self.kw = kw
        self.unique = unique
        self.assign_id = int(assign_id)
        self.usecount = int(usecount)
        self.channel_suffix = channel_suffix or ""
        # 上线/心跳/下线频道（现场存在不带后缀与带 _1 两套，见 protocol.chan）
        self.ch_online = P.chan(P.CH_STRATEGY_ONLINE, self.channel_suffix)
        self.ch_beat = P.chan(P.CH_STRATEGY_BEAT, self.channel_suffix)
        self.ch_offline = P.chan(P.CH_STRATEGY_OFFLINE, self.channel_suffix)
        self.reply_data = reply_data
        self.do_reply = do_reply
        self.do_assign = do_assign
        self.online_interval = float(online_interval)
        self.beat_interval = float(beat_interval)
        self.reply_mode = reply_mode
        self.workers = max(1, int(workers))
        # 每次 XREADGROUP 取多少条。真插件用的是 COUNT 1（实测），
        # 但那样 mock 本身就成了瓶颈（实测只能 ~475 条/秒，
        # 导致测出来的"响应时间"其实是在排队，不是链路 RTT）。
        # 默认取 100，让 mock 不拖后腿；要复刻真插件行为用 --read-count 1。
        self.read_count = max(1, int(read_count))
        self.quiet = quiet
        self.verbose = bool(verbose)      # True=每条都打印
        self.log_first = int(log_first)   # 前 N 条逐条打印
        self.log_every = int(log_every)   # 之后每 N 条打一行进度
        self.listener = listener      # 可选回调 listener(event_dict)
        self.max_messages = int(max_messages or 0)

        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.have_id = threading.Event()

        # 统计
        self.stat = {
            "online_sent": 0,
            "online_confirmed": 0,
            "assigned": 0,
            "datahub_beat": 0,
            "beats": 0,
            "recv": 0,
            "reply_ok": 0,
            "reply_fail": 0,
            "ack_ok": 0,
            "ack_fail": 0,
            "bytes_in": 0,
            "bytes_out": 0,
            "last_error": "",
            "start": time.time(),
        }
        self._by_type = {}
        self._lat = []                 # 每条报文的处理耗时（毫秒）
        self._threads = []
        # 每个消费线程一个连接（XREADGROUP 是阻塞命令，不能共用连接）
        self._sub_conn = None
        self._pub_lock = threading.Lock()
        self._pub_conn = None

    # ------------------------------------------------------------ 工具
    def _emit(self, kind, **kw):
        if self.listener:
            try:
                self.listener(dict(kind=kind, t=time.time(), **kw))
            except Exception:
                pass

    def _pub(self):
        """常驻发布连接（订阅连接上不能 PUBLISH）。断线自动重连一次。"""
        with self._pub_lock:
            c = self._pub_conn
            if c is not None and c.connected:
                return c
            for _ in range(2):
                try:
                    c = RespClient(self.kw["host"], self.kw["port"],
                                   self.kw["password"], self.kw["db"]).connect()
                    self._pub_conn = c
                    return c
                except Exception as e:
                    self.stat["last_error"] = "pub connect: %s" % e
                    time.sleep(0.5)
            raise RuntimeError("无法连接 Redis %s" % self.kw)

    def _publish(self, channel, payload):
        c = self._pub()
        n = c.publish(channel, payload)
        with self.lock:
            self.stat["bytes_out"] += len(payload.encode("utf-8"))
        return n

    def online_payload(self, sid):
        return json.dumps({
            "id": sid,
            "unique_string": self.unique,
            "ip": self.kw.get("ip", ""),
            "mac": self.kw.get("mac", ""),
            "usecount": self.usecount,
        }, ensure_ascii=False, separators=(",", ":"))

    # ------------------------------------------------------------ 线程
    def _online_loop(self):
        """上线：先 id=-1 抢编号；拿到编号后再发一次带 id 的（确认）。"""
        while not self.stop.is_set():
            if not self.have_id.is_set():
                try:
                    payload = self.online_payload(-1)
                    self._publish(self.ch_online, payload)
                    with self.lock:
                        self.stat["online_sent"] += 1
                    self._emit("online", id=-1, payload=payload)
                    log("→ 上线 strategyserver_online id=-1 (第%d次)"
                        % self.stat["online_sent"], self.quiet)
                except Exception as e:
                    self.stat["last_error"] = "online: %s" % e
                    log("上线失败: %s" % e, self.quiet, force=True)
            else:
                # 确认上线（实测插件拿到编号后会再发一次带真实 id 的）
                try:
                    payload = self.online_payload(self.assign_id)
                    self._publish(self.ch_online, payload)
                    with self.lock:
                        self.stat["online_confirmed"] += 1
                    self._emit("online_confirm", id=self.assign_id, payload=payload)
                    log("→ 上线确认 strategyserver_online id=%d" % self.assign_id,
                        self.quiet)
                except Exception as e:
                    log("上线确认失败: %s" % e, self.quiet, force=True)
                return
            self.stop.wait(self.online_interval)

    def _beat_loop(self):
        """心跳：拿到编号后每 beat_interval 秒一次。"""
        while not self.stop.is_set():
            if self.have_id.is_set():
                try:
                    payload = self.online_payload(self.assign_id)
                    self._publish(self.ch_beat, payload)
                    with self.lock:
                        self.stat["beats"] += 1
                    self._emit("beat", id=self.assign_id, payload=payload)
                except Exception as e:
                    self.stat["last_error"] = "beat: %s" % e
            self.stop.wait(self.beat_interval)

    def _sub_loop(self):
        """订阅 unique_string：收数据中台分配的编号 + 数据中台心跳。"""
        while not self.stop.is_set():
            try:
                c = RespClient(self.kw["host"], self.kw["port"],
                               self.kw["password"], self.kw["db"]).connect()
                c.subscribe([self.unique])
                self._sub_conn = c
                log("已订阅 unique_string=%s" % self.unique, self.quiet, force=True)
                while not self.stop.is_set():
                    m = c.read_message(timeout=1.0)
                    if not m:
                        continue
                    kind, ch, payload = m
                    if kind not in ("message", "pmessage") or not payload:
                        continue
                    self._handle_sub(ch, payload)
            except Exception as e:
                if self.stop.is_set():
                    return
                self.stat["last_error"] = "sub: %s" % e
                log("订阅连接断开，2s 后重连: %s" % e, self.quiet, force=True)
                time.sleep(2)
            finally:
                try:
                    if self._sub_conn:
                        self._sub_conn.close()
                except Exception:
                    pass
                self._sub_conn = None

    def _handle_sub(self, chan, payload):
        try:
            d = json.loads(payload)
        except Exception:
            return
        if not isinstance(d, dict):
            return
        sid = d.get("id")

        # 关键：分配编号 和 中台心跳 长得几乎一样，都带 dataHubString ——
        #   分配编号 {"id":7,"dataHubString":"test"}
        #   中台心跳 {"id":7,"dataHubString":"test1"}
        # 实测真插件也不是靠 dataHubString 的内容区分的，而是靠"有没有拿到过编号"：
        # 还没编号时收到的第一条 id>=0 的消息就是分配编号，之后才是心跳。
        if not self.have_id.is_set():
            if isinstance(sid, int) and sid >= 0:
                self.assign_id = int(sid)
                with self.lock:
                    self.stat["assigned"] += 1
                self._emit("assigned", id=self.assign_id, payload=payload)
                log("★ 收到数据中台分配的编号 id=%s -> 下发流 %s"
                    % (self.assign_id, P.stream_for(self.assign_id)),
                    self.quiet, force=True)
                self.have_id.set()
                self._ensure_streams()
                return
            # sid == -1：别的策略平台在抢编号 / 中台还没决定，忽略
            return

        # 已有编号：剩下的都是中台心跳
        self.stat["datahub_beat"] += 1
        self._emit("datahub_beat", id=sid, payload=payload)
        log("← 数据中台心跳 %s" % payload, self.quiet)

    def _ensure_streams(self):
        """建 ST-<id> / ST-<id>-reply 两个流 + user_group（插件实测行为）。"""
        req = P.stream_for(self.assign_id)
        rep = P.reply_stream_for(self.assign_id)
        try:
            c = self._pub()
            for s in (req, rep):
                r = c.xgroup_create(s, P.GROUP, "0", mkstream=True)
                log("  XGROUP CREATE %s %s -> %s" % (s, P.GROUP, r), self.quiet)
        except Exception as e:
            log("建流失败: %s" % e, self.quiet, force=True)

    def _reply_targets(self):
        """回包写哪些流（见 protocol.py 的说明）。"""
        if self.reply_mode == P.REPLY_ST:
            return [P.reply_stream_for(self.assign_id)]
        if self.reply_mode == P.REPLY_BOTH:
            return [P.STREAM_REPLY, P.reply_stream_for(self.assign_id)]
        return [P.STREAM_REPLY]

    def _reply_loop(self, worker_id):
        """消费 ST-<id>，回包到 DataHub_reply_stream。"""
        # 等拿到编号
        while not self.stop.is_set() and not self.have_id.is_set():
            self.stop.wait(0.3)
        if self.stop.is_set():
            return

        req = P.stream_for(self.assign_id)
        rep = P.reply_stream_for(self.assign_id)
        consumer = "%s-w%d" % (req, worker_id)
        conn = None
        idle_logged = 0

        while not self.stop.is_set():
            try:
                if conn is None or not conn.connected:
                    conn = RespClient(self.kw["host"], self.kw["port"],
                                      self.kw["password"], self.kw["db"]).connect()
                    if worker_id == 0:
                        log("开始消费 %s / %s（consumer=%s）" % (req, rep, consumer),
                            self.quiet, force=True)
                items = conn.xreadgroup(P.GROUP, consumer, [req, rep],
                                        count=self.read_count, block=200)
                if not items:
                    continue
                # 攒一批一起处理：回包和 XACK 都用管道批量发，
                # 避免"每条报文 2 次网络往返"把 mock 变成瓶颈。
                batch = []
                for stream, entries in items:
                    for entry_id, fields in entries:
                        batch.append((stream, entry_id, fields))
                if batch:
                    self._process_batch(conn, batch)
            except RespError as e:
                self.stat["last_error"] = "xreadgroup: %s" % e
                # 组不存在等情况：补建一次
                try:
                    if conn:
                        for s in (req, rep):
                            conn.xgroup_create(s, P.GROUP, "0", mkstream=True)
                except Exception:
                    pass
                time.sleep(0.5)
            except Exception as e:
                if self.stop.is_set():
                    return
                self.stat["last_error"] = "reply loop: %s" % e
                log("消费连接异常，2s 后重连: %s" % e, self.quiet, force=True)
                try:
                    if conn:
                        conn.close()
                except Exception:
                    pass
                conn = None
                time.sleep(2)

    def _process_batch(self, conn, batch):
        """把一批报文一起处理：先批量 XADD 回包，再批量 XACK。

        为什么要批量：单条处理要 2 次网络往返（回包 XADD + XACK），
        8 线程压测时 mock 自己就先饱和了，测出来的"响应时间"其实是排队时间。
        """
        t0 = time.time()
        targets = self._reply_targets()

        # --- 1) 批量回包 ---
        if self.do_reply:
            arg_list = []
            for stream, entry_id, fields in batch:
                rid = fields.get("request_id", "")
                for tgt in targets:
                    arg_list.append(("XADD", tgt, "*", "request_id", rid,
                                     "task", self.reply_data))
            try:
                res = conn.pipeline([tuple(a) for a in arg_list])
                ok = sum(1 for r in res if not isinstance(r, RespError))
                with self.lock:
                    self.stat["reply_ok"] += ok
                    self.stat["reply_fail"] += len(res) - ok
                    self.stat["bytes_out"] += (
                        len(self.reply_data.encode("utf-8")) * ok)
            except Exception as e:
                with self.lock:
                    self.stat["reply_fail"] += len(arg_list)
                self.stat["last_error"] = "reply batch: %s" % e
                log("批量回包失败: %s" % e, self.quiet, force=True)

        # --- 2) 批量 XACK ---
        try:
            ack_args = [("XACK", s, P.GROUP, eid) for s, eid, _ in batch]
            res = conn.pipeline(ack_args)
            ok = sum(1 for r in res if not isinstance(r, RespError))
            with self.lock:
                self.stat["ack_ok"] += ok
                self.stat["ack_fail"] += len(res) - ok
        except Exception as e:
            with self.lock:
                self.stat["ack_fail"] += len(batch)
            self.stat["last_error"] = "ack batch: %s" % e

        # --- 3) 记账 + 打印 ---
        n = len(batch)
        for stream, entry_id, fields in batch:
            self._count_one(stream, entry_id, fields)

        # 每条的"处理耗时"按批次均摊（批量处理的固有代价）
        per = (time.time() - t0) * 1000.0 / max(1, n)
        with self.lock:
            self._lat.extend([per] * n)
            if len(self._lat) > 20000:
                del self._lat[:10000]

        if self.max_messages and self.stat["recv"] >= self.max_messages:
            log("已达 --max-messages=%d，停止消费" % self.max_messages,
                self.quiet, force=True)
            self.stop.set()

    def _count_one(self, stream, entry_id, fields):
        """统计 + 日志（单条）。

        日志策略：前 log_first 条逐条打印（方便看报文结构），
        之后每收满 log_every 条打一行进度，避免压测时刷屏。
        要全部打印用 --verbose。
        """
        request_id = fields.get("request_id", "")
        task = fields.get("task", "")
        with self.lock:
            self.stat["recv"] += 1
            n = self.stat["recv"]
            self.stat["bytes_in"] += len(str(task).encode("utf-8"))
        self._emit("recv", stream=stream, entry_id=entry_id,
                   request_id=request_id, task=task)

        mt = None
        brief = ""
        try:
            d = json.loads(task)
            if isinstance(d, dict):
                mt = d.get("MsgType")
                brief = "MsgType=%s %s" % (mt, P.MSG_TYPE_NAMES.get(mt, ""))
        except Exception:
            brief = "非JSON(解析失败)"
        with self.lock:
            self._by_type[mt] = self._by_type.get(mt, 0) + 1

        if self.verbose:
            show = True
        elif n <= self.log_first:
            show = True
        else:
            show = (self.log_every > 0 and n % self.log_every == 0)
        if not show:
            return
        if self.verbose or n <= self.log_first:
            log("← [%s] #%s request_id=%s  %s  task=%s"
                % (stream, entry_id, request_id, brief,
                   (task[:300] + "...") if len(task) > 300 else task),
                self.quiet)
        else:
            log("← 已收 %d 条（最近 #%s %s）" % (n, entry_id, brief), self.quiet)

    # 兼容：单条处理（保留给外部直接调用）
    def _on_message(self, conn, stream, entry_id, fields):
        self._process_batch(conn, [(stream, entry_id, fields)])

    # ------------------------------------------------------------ 生命周期
    def start(self):
        # 先连一下，地址不对要立刻报错，别等半天
        c = self._pub()
        c.ping()
        log("已连接 Redis %s:%s db%s" % (self.kw["host"], self.kw["port"], self.kw["db"]),
            self.quiet, force=True)

        if self.do_assign:
            # 自己扮演数据中台应答：直接把编号给自己（无需人工介入）
            self._ensure_streams()
            self.have_id.set()
            log("自应答模式：直接使用分配编号 id=%d -> 下发流 %s"
                % (self.assign_id, P.stream_for(self.assign_id)), self.quiet, force=True)

        self._threads = [
            threading.Thread(target=self._sub_loop, name="sub", daemon=True),
            threading.Thread(target=self._online_loop, name="online", daemon=True),
            threading.Thread(target=self._beat_loop, name="beat", daemon=True),
        ]
        for i in range(self.workers):
            self._threads.append(
                threading.Thread(target=self._reply_loop, args=(i,),
                                 name="reply%d" % i, daemon=True))
        for t in self._threads:
            t.start()

    def run_forever(self, seconds=0):
        end = (time.time() + seconds) if seconds else None
        try:
            while not self.stop.is_set():
                if end and time.time() >= end:
                    break
                time.sleep(0.3)
        except KeyboardInterrupt:
            log("收到 Ctrl+C", self.quiet, force=True)
        finally:
            self.shutdown()

    def shutdown(self):
        self.stop.set()
        time.sleep(0.4)
        # 下线报文（实测：PUBLISH strategyserver_offline {"id":N}）
        try:
            payload = json.dumps({"id": self.assign_id}, separators=(",", ":"))
            self._publish(self.ch_offline, payload)
            self._emit("offline", id=self.assign_id, payload=payload)
            log("→ 下线 strategyserver_offline %s" % payload, self.quiet, force=True)
        except Exception:
            pass
        for c in (self._pub_conn, self._sub_conn):
            try:
                if c:
                    c.close()
            except Exception:
                pass

    def report(self):
        with self.lock:
            s = dict(self.stat)
            by = dict(self._by_type)
            lat = list(self._lat)
        dur = max(0.001, time.time() - s["start"])
        print()
        print("=" * 66)
        print("策略平台 Mock 统计（运行 %.1fs）" % dur)
        print("=" * 66)
        print("  unique_string      : %s" % self.unique)
        print("  分配编号 / 下发流  : %s / %s" % (self.assign_id, P.stream_for(self.assign_id)))
        print("  上线报文(id=-1)    : %d 次" % s["online_sent"])
        print("  上线确认(id=%s)   : %d 次" % (self.assign_id, s["online_confirmed"]))
        print("  收到编号次数       : %d" % s["assigned"])
        print("  收到中台心跳       : %d" % s["datahub_beat"])
        print("  发出心跳           : %d 次" % s["beats"])
        print("  收到业务报文       : %d 条" % s["recv"])
        if s["recv"]:
            print("    └ 收包流量       : %.2f MB (%.0f B/条)"
                  % (s["bytes_in"] / 1048576.0, s["bytes_in"] / float(s["recv"])))
        print("  回复 DataHub_reply : 成功 %d / 失败 %d"
              % (s["reply_ok"], s["reply_fail"]))
        print("  XACK               : 成功 %d / 失败 %d" % (s["ack_ok"], s["ack_fail"]))
        if by:
            print("  按 MsgType 分布    :")
            for k in sorted(by, key=lambda x: (x is None, x)):
                name = P.MSG_TYPE_NAMES.get(k, "未知")
                print("      MsgType=%-4s %-22s %d" % (k, name, by[k]))
        if lat:
            lat.sort()
            n = len(lat)
            print("  处理耗时(ms)       : 平均 %.3f  中位 %.3f  P95 %.3f  P99 %.3f  最大 %.3f"
                  % (sum(lat) / n, lat[n // 2], lat[int(n * 0.95)], lat[int(n * 0.99)], lat[-1]))
            if s["recv"] and dur > 0:
                print("  吞吐               : %.1f 条/秒" % (s["recv"] / dur))
        if s["last_error"]:
            print("  最近错误           : %s" % s["last_error"])
        print("=" * 66)
        return s


# ================================================================ CLI
def build_parser():
    cp = cfgmod.load()
    ap = argparse.ArgumentParser(
        description="模拟策略平台（收 ST-<id> 报文，回 DataHub_reply_stream）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("--host", default=None, help="Redis 主机")
    ap.add_argument("--port", type=int, default=None, help="Redis 端口")
    ap.add_argument("--pwd", default=None, help="Redis 密码")
    ap.add_argument("--db", type=int, default=None, help="Redis 库号")

    ap.add_argument("--unique", default="",
                    help="完整 unique_string；默认按 ST-<rand>-<mac><name> 拼")
    ap.add_argument("--unique-rand", default=None, help="unique_string 里的随机段")
    ap.add_argument("--unique-name", default=None, help="unique_string 里的名字段")
    ap.add_argument("--mac", default=None, help="上线报文里的 mac")
    ap.add_argument("--ip", default=None, help="上线报文里的 ip")
    ap.add_argument("--usecount", type=int, default=None, help="条件单用户数")
    ap.add_argument("--assign-id", type=int, default=None,
                    help="数据中台分配的编号（决定下发流 ST-<id>），默认取配置")
    ap.add_argument("--auto-assign-id", action="store_true",
                    help="自动从 --assign-id 起找第一个未被占用的 ST-<n> 流（多开时避免撞车）")
    ap.add_argument("--no-assign", action="store_true",
                    help="不自己应答上线，等真数据中台分配编号")
    ap.add_argument("--no-reply", action="store_true",
                    help="只收不回（观察真数据中台到底发什么）")
    ap.add_argument("--reply-data", default=None, help='回包内容，默认 {"status":"OK"}')
    ap.add_argument("--reply-stream", choices=[P.REPLY_AUTO, P.REPLY_ST, P.REPLY_BOTH],
                    default=P.REPLY_AUTO,
                    help="回包写到哪：auto=DataHub_reply_stream(实测真插件行为), "
                         "reply=ST-<id>-reply, both=两边都写")
    ap.add_argument("--online-interval", type=float, default=None, help="上线重发间隔秒")
    ap.add_argument("--beat-interval", type=float, default=None, help="心跳间隔秒")
    ap.add_argument("--workers", type=int, default=None, help="消费线程数")
    ap.add_argument("--read-count", type=int, default=100,
                    help="每次 XREADGROUP 取多少条（真插件是 1；1 会让 mock 成为瓶颈，"
                         "默认 100 以便压测；想复刻真插件行为用 --read-count 1）")
    ap.add_argument("--channel-suffix", default="",
                    help="频道后缀：现场存在不带后缀(插件用)与 _1(真中台用)两套，"
                         "默认空；要匹配现场真中台用 --channel-suffix _1")
    ap.add_argument("--seconds", type=float, default=0, help="运行秒数，0=一直跑")
    ap.add_argument("--max-messages", type=int, default=0, help="收到多少条后停，0=不限")
    ap.add_argument("--quiet", action="store_true", help="安静模式")
    ap.add_argument("--verbose", action="store_true",
                    help="逐条打印收到的报文（压测时别开，会刷屏）")
    ap.add_argument("--no-report", action="store_true", help="退出时不打印统计")
    return ap, cp


def main():
    ap, cp = build_parser()
    a = ap.parse_args()

    kw = cfgmod.redis_kwargs(cp, a)
    S = cfgmod.DEFAULTS["strategy"]

    def g(attr, key):
        v = getattr(a, attr, None)
        return v if v not in (None, "") else cfgmod.get(cp, "strategy", key)

    unique = a.unique
    if not unique:
        unique = P.unique_string(g("unique_rand", "unique_rand"),
                                 g("mac", "mac"),
                                 g("unique_name", "unique_name"))
    kw["ip"] = g("ip", "ip")
    kw["mac"] = g("mac", "mac")

    assign_id = a.assign_id if a.assign_id is not None else int(g("assign_id", "assign_id"))

    # 自动挑一个没被占用的编号，方便同时开多个 mock
    if a.auto_assign_id:
        try:
            c = RespClient(kw["host"], kw["port"], kw["password"], kw["db"]).connect()
            n = assign_id
            while c.cmd("EXISTS", P.stream_for(n)):
                log("ST-%d 已存在，试下一个编号" % n, a.quiet)
                n += 1
            assign_id = n
            log("自动选用编号 %d" % assign_id, a.quiet, force=True)
            c.close()
        except Exception as e:
            log("自动选号失败，沿用 %d: %s" % (assign_id, e), a.quiet, force=True)

    mock = MockStrategy(
        kw=kw, unique=unique, assign_id=assign_id,
        usecount=int(g("usecount", "usecount")),
        reply_data=(a.reply_data or cfgmod.get(cp, "strategy", "reply_data")),
        do_reply=not a.no_reply,
        do_assign=not a.no_assign,
        online_interval=float(g("online_interval", "online_interval")),
        beat_interval=float(g("beat_interval", "beat_interval")),
        reply_mode=a.reply_stream,
        read_count=a.read_count,
        channel_suffix=a.channel_suffix,
        workers=(a.workers if a.workers is not None
                 else int(cfgmod.get(cp, "test", "workers"))),
        quiet=a.quiet,
        verbose=a.verbose,
        max_messages=a.max_messages,
    )

    log("策略平台 Mock 启动 → %s:%s db%s" % (kw["host"], kw["port"], kw["db"]),
        a.quiet, force=True)
    log("  unique_string = %s" % unique, a.quiet, force=True)
    log("  分配编号      = %s  →  下发流 %s"
        % (assign_id, P.stream_for(assign_id)), a.quiet, force=True)
    log("  回包目标      = %s" % "+".join(mock._reply_targets()), a.quiet, force=True)
    log("  上线/心跳频道 = %s / %s" % (mock.ch_online, mock.ch_beat),
        a.quiet, force=True)
    if a.no_assign:
        log("  模式          = 等待真数据中台分配编号", a.quiet, force=True)
    if a.no_reply:
        log("  模式          = 只收不回（观察）", a.quiet, force=True)

    try:
        mock.start()
        mock.run_forever(a.seconds)
    except Exception as e:
        log("运行异常: %s" % e, a.quiet, force=True)
        mock.stop.set()
    if not a.no_report:
        mock.report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
