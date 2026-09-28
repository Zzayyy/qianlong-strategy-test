# -*- coding: utf-8 -*-
"""
_cleanup_my_consumers.py —— 清理我误测时在 136 ST-0 上留下的消费者
================================================================
这些 ST-0-w1/w2/w3 是我误测的垃圾（安全闸 bug 导致），不是真平台的。
删除前逐个确认 pending=0（=没扣住消息），有 pending 就拒绝删除（否则会丢消息）。

安全前提：【只删我自己建的 -wN 名字】，绝不碰真平台的 "ST-0"。
用法：python _cleanup_my_consumers.py --apply     # 不带 --apply 只预览
"""
import argparse
import sys

sys.path.insert(0, r"D:\Code\Python\多线程\strategy_test")
from resp_min import RespClient

a = argparse.ArgumentParser()
a.add_argument("--host", default="192.168.1.136")
a.add_argument("--db", type=int, default=0)
a.add_argument("--stream", default="ST-0")
a.add_argument("--group", default="user_group")
a.add_argument("--apply", action="store_true", help="真的删除；不加只预览")
args = a.parse_args()

c = RespClient(args.host, 6379, "QianLong@2026&", args.db).connect()
print("目标 %s db%d 流=%s group=%s" % (args.host, args.db, args.stream, args.group))

# 先看该 group 是否真的存在
groups = [g.get("name") for g in c.xinfo_groups(args.stream)]
if args.group not in groups:
    print("该 group 不存在，无需清理")
    c.close()
    sys.exit(0)

mine = []
for cc in c.xinfo_consumers(args.stream, args.group):
    nm = str(cc.get("name"))
    # 【注意】mock 的 consumer 名绑定的是"req 流名"，不是当前流名：
    #   mock_strategy 用 consumer = ST-<id>-wN，同时 XREADGROUP 读
    #   ST-<id> 和 ST-<id>-reply 两条流 —— 所以在 ST-<id>-reply 上，
    #   消费者也叫 ST-<id>-wN（而不是 ST-<id>-reply-wN）。
    # 这里按"以 ST- 开头且去掉 ST-<n>-reply 后剩下 -wN"来匹配，
    # 否则在 -reply 流上会漏判（实测踩过：报"很干净"但实际有残留）。
    base = args.stream
    if base.endswith("-reply"):
        base = base[:-len("-reply")]          # ST-0-reply -> ST-0
    if nm.startswith(base + "-w") and nm[len(base) + 2:].isdigit():
        mine.append((nm, cc.get("pending"), cc.get("idle")))

if not mine:
    print("没有我建的消费者（很干净）")
    c.close()
    sys.exit(0)

print("\n发现我建的消费者 %d 个：" % len(mine))
safe = True
for nm, pend, idle in mine:
    ok = pend in (0, "0")
    print("   %-12s pending=%-4s idle=%-9sms %s"
          % (nm, pend, idle, "可安全删除" if ok else "⚠ 有扣留消息，跳过！"))
    if not ok:
        safe = False

if not safe:
    print("\n存在有待处理消息的消费者，为免丢消息【不执行删除】。请人工确认。")
    c.close()
    sys.exit(1)

if not args.apply:
    print("\n（预览模式）确认无误后加 --apply 执行删除")
    c.close()
    sys.exit(0)

print("\n开始删除：")
for nm, _, _ in mine:
    try:
        n = c.cmd("XGROUP", "DELCONSUMER", args.stream, args.group, nm)
        print("   DELCONSUMER %s -> 释放了 %s 条 pending 消息" % (nm, n))
    except Exception as e:
        print("   删除 %s 失败: %s" % (nm, e))

print("\n删除后剩余消费者：")
for cc in c.xinfo_consumers(args.stream, args.group):
    print("   %-12s pending=%s idle=%sms" % (cc.get("name"), cc.get("pending"), cc.get("idle")))
c.close()
