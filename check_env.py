# -*- coding: utf-8 -*-
"""
check_env.py —— 只读环境体检（连之前先跑这个）
=============================================
回答一个问题：**我连的这个 Redis 里，真实策略平台在不在？我的编号该用哪个？**

【严格只读】只做 PING / KEYS / TYPE / XLEN / XINFO / NUMSUB / CLIENT LIST / GET / JSON.GET，
绝不 XADD / PUBLISH / DEL，可以安全地在生产环境上跑。

判读方法：
  * 出现 `strategysrv-<id>` 这个 ReJSON 键  → 说明数据中台已经登记了编号为 <id> 的策略平台
    该键的内容是"这个策略平台上跑了哪些账号"，长度 = 该平台承载的用户数
  * 出现 `strategy-srv` 这个注册表         → 策略平台上线登记表（有 ip/mac/usecount/last-active）
  * `traderserver` 的 last-active-time 是新鲜的 → 那套环境是活的
  * 流 ST-<id> 存在且有 user_group 消费者 ST-<id> → 真实策略平台正在消费该流
  * PUBSUB NUMSUB>0 的频道                 → 有进程在订阅（真中台订阅带 _1 的那套）

用法：
    python check_env.py                       # 默认扫 136 与 137 的 db0/db1
    python check_env.py --host 192.168.1.136 --db 0
"""
import argparse
import re
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, r"D:\Code\Python\多线程\strategy_test")
from resp_min import RespClient

DEFAULT_HOSTS = ["192.168.1.136", "192.168.1.137"]
CHANS = ["strategyserver_online", "strategyserver_online_1",
         "strategyserver_beat", "strategyserver_beat_1",
         "strategyserver_offline", "strategyserver_offline_1"]


def flat(v, n=500):
    s = str(v)
    return s if len(s) <= n else s[:n] + "...(len=%d)" % len(s)


