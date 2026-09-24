# -*- coding: utf-8 -*-
"""
mock_datahub.py —— 模拟数据中台（策略平台方向）
==============================================
datahub_test/mock_datahub.py 是"数据中台应答委托服务器(WT)"，
本脚本是它的对偶：应答【策略平台(ST)】方向。

它扮演数据中台，对策略平台做这几件事：
  1. 订阅 strategyserver_online，收到 {"id":-1,"unique_string":"ST-..."} 后
     往 unique_string 回 {"id":<alloc>,"dataHubString":"test"}  —— 分配编号
  2. 给分配出去的每个策略平台建流 ST-<id> / ST-<id>-reply（+ user_group）
  3. 周期发心跳到各 unique_string：{"id":N,"dataHubString":"test1"}
  4. 可选：持续往 ST-<id> XADD 业务报文（让 mock 策略平台有事可做）
  5. 订阅 strategyserver_beat 更新活跃时间；订阅 strategyserver_offline 回收编号
  6. 打印在线策略平台表（编号 / unique_string / 最后心跳 / 收到多少心跳）

两种典型用法：
  A) 联调 mock 策略平台（不需要真 DataHub）：
       # 终端1
       python mock_datahub.py --workers 0
       # 终端2
       python mock_strategy.py --no-assign
     这样 mock 策略平台会真的走"等数据中台分配编号"这条路径。

  B) 只做观察者（真数据中台在跑，你想看它给策略平台分了几号）：
       python mock_datahub.py --observe-only

  C) 想造业务流量：
       python mock_datahub.py --push --push-interface create --push-interval 0.5
"""
import argparse
import json
import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cases as C
import config as cfgmod
import protocol as P
from resp_min import RespClient, RespError


def ts():
    return time.strftime("%H:%M:%S")


def log(msg, quiet=False, force=False):
    if quiet and not force:
        return
    print("[%s] %s" % (ts(), msg), flush=True)


