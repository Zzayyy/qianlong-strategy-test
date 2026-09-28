# -*- coding: utf-8 -*-
"""
_lat_test.py —— 验证"响应时间"到底是链路真实 RTT 还是读取线程排队造成的。
做法：直接用一条连接发一条、然后立刻读回包，测真实 RTT（单条 syn).
再对比 send_test 的批量结果。
"""
import sys, time, json
sys.path.insert(0, r"D:\Code\Python\多线程\strategy_test")
from resp_min import RespClient
import protocol as P
import excel_loader as XL

_CASES = XL.load_cases(XL.default_excel("create"), want_types={"normal"}, quiet=True)
if not _CASES:
    sys.exit("没有可用用例，先生成：python make_excel.py --interface create")

kw = dict(host="192.168.1.137", port=6379, password="QianLong@2026&", db=0)
send = RespClient(**kw).connect()
read = RespClient(**kw).connect()

ASSIGN = 93      # 高位号段，避开真平台
stream = P.stream_for(ASSIGN)
send.xgroup_create(stream, P.GROUP, "0", mkstream=True)

# 先定位到末尾
raw = read.cmd("XREVRANGE", P.STREAM_REPLY, "+", "-", "COUNT", "1")
last = raw[0][0] if raw else "$"
print("reply stream last id =", last)

print("\n=== 单条同步 RTT（发一条立刻读回包）===")
lat = []
for i in range(30):
    payload = _CASES[i % len(_CASES)][3]
    rid = "LAT_%d_%d" % (i, int(time.time() * 1000))
    t0 = time.perf_counter()
    send.cmd("XADD", stream, "*", "request_id", rid, "task", payload)
    # 一直读到这条 rid 的回包
    deadline = time.time() + 5
    got = None
    while time.time() < deadline:
        r = read.cmd("XREAD", "BLOCK", "200", "COUNT", "100",
                     "STREAMS", P.STREAM_REPLY, last)
        if not r:
            continue
        for st, entries in r:
            for eid, f in RespClient._entries(entries):
                last = eid
                if f.get("request_id") == rid:
                    got = f
        if got:
            break
    dt = (time.perf_counter() - t0) * 1000
    lat.append(dt)
    print("  #%2d rid=%s  RTT=%.3f ms  reply=%s" % (i, rid, dt, got and got.get("task")))

lat.sort()
n = len(lat)
print("\n单条 RTT: 平均 %.3f ms  中位 %.3f  P95 %.3f  最大 %.3f"
      % (sum(lat)/n, lat[n//2], lat[int(n*0.95)], lat[-1]))
print("=> 若这个值远小于 send_test 批量跑出来的 250ms，")
print("   说明批量时的延迟主要是【单读取线程排队】造成的，不是链路慢。")

# 清理
send.delete(stream, P.stream_for(ASSIGN) + "-reply")
print("\n已清理 ST-%d" % ASSIGN)
send.close(); read.close()
