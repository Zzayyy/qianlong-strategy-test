# -*- coding: utf-8 -*-
"""看「平台卡在哪一条」：只读，对着服务端 PEL 列出已 ACK / 未 ACK 的分界。

用法:
    python tests/_ack_gap.py --stream ST-50
    python tests/_ack_gap.py --stream ST-50 -n 40

★ 为什么不能只看 XPENDING：
  不在 PEL 里 ≠ 已 ACK —— 还可能是【平台压根没读】。
  真正的分界是 last-delivered-id：
      id <= last-delivered-id  = 投递过（再分：在 PEL 里 = 没确认 / 不在 = 已确认）
      id >  last-delivered-id  = 平台从没读过
  三种状态分开看，才能判断平台是"卡住"还是"根本没消费"。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from resp_min import RespClient

a = argparse.ArgumentParser(description="定位平台卡在哪一条（只读）")
a.add_argument("--host", default="192.168.1.137")
a.add_argument("--port", type=int, default=6379)
a.add_argument("--pwd", default="QianLong@2026&")
a.add_argument("--db", type=int, default=0)
a.add_argument("--stream", default="ST-50")
a.add_argument("--group", default="user_group")
a.add_argument("-n", "--count", type=int, default=25, help="最多列出多少条")
args = a.parse_args()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def sid(s):
    """'ms-seq' -> (ms, seq)，用于可靠比较。"""
    x, _, y = str(s).partition("-")
    return (int(x), int(y or 0))


def brief(task, limit=46):
    t = str(task).replace("\n", " ")
    try:
        d = json.loads(t)
        mt = d.get("MsgType") if isinstance(d, dict) else "?"
    except Exception:
        mt = "非JSON"
    return "MsgType=%-7s %s" % (mt, t[:limit])


c = RespClient(args.host, args.port, args.pwd, args.db).connect()
try:
    if not c.cmd("EXISTS", args.stream):
        print("流 %s 不存在（现有: %s）"
              % (args.stream, sorted(c.keys("ST-*") or [])))
        sys.exit(1)
    entries = c.xrange(args.stream, "-", "+")
    groups = c.xinfo_groups(args.stream)
    g = next((x for x in groups if x.get("name") == args.group), None)
    print("流 %s  XLEN=%d" % (args.stream, len(entries)))
    if not g:
        print("没有消费者组 %s -> 没有平台在消费这条流" % args.group)
        sys.exit(0)
    last = sid(g.get("last-delivered-id") or "0-0")
    cons = [str(x.get("name")) for x in c.xinfo_consumers(args.stream, args.group)]
    print("组 %s: 消费者=%s  entries-read=%s  pending=%s  lag=%s"
          % (args.group, ",".join(cons), g.get("entries-read"),
             g.get("pending"), g.get("lag")))
    print("last-delivered-id=%s" % g.get("last-delivered-id"))
    print("=" * 84)

    pend = set(r[0] for r in (c.cmd("XPENDING", args.stream, args.group,
                                    "-", "+", "100000") or []))
    n_ack = n_pend = n_never = 0
    first_pend = first_never = None
    for i, (eid, f) in enumerate(entries, 1):
        if eid in pend:
            st, n_pend = "PEND", n_pend + 1
            first_pend = first_pend or i
        elif sid(eid) <= last:
            st, n_ack = "ACK", n_ack + 1
        else:
            st, n_never = "NEVER", n_never + 1
            first_never = first_never or i
        if i <= args.count:
            print("  #%-4d %-5s %-18s %s"
                  % (i, st, eid, brief(f.get("task"))))
    print("=" * 84)
    print("ACK=%d（平台处理完）  PEND=%d（读了没确认）  NEVER=%d（从没读过）"
          % (n_ack, n_pend, n_never))
    print("第一条 PEND=第 %s 条   第一条 NEVER=第 %s 条" % (first_pend, first_never))
    print()
    if n_pend and first_pend:
        print("-> 平台读到第 %d 条附近就不再动了；从第 %s 条起是它没处理完的。"
              % (first_pend, first_pend))
        print("   重点看那一条的 task 内容（很可能是让平台卡死的畸形报文）。")
    elif n_never:
        print("-> 平台从第 %s 条起【完全没读】，说明它在更早的地方就停了"
              "（或消费者已不在）。" % first_never)
    else:
        print("-> 全部处理完，平台正常。")
finally:
    c.close()
