# -*- coding: utf-8 -*-
"""
_e2e_destroy.py —— 端到端：破坏用例 + 压测
流程同 _e2e_test.py，但先跑 normal 压测，再跑 destroy 破坏。
"""
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from resp_min import RespClient
import protocol as P

PY = sys.executable
HOST, DB, ASSIGN = "192.168.1.137", 0, 2


def ts():
    return time.strftime("%H:%M:%S")


def say(m):
    print("[%s] %s" % (ts(), m), flush=True)


def spawn(args, tag, sink):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen([PY, "-u"] + args, cwd=HERE, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace")
    threading.Thread(target=lambda: [sink.append("[%s] %s" % (tag, l.rstrip()))
                                     for l in p.stdout], daemon=True).start()
    return p


def main():
    kw = dict(host=HOST, port=6379, password="QianLong@2026&", db=DB)
    c = RespClient(**kw).connect()
    c.delete(P.stream_for(ASSIGN), P.reply_stream_for(ASSIGN))
    say("已清理 ST-%d / ST-%d-reply" % (ASSIGN, ASSIGN))
    before = c.xlen(P.STREAM_REPLY)
    say("DataHub_reply_stream 起始 XLEN=%s" % before)

    logs = {"dh": [], "st": [], "normal": [], "destroy": []}
    dh = spawn(["mock_datahub.py", "--db", str(DB), "--host", HOST,
                "--alloc-start", str(ASSIGN), "--beat-interval", "5",
                "--quiet", "--no-report"], "DH", logs["dh"])
    time.sleep(1.5)
    st = spawn(["mock_strategy.py", "--db", str(DB), "--host", HOST,
                "--assign-id", str(ASSIGN), "--unique-rand", "555",
                "--unique-name", "dst", "--workers", "4", "--seconds", "70",
                "--quiet", "--no-report"], "ST", logs["st"])
    time.sleep(5)

    stream = P.stream_for(ASSIGN)
    say("ST-%d 已建, XLEN=%s" % (ASSIGN, c.xlen(stream)))

    say("===== 阶段1：normal 压测 2000 条 / 8 线程 / 不限速 =====")
    p1 = spawn(["send_test.py", "--db", str(DB), "--host", HOST,
                "--assign-id", str(ASSIGN), "--interface", "create",
                "--type", "normal", "--max", "2000", "--workers", "8",
                "--wait", "10", "--label", "normal_2k"],
               "N", logs["normal"])
    p1.wait(timeout=180)
    time.sleep(2)
    say("normal 后 XLEN=%s  reply=%s" % (c.xlen(stream), c.xlen(P.STREAM_REPLY)))

    say("===== 阶段2：destroy 破坏用例 1500 条 / 8 线程 =====")
    p2 = spawn(["send_test.py", "--db", str(DB), "--host", HOST,
                "--assign-id", str(ASSIGN), "--type", "destroy",
                "--max", "1500", "--workers", "8", "--wait", "10",
                "--label", "destroy_1500"], "D", logs["destroy"])
    p2.wait(timeout=180)
    time.sleep(2)

    say("===== 结果 =====")
    say("ST-%d XLEN = %s" % (ASSIGN, c.xlen(stream)))
    say("DataHub_reply_stream XLEN = %s (增加 %s)"
        % (c.xlen(P.STREAM_REPLY), c.xlen(P.STREAM_REPLY) - before))
    for g in c.xinfo_groups(stream):
        say("  GROUP %s consumers=%s pending=%s entries-read=%s lag=%s"
            % (g.get("name"), g.get("consumers"), g.get("pending"),
               g.get("entries-read"), g.get("lag")))

    for p in (p2, p1, st, dh):
        try:
            p.terminate()
        except Exception:
            pass
    time.sleep(1.5)
    for p in (p2, p1, st, dh):
        try:
            p.kill()
        except Exception:
            pass

    for key in ("normal", "destroy"):
        print()
        print("#" * 74)
        print("### send_test %s 输出（尾部）" % key)
        print("#" * 74)
        for l in logs[key][-60:]:
            print(l)
    print()
    print("#" * 74)
    print("### mock_strategy 统计（尾部）")
    print("#" * 74)
    for l in logs["st"][-45:]:
        print(l)
    c.close()


if __name__ == "__main__":
    main()