def scan(host, port, pwd, dbs):
    print("=" * 76)
    print("Redis %s:%d" % (host, port))
    print("=" * 76)
    try:
        c = RespClient(host, port, pwd, 0, timeout=10).connect()
    except Exception as e:
        print("  ✗ 连不上：%s" % e)
        return
    print("  PING = %s" % c.ping())

    verdict = {"real_strategy_ids": [], "live_env": False, "subs": {}}

    for db in dbs:
        c.select(db)
        n = c.dbsize()
        if not n:
            print("\n  db%d：空" % db)
            continue
        keys = c.keys()
        print("\n  db%d  dbsize=%d" % (db, n))

        # --- 策略平台登记 ---
        srv_ids = [k for k in keys if re.match(r"^strategysrv-\d+$", k)]
        if srv_ids:
            print("    ★ 数据中台已登记的策略平台：")
            for k in sorted(srv_ids):
                sid = k.split("-")[-1]
                val = c.json_get(k)
                cnt = 0
                try:
                    import json as _j
                    cnt = len(_j.loads(val))
                except Exception:
                    pass
                verdict["real_strategy_ids"].append((host, db, sid))
                print("        ST-%s   承载账号数=%d   (%s)" % (sid, cnt, k))
        if c.cmd("EXISTS", "strategy-srv"):
            print("    ★ strategy-srv 注册表（策略平台上线记录）：")
            v = c.json_get("strategy-srv")
            try:
                import json as _j
                for item in _j.loads(v).get("strategy-srv", []):
                    print("        id=%s usecount=%s ip=%s mac=%s last=%s enable=%s"
                          % (item.get("id"), item.get("usecount"), item.get("ip"),
                             item.get("mac"), item.get("last-active-time"),
                             item.get("enable")))
            except Exception:
                print("        %s" % flat(v))

        # --- 委托服务器注册表（判断环境是否活跃）---
        if c.cmd("EXISTS", "traderserver"):
            v = c.json_get("traderserver")
            try:
                import json as _j
                print("    traderserver 注册表：")
                for item in _j.loads(v).get("traderserver", []):
                    lt = item.get("lastActiveTimeLong") or 0
                    age = int(time.time() - lt) if lt else -1
                    fresh = "  ← 活跃" if 0 <= age < 300 else ""
                    print("        id=%s %s mac=%s last=%s (%s前) enable=%s%s"
                          % (item.get("id"), item.get("ip"), item.get("mac"),
                             item.get("last-active-time"),
                             ("%ds" % age) if age >= 0 else "?", item.get("enable"), fresh))
                    if 0 <= age < 300:
                        verdict["live_env"] = True
            except Exception:
                print("        %s" % flat(v))

        # --- ST-* 流 ---
        st = [k for k in keys if re.match(r"^ST-\d+", k)]
        if st:
            print("    策略方向的流：")
            for k in sorted(st):
                if c.cmd("TYPE", k) != "stream":
                    continue
                xlen = c.xlen(k)
                gs = c.xinfo_groups(k)
                info = "(无消费组)"
                if gs:
                    parts = []
                    for g in gs:
                        cons = c.xinfo_consumers(k, g.get("name"))
                        parts.append("group=%s consumers=%s pending=%s"
                                     % (g.get("name"),
                                        [x.get("name") for x in cons], g.get("pending")))
                    info = " | ".join(parts)
                print("        %-16s XLEN=%-8s %s" % (k, xlen, info))

    # --- 频道订阅 ---
    c.select(0)
    print("\n  频道订阅者：")
    for ch in CHANS:
        r = c.cmd("PUBSUB", "NUMSUB", ch)
        cnt = r[1] if len(r) > 1 else "?"
        verdict["subs"][ch] = cnt
        if cnt not in (0, "0"):
            print("      %-30s %s  ← 有进程在订阅" % (ch, cnt))
    if not any(v not in (0, "0") for v in verdict["subs"].values()):
        print("      （全部为 0，没有进程在订阅策略频道）")

    # --- 订阅连接来源 ---
    try:
        raw = c.cmd("CLIENT", "LIST")
        rows = []
        for line in str(raw).splitlines():
            m = dict(re.findall(r"(\w[\w-]*)=(\S+)", line))
            subs = int(m.get("sub", 0)) + int(m.get("psub", 0))
            if subs:
                rows.append((m.get("addr"), m.get("db"), subs,
                             m.get("cmd"), m.get("tot-cmds")))
        if rows:
            print("\n  带订阅的连接（谁在订）：")
            for a, d, s, cmd, tc in rows:
                print("      %-24s db=%-3s sub=%-3s cmd=%-12s tot-cmds=%s" % (a, d, s, cmd, tc))
    except Exception:
        pass

    c.close()

    print("\n  " + "-" * 72)
    if verdict["real_strategy_ids"]:
        print("  ▶ 结论：这套环境里有【真实策略平台】登记：")
        for h, db, sid in verdict["real_strategy_ids"]:
            print("       %s db%d  ->  下发流 = ST-%s" % (h, db, sid))
            print("       要打真实策略平台：send_test.py --host %s --db %d --assign-id %s"
                  % (h, db, sid))
    else:
        print("  ▶ 结论：没发现已登记的策略平台（可能是纯测试环境）")
    if verdict["live_env"]:
        print("  ▶ 注意：traderserver 心跳是新鲜的 => 这是【活跃环境】，别乱写数据")
    print()


def main():
    ap = argparse.ArgumentParser(description="只读环境体检")
    ap.add_argument("--host", default="", help="只查这一台；默认查 136 和 137")
    ap.add_argument("--port", type=int, default=6379)
    ap.add_argument("--pwd", default="QianLong@2026&")
    ap.add_argument("--dbs", default="0,1")
    a = ap.parse_args()

    hosts = [a.host] if a.host else DEFAULT_HOSTS
    dbs = [int(x) for x in a.dbs.split(",")]
    print("【只读体检】不会写入任何数据\n")
    for h in hosts:
        scan(h, a.port, a.pwd, dbs)
    print("提示：本脚本只读。真正发数据前请确认 --host/--db 指向的是你想测的环境。")


if __name__ == "__main__":
    main()
