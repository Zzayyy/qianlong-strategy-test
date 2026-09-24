# -*- coding: utf-8 -*-
"""
_plugin_e2e.py —— 验证「真插件当策略平台」这条路也能和 mock_datahub 闭环

在 136 上跑（.so 是 Linux 库）：
    1. mock_datahub 在 137 分配编号（或者在 136 本地也行，参数可调）
    2. 真插件 CreateMQ → 上线 → 拿编号 → 消费 ST-N → 回 DataHub_reply_stream
    3. 观察者抓包核对

本脚本由 Windows 侧通过 SSH 驱动。
"""
import paramiko
import sys

HOST, PORT, USER, PWD = "192.168.1.136", 22, "yangsh", "qianlong@135246"

REMOTE_DIR = "/home/yangsh/so_test/strategy"

UPLOAD = {
    r"D:\Code\Python\多线程\strategy_test\resp_min.py": REMOTE_DIR + "/resp_min.py",
    r"D:\Code\Python\多线程\strategy_test\protocol.py": REMOTE_DIR + "/protocol.py",
    r"D:\Code\Python\多线程\strategy_test\config.py": REMOTE_DIR + "/config.py",
    r"D:\Code\Python\多线程\strategy_test\cases.py": REMOTE_DIR + "/cases.py",
    r"D:\Code\Python\多线程\strategy_test\config.ini": REMOTE_DIR + "/config.ini",
}


def sh(cli, cmd, timeout=180):
    _, out, err = cli.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    return o, e