class MockDataHub(object):
    """模拟数据中台（策略方向）。"""

    def __init__(self, kw, alloc_start=1, usecount_aware=True, quiet=False,
                 beat_interval=10.0, observe_only=False,
                 push=False, push_interface="create", push_rate=0.0,
                 push_interval=1.0, push_max=0, push_destroy=False,
                 online_channels=None, channel_suffix=""):
        self.kw = kw
        self.alloc_start = int(alloc_start)
        self.quiet = quiet
        self.beat_interval = float(beat_interval)
        self.observe_only = observe_only
        self.push = push
        self.push_interface = push_interface
        self.push_rate = float(push_rate or 0)
        self.push_interval = float(push_interval or 0)
        self.push_max = int(push_max or 0)
        self.push_destroy = bool(push_destroy)
        self.channel_suffix = channel_suffix or ""
        self.ch_online = P.chan(P.CH_STRATEGY_ONLINE, self.channel_suffix)
        self.ch_beat = P.chan(P.CH_STRATEGY_BEAT, self.channel_suffix)
        self.ch_offline = P.chan(P.CH_STRATEGY_OFFLINE, self.channel_suffix)
        if online_channels:
            self.online_channels = list(online_channels)
        else:
            # 默认同时听不带后缀与带 _1 两套，哪种命中了都能应答
            self.online_channels = sorted({self.ch_online,
                                           P.chan(P.CH_STRATEGY_ONLINE, P.CH_SUFFIX_1)})

        self.stop = threading.Event()
        self.lock = threading.Lock()
        # unique_string -> {id, ip, mac, usecount, last_beat, beats, online_at}
        self.servers = {}
        self.next_id = self.alloc_start
        self._used_ids = set()
        self.stat = {
            "online_recv": 0, "online_id_m1": 0, "online_confirmed": 0,
            "assigned": 0, "beats_recv": 0, "offline_recv": 0,
            "push_sent": 0, "push_bytes": 0,
            "last_error": "", "start": time.time(),
        }
        self._pub_conn = None
        self._pub_lock = threading.Lock()
        self._threads = []

    # ------------------------------------------------------------ 连接
    def _pub(self):
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
            raise RuntimeError("无法连接 Redis %s:%s" % (self.kw["host"], self.kw["port"]))

    def _alloc(self):
        """分配一个没被占用的编号。"""
        with self.lock:
            while self.next_id in self._used_ids:
                self.next_id += 1
            sid = self.next_id
            self._used_ids.add(sid)
            self.next_id += 1
            return sid

    # ------------------------------------------------------------ 订阅
    def _listen_loop(self, channels):
        """一个连接订阅一批频道，分发给处理函数。"""
        c = None
        while not self.stop.is_set():
            try:
                if c is None or not c.connected:
                    c = RespClient(self.kw["host"], self.kw["port"],
                                   self.kw["password"], self.kw["db"]).connect()
                    c.subscribe(channels)
                    log("已订阅 %s" % channels, self.quiet, force=True)
                m = c.read_message(timeout=1.0)
                if not m:
                    continue
                kind, ch, payload = m
                if kind not in ("message", "pmessage") or not payload:
                    continue
                self._dispatch(ch, payload)
            except Exception as e:
                if self.stop.is_set():
                    return
                self.stat["last_error"] = "listen: %s" % e
                log("订阅连接异常，2s 后重连: %s" % e, self.quiet, force=True)
                try:
                    if c:
                        c.close()
                except Exception:
                    pass
                c = None
                time.sleep(2)

    def _dispatch(self, chan, payload):
        # 避免重复：online 频道匹配了 _1 后缀，只处理精确名
        if chan in self.online_channels:
            self._on_online(payload)
        elif chan.startswith(P.CH_STRATEGY_BEAT):
            self._on_beat(payload)
        elif chan.startswith(P.CH_STRATEGY_OFFLINE):
            self._on_offline(payload)

    def _on_online(self, payload):
        try:
            d = json.loads(payload)
        except Exception:
            return
        if not isinstance(d, dict):
            return
        with self.lock:
            self.stat["online_recv"] += 1
        uniq = d.get("unique_string", "")
        sid = d.get("id")
        if not uniq:
            return

        if self.observe_only:
            log("← 上线(观察) %s id=%s usecount=%s"
                % (uniq, sid, d.get("usecount")), self.quiet)
            return

        if sid == -1:
            with self.lock:
                self.stat["online_id_m1"] += 1
            # 分配编号
            with self.lock:
                known = self.servers.get(uniq)
            if known:
                assign = known["id"]        # 已分配过，复用（插件会重发上线）
            else:
                assign = self._alloc()
                with self.lock:
                    self.servers[uniq] = {
                        "id": assign, "ip": d.get("ip", ""), "mac": d.get("mac", ""),
                        "usecount": d.get("usecount", 0),
                        "online_at": time.time(), "last_beat": 0.0, "beats": 0,
                    }
                with self.lock:
                    self.stat["assigned"] += 1
                self._ensure_streams(assign)
                log("★ 新策略平台上线 %s → 分配编号 %d（流 %s）usecount=%s"
                    % (uniq, assign, P.stream_for(assign), d.get("usecount")),
                    self.quiet, force=True)
            reply = json.dumps({"id": assign, "dataHubString": "test"},
                               separators=(",", ":"))
            try:
                self._pub().publish(uniq, reply)
                log("→ 分配编号 %d 到 %s : %s" % (assign, uniq, reply), self.quiet)
            except Exception as e:
                self.stat["last_error"] = "publish assign: %s" % e
                log("分配编号失败: %s" % e, self.quiet, force=True)
        else:
            # 带真实 id 的上线 = 上线确认，更新活跃时间
            with self.lock:
                self.stat["online_confirmed"] += 1
                if uniq in self.servers:
                    self.servers[uniq]["last_beat"] = time.time()
            log("← 上线确认 %s id=%s" % (uniq, sid), self.quiet)

    def _on_beat(self, payload):
        try:
            d = json.loads(payload)
        except Exception:
            return
        uniq = d.get("unique_string", "")
        with self.lock:
            self.stat["beats_recv"] += 1
            if uniq in self.servers:
                self.servers[uniq]["last_beat"] = time.time()
                self.servers[uniq]["beats"] += 1
                self.servers[uniq]["usecount"] = d.get("usecount",
                                                       self.servers[uniq]["usecount"])
            else:
                # 没见过的 unique_string 直接发心跳：也收进来
                if uniq:
                    sid = d.get("id")
                    self.servers[uniq] = {
                        "id": sid if isinstance(sid, int) else -1,
                        "ip": d.get("ip", ""), "mac": d.get("mac", ""),
                        "usecount": d.get("usecount", 0),
                        "online_at": time.time(), "last_beat": time.time(), "beats": 1,
                    }

    def _on_offline(self, payload):
        try:
            d = json.loads(payload)
        except Exception:
            return
        sid = d.get("id")
        with self.lock:
            self.stat["offline_recv"] += 1
            gone = [u for u, v in self.servers.items() if v["id"] == sid]
            for u in gone:
                self.servers.pop(u, None)
            if isinstance(sid, int):
                self._used_ids.discard(sid)
        if gone:
            log("← 策略平台下线 id=%s（%s）" % (sid, ",".join(gone)), self.quiet)

    def _ensure_streams(self, sid):
        try:
            c = self._pub()
            for s in (P.stream_for(sid), P.reply_stream_for(sid)):
                r = c.xgroup_create(s, P.GROUP, "0", mkstream=True)
                log("  XGROUP CREATE %s %s -> %s" % (s, P.GROUP, r), self.quiet)
        except Exception as e:
            log("建流失败: %s" % e, self.quiet, force=True)

    # ------------------------------------------------------------ 心跳
    def _beat_loop(self):
        """数据中台 → 策略平台 的心跳：PUBLISH <unique_string> {...}"""
        while not self.stop.is_set():
            if not self.observe_only:
                with self.lock:
                    targets = [(u, dict(v)) for u, v in self.servers.items()]
                for u, v in targets:
                    sid = v.get("id")
                    if not isinstance(sid, int) or sid < 0:
                        continue
                    # 中台心跳带该策略平台自己的编号（与真中台一致）
                    payload = json.dumps({"id": sid, "dataHubString": "test1"},
                                         separators=(",", ":"))
                    try:
                        self._pub().publish(u, payload)
                    except Exception as e:
                        self.stat["last_error"] = "beat: %s" % e
            self.stop.wait(self.beat_interval)

    # ------------------------------------------------------------ 推报文
    def _push_loop(self):
        """往 ST-<id> 持续 XADD 业务报文（--push 才跑）。"""
        if not self.push:
            return
        # 等第一个策略平台上线
        while not self.stop.is_set():
            with self.lock:
                if self.servers:
                    break
            self.stop.wait(0.5)
        if self.stop.is_set():
            return

        type_tag = "destroy" if self.push_destroy else "normal"
        pool = C.filter_cases(type_tag=type_tag,
                              interface=(self.push_interface or "all"))
        if not pool:
            log("推送：没有可用用例（interface=%s type=%s）"
                % (self.push_interface, type_tag), self.quiet, force=True)
            return
        log("推送线程启动：%s / %s，共 %d 种用例，interval=%.3fs rate=%.1f"
            % (self.push_interface, type_tag, len(pool),
               self.push_interval, self.push_rate), self.quiet, force=True)

        n = 0
        next_t = time.time()
        while not self.stop.is_set():
            if self.push_max and n >= self.push_max:
                log("推送达到 --push-max=%d，停止" % self.push_max,
                    self.quiet, force=True)
                return
            with self.lock:
                sids = [v["id"] for v in self.servers.values()
                        if isinstance(v["id"], int) and v["id"] >= 0]
            if not sids:
                self.stop.wait(0.5)
                continue
            sid = random.choice(sids)
            no, iface, desc, fn = pool[n % len(pool)]
            try:
                payload = C.payload_text(fn())
                rid = "datahub_%d_%d" % (int(time.time()), n)
                self._pub().cmd("XADD", P.stream_for(sid), "*",
                                "request_id", rid, "task", payload)
                with self.lock:
                    self.stat["push_sent"] += 1
                    self.stat["push_bytes"] += len(payload.encode("utf-8"))
                if not self.quiet and n < 50:
                    log("→ 推送 ST-%s #%s %s rid=%s len=%d"
                        % (sid, no, desc, rid, len(payload.encode("utf-8"))),
                        self.quiet)
                n += 1
            except Exception as e:
                self.stat["last_error"] = "push: %s" % e
                log("推送失败: %s" % e, self.quiet, force=True)
                time.sleep(0.5)
            # 限速
            if self.push_rate > 0:
                next_t += 1.0 / self.push_rate
                d = next_t - time.time()
                if d > 0:
                    self.stop.wait(d)
            elif self.push_interval > 0:
                self.stop.wait(self.push_interval)

    # ------------------------------------------------------------ 生命周期
    def start(self):
        self._pub().ping()
        log("数据中台 Mock 已连接 %s:%s db%s"
            % (self.kw["host"], self.kw["port"], self.kw["db"]), self.quiet, force=True)
        if self.observe_only:
            log("模式: 仅观察（不分配编号、不发心跳）", self.quiet, force=True)

        # 每个频道一个订阅连接（SUBSCRIBE 一个连接只能订一批，且要独立线程读）
        self._threads = [
            threading.Thread(target=self._listen_loop,
                             args=(self.online_channels,), daemon=True),
            threading.Thread(target=self._listen_loop,
                             args=([self.ch_beat,
                                    P.chan(P.CH_STRATEGY_BEAT, P.CH_SUFFIX_1)],),
                             daemon=True),
            threading.Thread(target=self._listen_loop,
                             args=([self.ch_offline,
                                    P.chan(P.CH_STRATEGY_OFFLINE, P.CH_SUFFIX_1)],),
                             daemon=True),
            threading.Thread(target=self._beat_loop, daemon=True),
        ]
        if self.push:
            self._threads.append(threading.Thread(target=self._push_loop, daemon=True))
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
        time.sleep(0.3)
        try:
            if self._pub_conn:
                self._pub_conn.close()
        except Exception:
            pass

    def report(self):
        with self.lock:
            s = dict(self.stat)
            servers = {k: dict(v) for k, v in self.servers.items()}
        dur = max(0.001, time.time() - s["start"])
        print()
        print("=" * 74)
        print("数据中台 Mock 统计（运行 %.1fs）" % dur)
        print("=" * 74)
        print("  收到上线报文      : %d （其中 id=-1 抢编号 %d 次，上线确认 %d 次）"
              % (s["online_recv"], s["online_id_m1"], s["online_confirmed"]))
        print("  分配编号次数      : %d" % s["assigned"])
        print("  收到心跳          : %d" % s["beats_recv"])
        print("  收到下线          : %d" % s["offline_recv"])
        if self.push:
            print("  推送报文          : %d 条 (%.2f MB)"
                  % (s["push_sent"], s["push_bytes"] / 1048576.0))
        if servers:
            print("-" * 74)
            print("  在线策略平台 (%d):" % len(servers))
            print("    %-8s %-34s %-10s %8s %8s  %s"
                  % ("编号", "unique_string", "usecount", "心跳数", "空闲s", "IP/MAC"))
            now = time.time()
            for u, v in sorted(servers.items(), key=lambda x: x[1]["id"]):
                idle = (now - v["last_beat"]) if v["last_beat"] else -1
                print("    ST-%-5s %-34s %-10s %8d %8s  %s"
                      % (v["id"], u[:34],
                         v.get("usecount", ""), v.get("beats", 0),
                         ("%.0f" % idle) if idle >= 0 else "从未",
                         ("%s/%s" % (v.get("ip", ""), v.get("mac", "")))[:40]))
        if s["last_error"]:
            print("  最近错误          : %s" % s["last_error"])
        print("=" * 74)
        return s


