# -*- coding: utf-8 -*-
"""只读：当前 137 平台状态 —— 是否在等编号、哪条流有活的消费者。"""
import sys, time
sys.path.insert(0, r"D:\Code\Python\多线程\strategy_test")
from resp_min import RespClient

kw = dict(host="192.168.1.137", port=6379, password="QianLong@2026&", db=0)
c = RespClient(**kw).connect()
print("KEYS:", sorted(c.keys("*")))
print("CHANNELS:", c.pubsub_channels())
print()
for s in sorted(c.keys("ST-*") or []):
    if s.endswith("-reply"):
        continue
    names = []
    for g in c.xinfo_groups(s):
        for cu in c.xinfo_consumers(s, g.get("name")):
            names.append("%s(idle=%sms)" % (cu.get("name"), cu.get("idle")))
    print("%-8s XLEN=%-4s consumers=%s" % (s, c.xlen(s), names or "无"))
c.close()

print()
print("MONITOR 8 秒：平台还在喊 id=-1（等编号）吗？")
mon = RespClient(**kw).connect()
mon._write("MONITOR"); mon._read(); mon.sock.settimeout(1.0)
stop = time.time() + 8
online = beat = xread = 0
while time.time() < stop:
    try:
        m = mon._read()
    except Exception:
        continue
    if not m:
        continue
    s = m if isinstance(m, str) else str(m)
    if "strategyserver_online" in s:
        online += 1
    elif "strategyserver_beat" in s:
        beat += 1
    elif "XREADGROUP" in s:
        xread += 1
mon.close()
print("  strategyserver_online (求编号) = %d 次" % online)
print("  strategyserver_beat  (心跳)    = %d 次" % beat)
print("  XREADGROUP           (在消费)  = %d 次" % xread)
