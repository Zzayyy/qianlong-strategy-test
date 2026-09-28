# -*- coding: utf-8 -*-
"""看「实际发出去的」报文：直接从 Redis 流里读回来（最接近真相）。

用法:
    python tests/_show_stream.py --stream ST-50            # 最近 5 条
    python tests/_show_stream.py --stream ST-50 -n 20      # 最近 20 条
    python tests/_show_stream.py --stream ST-50 --full     # 不截断，完整打印
    python tests/_show_stream.py --stream ST-50 --json     # 格式化 JSON（缩进）
    python tests/_show_stream.py --stream ST-50 --out dump.jsonl   # 导出到文件
"""
import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from resp_min import RespClient

a = argparse.ArgumentParser(description="查看 Redis 流里实际下发的报文")
a.add_argument("--host", default="192.168.1.137")
a.add_argument("--port", type=int, default=6379)
a.add_argument("--pwd", default="QianLong@2026&")
a.add_argument("--db", type=int, default=0)
a.add_argument("--stream", default="ST-50", help="要看的流（含 -reply 也行）")
a.add_argument("-n", "--count", type=int, default=5, help="看最近几条")
a.add_argument("--full", action="store_true", help="不截断 task")
a.add_argument("--json", action="store_true", help="格式化 JSON 缩进显示")
a.add_argument("--limit", type=int, default=2000, help="每条 task 最多打印多少字符")
a.add_argument("--out", default="", help="导出为 jsonl")
args = a.parse_args()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

c = RespClient(args.host, args.port, args.pwd, args.db).connect()
if not c.cmd("EXISTS", args.stream):
    print("流 %s 不存在（KEYS: %s）"
          % (args.stream, sorted(c.keys("ST-*") or [])))
    c.close()
    sys.exit(1)

print("流 %s  XLEN=%s  @ %s:%s db%s"
      % (args.stream, c.xlen(args.stream), args.host, args.port, args.db))
print("=" * 78)

rows = c.xrevrange(args.stream, args.count)
dump = io.open(args.out, "w", encoding="utf-8") if args.out else None
try:
    for eid, f in rows:
        rid = f.get("request_id", "")
        task = f.get("task", "")
        other = {k: v for k, v in f.items() if k not in ("request_id", "task")}
        print("\n[%s]  request_id=%s" % (eid, rid))
        if other:
            print("  其他字段: %s" % other)
        if args.json:
            try:
                obj = json.loads(task)
                print("  task(格式化):")
                print(json.dumps(obj, ensure_ascii=False, indent=2))
            except Exception as e:
                print("  task(非 JSON，%s):" % e)
                print("  " + task)
        else:
            show = task if args.full else task[:args.limit]
            print("  task(len=%d):" % len(task))
            print("  " + show)
            if not args.full and len(task) > args.limit:
                print("  ... 还有 %d 字符（加 --full 看全）" % (len(task) - args.limit))
        if dump:
            dump.write(json.dumps({"id": eid, "request_id": rid, "task": task},
                                  ensure_ascii=False) + "\n")
finally:
    if dump:
        dump.close()
        print("\n已导出 %d 条到 %s" % (len(rows), args.out))
c.close()
