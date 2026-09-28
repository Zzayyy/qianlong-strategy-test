# -*- coding: utf-8 -*-
"""手动发 pwdUpdate(MsgType=17)，对比 明文Pwd / 加密Pwd 两种情况，看真平台回不回。"""
import sys, time, json
sys.path.insert(0, r"D:\Code\Python\多线程\strategy_test")
from resp_min import RespClient
from pwd_encode import encode_pwd

HOST, PWD, DB = "192.168.1.137", "QianLong@2026&", 0
STREAM, GROUP = "ST-50", "user_group"

base = {"Account": {"Model": 0, "AccountType": 7, "AccAtt": 6,
                    "FAccount": "010100011300"},
        "UniqueAccount": "010100011300_7_6"}

cases = [
    ("明文 Pwd", dict(base, Pwd="123456")),
    ("加密 Pwd", dict(base, Pwd=encode_pwd("123123", "010100011300"))),
]
# 再补一个去掉 Model / AccAtt 的最简
cases.append(("加密 Pwd + 无 Model", {"Account": {"AccountType": 7, "AccAtt": 6,
                                                  "FAccount": "010100011300"},
                                      "UniqueAccount": "010100011300_7_6",
                                      "Pwd": encode_pwd("123123", "010100011300")}))

c = RespClient(host=HOST, port=6379, password=PWD, db=DB).connect()
start_id = c.cmd("XREVRANGE", "DataHub_reply_stream", "+", "-", "COUNT", "1")
before = (c.xrevrange("DataHub_reply_stream", 1) or [("0-0", {})])[0][0]

for i, (label, body) in enumerate(cases, 1):
    rid = "PWD17_%d_%d" % (int(time.time() * 1000), i)
    task = json.dumps({"pwdUpdate": body, "MsgType": 17}, separators=(",", ":"))
    eid = c.xadd(STREAM, {"request_id": rid, "task": task})
    print("\n[%s] XADD %s rid=%s" % (label, eid, rid))
    print("    task = %s" % task[:220])

print("\n等待 12 秒看回包 ...")
time.sleep(12)
print("\n--- 本次之后 DataHub_reply_stream 新增 ---")
seen = c.xrevrange("DataHub_reply_stream", 30)
for eid, d in reversed(seen):
    if eid > before:
        print("  %s  rid=%-30s %s" % (eid, d.get("request_id"), d.get("task")))
print("\n--- ST-50 group ---")
for g in c.xinfo_groups(STREAM):
    print("  ", g)
c.close()
