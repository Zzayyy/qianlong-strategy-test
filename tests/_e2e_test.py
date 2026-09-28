# -*- coding: utf-8 -*-
"""
_e2e_test.py —— 端到端联调自测（Windows 侧，直连 137:6379 db0）
=============================================================
同时拉起三个组件，验证 protocol.py 里记录的时序是否真的闭环：
    [mock_datahub]  扮演数据中台：应答上线、分配编号、建流、发心跳
    [mock_strategy] 扮演策略平台：抢编号、消费 ST-N、回 DataHub_reply_stream
    [send_test]     手动 XADD 几条报文进 ST-N

判定标准：
    1. mock_strategy 是否拿到编号
    2. 流 ST-N 是否被创建
    3. send_test 发出的报文是否被消费
    4. 回包是否落到 DataHub_reply_stream，且 request_id 对得上
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = ROOT   # 本脚本在 tests/ 下，项目根是上一层
PY = sys.executable
HOST, DB = "192.168.1.137", 0
ASSIGN = 91      # 用高位号段：137 上 ST-0/ST-1 可能被真实策略平台占用

sys.path.insert(0, HERE)
from resp_min import RespClient
import protocol as P


def ts():
    return time.strftime("%H:%M:%S")


def say(m):
    print("[%s] %s" % (ts(), m), flush=True)


def spawn(args, tag):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen([PY, "-u"] + args, cwd=HERE, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace")
    return p, tag


def drain(p, tag, sink, limit=200):
    """非阻塞读一行（后台线程用）。"""
    for line in p.stdout:
        sink.append("[%s] %s" % (tag, line.rstrip()))
        if len(sink) > limit:
            del sink[:limit // 2]


import threading


def main():
    kw = dict(host=HOST, port=6379, password="QianLong@2026&", db=DB)
    c = RespClient(**kw).connect()
    say("已连接 %s db%s" % (HOST, DB))
    say("PING=%s  初始 db 键数=%s" % (c.ping(), c.dbsize()))

    stream = P.stream_for(ASSIGN)
    # 清理上次的测试残留（只清 ST-<assign> / ST-<assign>-reply / 测试请求）
    before_reply = c.xlen(P.STREAM_REPLY)
    say("清理前: %s XLEN=%s  %s XLEN=%s  DataHub_reply_stream XLEN=%s"
        % (stream, c.xlen(stream) if c.cmd("EXISTS", stream) else "-",
           stream + "-reply", c.xlen(stream + "-reply")
           if c.cmd("EXISTS", stream + "-reply") else "-",
           before_reply))

    logs = {"dh": [], "st": [], "send": []}

    say("启动 mock_datahub（分配编号起始 %d）" % ASSIGN)
    dh, t1 = spawn(["mock_datahub.py", "--db", str(DB), "--host", HOST,
                    "--alloc-start", str(ASSIGN), "--beat-interval", "5",
                    "--quiet", "--no-report"], "DH")
    threading.Thread(target=drain, args=(dh, "DH", logs["dh"]), daemon=True).start()
    time.sleep(2.0)

    say("启动 mock_strategy（--no-assign，等中台分配编号）")
    st, t2 = spawn(["mock_strategy.py", "--db", str(DB), "--host", HOST,
                    "--unique-rand", "999", "--unique-name", "e2e",
                    "--no-assign", "--seconds", "30", "--no-report"], "ST")
    threading.Thread(target=drain, args=(st, "ST", logs["st"]), daemon=True).start()
    time.sleep(8.0)

    # 中间检查
    say("--- 中间检查 ---")
    say("ST-%d 存在=%s XLEN=%s" % (ASSIGN, c.cmd("EXISTS", stream),
                                   c.xlen(stream) if c.cmd("EXISTS", stream) else "-"))
    say("消费组: %s" % c.xinfo_groups(stream) if c.cmd("EXISTS", stream) else "  (流不存在)")
    say("PUBSUB CHANNELS: %s" % c.pubsub_channels())

    say("运行 send_test：发 5 条 normal create")
    sd, t3 = spawn(["send_test.py", "--db", str(DB), "--host", HOST,
                    "--assign-id", str(ASSIGN), "--interface", "create",
                    "--type", "normal", "--max", "5", "--workers", "1",
                    "--wait", "6", "--label", "e2e"], "SEND")
    threading.Thread(target=drain, args=(sd, "SEND", logs["send"]), daemon=True).start()
    sd.wait(timeout=90)
    time.sleep(2)

    say("--- 结果核对 ---")
    after_stream = c.xlen(stream) if c.cmd("EXISTS", stream) else 0
    say("ST-%d XLEN = %s" % (ASSIGN, after_stream))
    say("DataHub_reply_stream XLEN: %s -> %s"
        % (before_reply, c.xlen(P.STREAM_REPLY)))
    say("ST-%d 消费组: %s" % (ASSIGN, c.xinfo_groups(stream)))
    for g in c.xinfo_groups(stream):
        say("   GROUP %s pending=%s last-delivered=%s"
            % (g.get("name"), g.get("pending"), g.get("last-delivered-id")))
        for cc in c.xinfo_consumers(stream, g.get("name")):
            say("     CONSUMER %s pending=%s idle=%sms"
                % (cc.get("name"), cc.get("pending"), cc.get("idle")))

    say("ST-%d 最近 5 条（我们发的）:" % ASSIGN)
    for eid, f in c.xrevrange(stream, 5):
        say("   #%s request_id=%s task=%s" % (eid, f.get("request_id"),
                                              str(f.get("task"))[:120]))
    say("DataHub_reply_stream 最近 5 条（策略平台回的）:")
    for eid, f in c.xrevrange(P.STREAM_REPLY, 5):
        say("   #%s request_id=%s task=%s" % (eid, f.get("request_id"),
                                              str(f.get("task"))[:120]))

    # 收尾
    say("停止子进程")
    for p in (sd, st, dh):
        try:
            p.terminate()
        except Exception:
            pass
    time.sleep(1.5)
    for p in (sd, st, dh):
        try:
            p.kill()
        except Exception:
            pass

    print()
    print("#" * 76)
    print("### mock_datahub 输出")
    print("#" * 76)
    for l in logs["dh"]:
        print(l)
    print()
    print("#" * 76)
    print("### mock_strategy 输出")
    print("#" * 76)
    for l in logs["st"]:
        print(l)
    print()
    print("#" * 76)
    print("### send_test 输出")
    print("#" * 76)
    for l in logs["send"][:120]:
        print(l)

    c.close()


if __name__ == "__main__":
    main()