# ================================================================ CLI
def main():
    cp = cfgmod.load()
    ap = argparse.ArgumentParser(
        description="模拟数据中台（策略平台方向）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--pwd", default=None)
    ap.add_argument("--db", type=int, default=None)

    ap.add_argument("--alloc-start", type=int, default=None,
                    help="分配编号的起始值，默认取配置 assign_id")
    ap.add_argument("--beat-interval", type=float, default=10.0,
                    help="给策略平台发心跳的间隔（秒）")
    ap.add_argument("--observe-only", action="store_true",
                    help="只观察，不分配编号/不发心跳（真中台在跑时用）")
    ap.add_argument("--channel-suffix", default="",
                    help="频道后缀：现场存在不带后缀(插件用)与 _1(真中台用)两套，"
                         "默认空且会同时监听两套；只想听一套就用这个指定")
    ap.add_argument("--push", action="store_true",
                    help="持续往 ST-<id> 推业务报文")
    ap.add_argument("--push-interface", default="create",
                    help="推送的接口名（create/modify/.../all）")
    ap.add_argument("--push-rate", type=float, default=0.0, help="推送限速 条/秒")
    ap.add_argument("--push-interval", type=float, default=1.0, help="推送间隔秒")
    ap.add_argument("--push-max", type=int, default=0, help="最多推多少条，0=不限")
    ap.add_argument("--push-destroy", action="store_true", help="推送破坏用例")
    ap.add_argument("--seconds", type=float, default=0, help="运行秒数，0=一直跑")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-report", action="store_true")
    a = ap.parse_args()

    kw = cfgmod.redis_kwargs(cp, a)
    alloc = a.alloc_start if a.alloc_start is not None else int(
        cfgmod.get(cp, "strategy", "assign_id"))

    mock = MockDataHub(
        kw=kw, alloc_start=alloc, quiet=a.quiet,
        beat_interval=a.beat_interval, observe_only=a.observe_only,
        push=a.push, push_interface=a.push_interface, push_rate=a.push_rate,
        push_interval=a.push_interval, push_max=a.push_max,
        push_destroy=a.push_destroy, channel_suffix=a.channel_suffix)

    log("数据中台 Mock 启动 → %s:%s db%s（编号起始 %d）"
        % (kw["host"], kw["port"], kw["db"], alloc), a.quiet, force=True)
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