def main():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, port=PORT, username=USER, password=PWD, timeout=20)
    print("[OK] 已连上 %s" % HOST)

    sftp = cli.open_sftp()
    for src, dst in UPLOAD.items():
        sftp.put(src, dst)
        print("  上传 %s" % dst)
    sftp.close()

    # 远程配置指向 137 db0
    o, e = sh(cli, "cd %s && sed -i 's/^host = .*/host = 192.168.1.137/' config.ini "
                   "&& sed -i 's/^db = .*/db = 0/' config.ini && grep -E '^(host|db)' config.ini"
             % REMOTE_DIR)
    print("[cfg] " + o.strip())

    print("\n" + "=" * 74)
    print("### 1) 语法检查")
    print("=" * 74)
    o, e = sh(cli, "cd %s && python3 -m py_compile resp_min.py protocol.py config.py cases.py && echo COMPILE_OK" % REMOTE_DIR)
    print(o.strip() or e.strip())

    print("\n" + "=" * 74)
    print("### 2) 用例库自检（应打印 normal/destroy 数量）")
    print("=" * 74)
    o, e = sh(cli, "cd %s && PYTHONIOENCODING=utf-8 python3 -c \"import cases; cases.summarize()\"" % REMOTE_DIR)
    print(o.strip() or e.strip()[:2000])

    print("\n" + "=" * 74)
    print("### 3) 真插件 + mock_datahub 闭环")
    print("=" * 74)

    # 关键：.so 读的是 DataHub.ini（不是 config.ini）来决定连哪个 Redis。
    # 先看看它现在指向哪里，再把 mock 对齐到同一个地址，否则两边根本不在一个 Redis 上。
    o, _ = sh(cli, "grep -E 'REDIS(HOST|PORT|SELECT|PWD)' %s/DataHub.ini" % REMOTE_DIR)
    print("[.so 的 DataHub.ini 指向]\n%s" % o.strip())
    sh(cli, "cp -n %s/DataHub.ini %s/DataHub.ini.bak 2>/dev/null" % (REMOTE_DIR, REMOTE_DIR))

    # 让插件连 137 db0（与本次测试目标一致）
    sh(cli, "cd %s && sed -i 's/^REDISHOST=.*/REDISHOST=192.168.1.137/' DataHub.ini "
            "&& sed -i 's/^REDISSELECT=.*/REDISSELECT=0/' DataHub.ini" % REMOTE_DIR)
    o, _ = sh(cli, "grep -E 'REDIS(HOST|PORT|SELECT)' %s/DataHub.ini" % REMOTE_DIR)
    print("[改成]\n%s" % o.strip())

    # 清理可能的残留进程
    sh(cli, "pkill -f 'probe_plugin_e2e' 2>/dev/null; sleep 0.3")

    # 上传一个最小驱动脚本：跑 mock_datahub(线程) + 真插件(子进程)
    driver = r'''
# -*- coding: utf-8 -*-
"""真插件闭环自测：mock_datahub 在后台分配编号，插件前台跑"""
import json, os, subprocess, sys, threading, time
sys.path.insert(0, "/home/yangsh/so_test/strategy")
import protocol as P
from resp_min import RespClient

HOST, PORT, PWD, DB = "192.168.1.137", 6379, "QianLong@2026&", 0
ASSIGN = 21
STOP = threading.Event()

def ts(): return time.strftime("%H:%M:%S")
def say(m): print("[%s] %s" % (ts(), m), flush=True)

pub = RespClient(HOST, PORT, PWD, DB).connect()
say("已连 %s db%s  PING=%s" % (HOST, DB, pub.ping()))
for k in (P.stream_for(ASSIGN), P.reply_stream_for(ASSIGN)):
    try: pub.delete(k)
    except Exception: pass

def datahub():
    """模拟数据中台：订阅上线、分配编号、建流、发心跳"""
    sub = RespClient(HOST, PORT, PWD, DB).connect()
    sub.subscribe([P.CH_STRATEGY_ONLINE])
    say("mock 中台已订阅 %s" % P.CH_STRATEGY_ONLINE)
    uniq_seen = {}
    last = time.time()
    while not STOP.is_set():
        try:
            m = sub.read_message(timeout=1.0)
        except Exception:
            return
        if m:
            kind, ch, pl = m
            if kind in ("message", "pmessage") and pl:
                try: d = json.loads(pl)
                except Exception: continue
                u = d.get("unique_string", "")
                if d.get("id") == -1 and u:
                    sid = uniq_seen.setdefault(u, ASSIGN)
                    pub.publish(u, json.dumps({"id": sid, "dataHubString": "test"}))
                    say("→ 分配编号 %d -> %s" % (sid, u))
                    for s in (P.stream_for(sid), P.reply_stream_for(sid)):
                        r = pub.xgroup_create(s, P.GROUP, "0", mkstream=True)
                        say("   XGROUP CREATE %s -> %s" % (s, r))
        if time.time() - last > 5:
            last = time.time()
            for u, sid in uniq_seen.items():
                pub.publish(u, json.dumps({"id": sid, "dataHubString": "test1"}))
    say("mock 中台退出")

threading.Thread(target=datahub, daemon=True).start()
time.sleep(2)

raw = pub.cmd("XREVRANGE", P.STREAM_REPLY, "+", "-", "COUNT", "1")
before = raw[0][0] if raw else "0-0"
say("回包流末尾 = %s" % before)

env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
cmd = [sys.executable, "/home/yangsh/so_test/strategy/strategy_client.py",
       "--so", "/home/yangsh/so_test/strategy/libdatahub_strategy_plug.so",
       "--logpath", "/home/yangsh/so_test/strategy/", "--unique", "e2e",
       "--autoreply", "1", "--reply-data", '{"status":"OK"}', "--wait", "75"]
say("启动真插件: %s" % " ".join(cmd[1:]))
p = subprocess.Popen(cmd, cwd="/home/yangsh/so_test/strategy",
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

# 必须在独立线程里读 stdout，否则主线程会被阻塞到插件退出
def pump():
    try:
        for line in p.stdout:
            s = line.decode("utf-8", "replace").rstrip()
            if any(w in s for w in ("CreateMQ", "回调", "SUBSCRIBE", "XGROUP",
                                    "XREADGROUP", "XADD", "XACK", "ret=",
                                    "data1", "data2", "error")):
                say("[PLUGIN] " + s)
    except Exception:
        pass
threading.Thread(target=pump, daemon=True).start()

# 等插件拿到编号并建出 ST-21（真插件启动可能要 20s+）
say("等待插件建出 ST-%d ..." % ASSIGN)
ok = False
for i in range(70):
    if pub.cmd("EXISTS", P.stream_for(ASSIGN)):
        ok = True
        say("ST-%d 已出现（等待 %ds）" % (ASSIGN, i))
        break
    time.sleep(1)
if not ok:
    say("!! 超时：ST-%d 一直没出现，插件可能没拿到编号" % ASSIGN)

say("--- 核对 ---")
if pub.cmd("EXISTS", P.stream_for(ASSIGN)):
    for g in pub.xinfo_groups(P.stream_for(ASSIGN)):
        say("  GROUP %s consumers=%s pending=%s" % (g.get("name"), g.get("consumers"), g.get("pending")))
        for cc in pub.xinfo_consumers(P.stream_for(ASSIGN), g.get("name")):
            say("    CONSUMER %s pending=%s" % (cc.get("name"), cc.get("pending")))

import cases as C
n0 = pub.xlen(P.STREAM_REPLY)
say("发 3 条业务报文到 ST-%d" % ASSIGN)
for i in range(3):
    payload = C.payload_text(C.build_payload("create", "normal"))
    rid = "PLUGIN_E2E_%d" % i
    pub.cmd("XADD", P.stream_for(ASSIGN), "*", "request_id", rid, "task", payload)
    say("→ XADD ST-%d rid=%s" % (ASSIGN, rid))
time.sleep(6)

say("--- 回包核对 ---")
found = 0
for eid, f in pub.xrevrange(P.STREAM_REPLY, 12):
    rid = str(f.get("request_id", ""))
    if rid.startswith("PLUGIN_E2E_"):
        found += 1
        say("  ← #%s request_id=%s task=%s" % (eid, rid, str(f.get("task"))[:120]))
say("匹配到 %d/3 条插件回包；回包流总量 %s -> %s"
    % (found, before, pub.xlen(P.STREAM_REPLY)))

try: p.terminate()
except Exception: pass
try: p.kill()
except Exception: pass
STOP.set()
time.sleep(0.5)
say("结束")
'''
    sftp = cli.open_sftp()
    with sftp.open(REMOTE_DIR + "/probe_plugin_e2e.py", "w") as f:
        f.write(driver)
    sftp.close()
    print("  上传 probe_plugin_e2e.py")

    o, e = sh(cli, "cd %s && PYTHONIOENCODING=utf-8 timeout 150 python3 probe_plugin_e2e.py 2>&1" % REMOTE_DIR,
              timeout=260)
    print(o)
    if e.strip():
        print("[stderr]", e[:3000])

    # 恢复 DataHub.ini（本脚本会改它来对齐 Redis 地址，跑完必须还原，
    # 否则下次别人跑真插件会莫名其妙连到别的 Redis 上）
    print("\n" + "=" * 74)
    print("### 4) 恢复 DataHub.ini")
    print("=" * 74)
    # 必须用 cp -f：DataHub.ini 一定存在，cp -n 不会覆盖，等于没还原。
    o, _ = sh(cli, "cd %s && if [ -f DataHub.ini.bak ]; then cp -f DataHub.ini.bak DataHub.ini; "
                   "echo RESTORED; else echo NO_BACKUP; fi && "
                   "grep -E 'REDIS(HOST|PORT|SELECT)' DataHub.ini" % REMOTE_DIR)
    print(o.strip())

    cli.close()


if __name__ == "__main__":
    main()
