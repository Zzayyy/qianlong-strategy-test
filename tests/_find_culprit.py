# -*- coding: utf-8 -*-
"""逐条定位「哪条报文把策略平台弄挂了」。

背景 / 为什么需要它
------------------
策略平台是【多线程】处理的，完成顺序 != 发送顺序。所以事后从终态
（"ACK 到第 51 条为止"）反推凶手是【无效】的：那只能说明"有一批没做完"，
不能说明是哪一条。9 条「已 ACK 却无回包」+ 回包里的 "json parse error"
也证明平台对畸形报文是有容错的，不是一碰就死。

本工具消除并发这个变量：
    一次只发【一条】，等平台明确处理完（回包 或 XACK），再发下一条。
并发=1 时顺序重新确定，于是：
    * 前一条确认成功、后一条超时未确认 -> 后一条就是触发者

安全约束（重要）
--------------
只读命令：XPENDING / XRANGE / XINFO，**绝不 XREADGROUP**
  —— XREADGROUP 会加入平台的消费组抢走消息（Redis Streams 同组是负载均衡），
     真平台的单子会莫名少掉且不报错。
本工具只 XADD（发送）+ 只读探测，不消费。
若目标流上有外来（真平台）消费者，需显式 --force-live 才继续。

用法
----
# 先看只读体检，确认平台在线、告诉你该用哪个编号
python check_env.py

# 逐条试（默认从 destroy 用例开头一条条来；平台挂了就停并报告）
python tests/_find_culprit.py --assign-id 10 --interface account --type destroy

# 已知前 51 条是好的？直接从可疑段开始（但注意：多线程下"之前都好"不能保证）
python tests/_find_culprit.py --assign-id 10 --interface account --type destroy \
    --cases AD240-AD503

# 每条的等待上限（默认 5 秒）；平台慢就调大
python tests/_find_culprit.py --assign-id 10 --interface account --type destroy --wait 8
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import excel_loader as XL
import safety
from resp_min import RespClient

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def sid(s):
    """'ms-seq' -> (ms, seq)，用于可靠比较。"""
    x, _, y = str(s).partition("-")
    return (int(x), int(y or 0))


class Probe(object):
    """一次会话：负责发单条 + 只读判断它有没有被平台处理完。"""

    def __init__(self, host, port, pwd, db, stream, group, reply_stream,
                 wait, quiet=False):
        self.stream = stream
        self.group = group
        self.reply_stream = reply_stream
        self.wait = wait
        self.quiet = quiet
        self.conn = RespClient(host, port, pwd, db).connect()
        self.seq = 0

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    # ---------------- 只读状态 ----------------
    def group_info(self):
        try:
            gs = self.conn.xinfo_groups(self.stream)
        except Exception:
            return None
        return next((g for g in gs if g.get("name") == self.group), None)

    def consumers(self):
        try:
            return [str(c.get("name"))
                    for c in self.conn.xinfo_consumers(self.stream, self.group)]
        except Exception:
            return []

    def platform_alive(self):
        """平台是否还在消费这条流（有 consumer 且最近有活动）。"""
        g = self.group_info()
        if not g:
            return False, "消费组不存在"
        cs = self.conn.xinfo_consumers(self.stream, self.group)
        if not cs:
            return False, "该组下没有消费者（平台没在读）"
        idles = [int(c.get("idle") or 0) for c in cs]
        return True, "consumers=%s idle=%sms" % (
            [c.get("name") for c in cs], min(idles))

    def ped_ids(self):
        """当前 PEL（平台读了没确认）里的 entry id 集合。"""
        try:
            raw = self.conn.cmd("XPENDING", self.stream, self.group,
                                "-", "+", "100000") or []
            return set(r[0] for r in raw)
        except Exception:
            return set()

    def replies(self):
        """读回包流（只读 XRANGE）-> {request_id: task}。"""
        out = {}
        try:
            for _eid, f in self.conn.xrange(self.reply_stream, "-", "+"):
                rid = str(f.get("request_id", ""))
                if rid:
                    out[rid] = str(f.get("task", ""))
        except Exception:
            pass
        return out

    # ---------------- 发一条并等确认 ----------------
    def send_one(self, payload, label=""):
        """发一条，等平台处理完。返回 dict(verdict, reply, eid, waited)。"""
        self.seq += 1
        rid = "CULPRIT_%d_%d" % (int(time.time() * 1000), self.seq)

        # 记下发之前的状态，才能判断"有没有前进"
        g0 = self.group_info() or {}
        last0 = str(g0.get("last-delivered-id") or "0-0")
        replies0 = self.replies()

        try:
            eid = self.conn.cmd("XADD", self.stream, "*",
                                "request_id", rid, "task", payload)
        except Exception as e:
            return {"verdict": "SEND_FAIL", "reply": "", "eid": "",
                    "waited": 0.0, "rid": rid, "err": str(e)}
        eid = str(eid)

        t0 = time.time()
        reply = ""
        acked = False
        delivered = False
        while time.time() - t0 < self.wait:
            # 1) 平台读到了吗（last-delivered-id 前进到 >= 本条）
            g = self.group_info() or {}
            last = str(g.get("last-delivered-id") or "0-0")
            if sid(last) >= sid(eid) and last != "0-0":
                delivered = True
            # 2) 确认了吗（不在 PEL 里 + 已投递）
            if delivered and eid not in self.ped_ids():
                acked = True
            # 3) 有回包吗
            if not reply:
                rp = self.replies()
                if rid in rp:
                    reply = rp[rid]
                elif len(rp) > len(replies0):
                    # 有新增但 rid 对不上：可能是别的进程发的，忽略
                    pass
            if acked:
                break
            time.sleep(0.15)
        waited = time.time() - t0

        # 判定
        if acked:
            verdict = "OK" if reply else "OK_NO_REPLY"
        elif delivered:
            verdict = "STUCK_PEND"      # 读了但一直不确认 -> 很可能卡在这条
        else:
            verdict = "NOT_READ"        # 压根没读 -> 平台已经不干活了
        return {"verdict": verdict, "reply": reply, "eid": eid,
                "waited": waited, "rid": rid, "err": ""}


def brief(task, n=70):
    t = str(task).replace("\n", " ")
    return t[:n]


def main():
    ap = argparse.ArgumentParser(
        description="逐条定位哪条报文把策略平台弄挂了（单条注入 + 等确认）")
    ap.add_argument("--host", default="192.168.1.137")
    ap.add_argument("--port", type=int, default=6379)
    ap.add_argument("--pwd", default="QianLong@2026&")
    ap.add_argument("--db", type=int, default=0)
    ap.add_argument("--assign-id", type=int, required=True,
                    help="平台的分配编号，目标流 = ST-<id>")
    ap.add_argument("--stream", default="", help="直接指定流名（覆盖 --assign-id）")
    ap.add_argument("--group", default="user_group")
    ap.add_argument("--reply-stream", default="DataHub_reply_stream")
    ap.add_argument("--interface", default="account")
    ap.add_argument("--type", default="destroy")
    ap.add_argument("--cases", default="", help="只试这批（如 AD240-AD503）")
    ap.add_argument("--wait", type=float, default=5.0,
                    help="每条最多等多少秒确认（默认 5）")
    ap.add_argument("--limit", type=int, default=0,
                    help="最多试多少条（0=全部）")
    ap.add_argument("--baseline", default="1",
                    help="先发这条【正常】用例做健康基线（0=跳过）。"
                         "默认先发第 1 条 normal，确认平台本来是活的")
    ap.add_argument("--force-live", action="store_true",
                    help="目标流有外来（真平台）消费者时也继续")
    ap.add_argument("--yes", action="store_true",
                    help="跳过开跑前的确认提示")
    a = ap.parse_args()

    stream = a.stream or safety.stream_for(a.assign_id)

    # ---------------- 载入用例 ----------------
    excel = XL.default_excel(a.interface)
    try:
        pool = XL.load_cases(excel, want_types={a.type} if a.type else None,
                             cases_spec=a.cases, quiet=True)
    except SystemExit as e:
        print(e)
        return 1
    if not pool:
        print("没有匹配的用例：interface=%s type=%s cases=%r"
              % (a.interface, a.type, a.cases))
        return 1

    print("=" * 88)
    print("逐条定位「谁把平台弄挂」  目标流 = %s  用例 %d 条（%s/%s）"
          % (stream, len(pool), a.interface, a.type))
    print("=" * 88)
    print("原理：一次只发 1 条并等确认（回包 或 XACK）。并发=1 时顺序确定，")
    print("      所以「上一条成功、这一条超时未确认」= 这一条就是触发者。")
    print("      只用只读命令探测（XPENDING/XRANGE/XINFO），绝不 XREADGROUP。")
    print()

    probe = Probe(a.host, a.port, a.pwd, a.db, stream, a.group,
                  a.reply_stream, a.wait)
    try:
        # ---------------- 安全闸 ----------------
        if not safety.guard_stream(probe.conn, stream,
                                   force_live=a.force_live,
                                   tool="_find_culprit", what="发送"):
            print("[安全闸] 已中止。确认无害后加 --force-live。")
            return 2

        # ---------------- 健康基线 ----------------
        alive, why = probe.platform_alive()
        print("[健康检查] %s  -> %s" % (why, "平台在线" if alive else "★ 平台不在读！"))
        if not alive:
            print()
            print("平台当前不在这条流上消费，这时发什么都只会显示 NOT_READ，")
            print("分不出是哪条的锅。请先：")
            print("  1) 重启策略平台（真平台）或 python mock_strategy.py --assign-id %d"
                  % a.assign_id)
            print("  2) 等它开始消费后重跑本工具")
            return 3

        if a.baseline and a.baseline != "0":
            nb = XL.load_cases(excel, want_types={"normal"},
                               cases_spec=a.baseline, quiet=True)
            if not nb:
                nb = XL.load_cases(excel, want_types=None,
                                   cases_spec=a.baseline, quiet=True)
            if nb:
                no, typ, desc, txt, _rn = nb[0]
                print("[基线] 先发 1 条正常用例 %s（%s）确认平台本来能处理..."
                      % (no, typ))
                r = probe.send_one(txt, no)
                print("       -> %s  回包=%s  等 %.1fs"
                      % (r["verdict"], brief(r["reply"], 50), r["waited"]))
                if r["verdict"] not in ("OK", "OK_NO_REPLY"):
                    print()
                    print("★ 基线都没过：平台本来就不正常，先修平台再定位。")
                    return 4
                print("       平台健康，开始逐条试。")
                print()
            else:
                print("[基线] 找不到用例 %s，跳过" % a.baseline)

        # ---------------- 逐条 ----------------
        print("%-4s %-9s %-9s %-7s %-9s %s"
              % ("#", "case", "verdict", "等(s)", "类型", "回包/说明"))
        print("-" * 88)
        rows = []
        n = len(pool) if not a.limit else min(a.limit, len(pool))
        culprit = None
        for i, (no, typ, desc, txt, _rn) in enumerate(pool[:n], 1):
            r = probe.send_one(txt, no)
            rows.append((no, desc, r))
            try:
                d = json.loads(r["reply"]) if r["reply"] else None
                kind = ("ERR" if isinstance(d, dict) and "ErrID" in d else "OK") \
                    if r["reply"] else "-"
            except Exception:
                kind = "非JSON" if r["reply"] else "-"
            print("%-4d %-9s %-9s %-7.1f %-9s %s"
                  % (i, no, r["verdict"], r["waited"], kind,
                     brief(r["reply"], 46) if r["reply"] else desc[:46]))
            if r["verdict"] in ("STUCK_PEND", "NOT_READ", "SEND_FAIL"):
                culprit = (no, desc, r)
                break

        # ---------------- 结论 ----------------
        print("-" * 88)
        if culprit:
            no, desc, r = culprit
            print()
            print("=" * 88)
            print("★ 触发者：%s  —— %s" % (no, desc))
            print("=" * 88)
            print("  状态    : %s" % r["verdict"])
            print("  等待    : %.1fs 未确认" % r["waited"])
            print("  entry_id: %s" % r["eid"])
            print("  request_id: %s" % r["rid"])
            if r["err"]:
                print("  错误    : %s" % r["err"])
            print()
            print("  该条的 task（这是要复现的东西）：")
            one = [p for p in pool if p[0] == no]
            if one:
                print("  " + one[0][3][:600].replace("\n", "\\n"))
            print()
            print("  下一步：把这条单独发给平台，看它卡在哪一步（业务日志/堆栈）。")
            print("  注意：多线程下也【可能】是这几条组合才触发；若单发不挂，")
            print("        就用 --cases 把这一批一起发来复现。")
        else:
            ok = sum(1 for _, _, r in rows if r["verdict"] == "OK")
            nr = sum(1 for _, _, r in rows if r["verdict"] == "OK_NO_REPLY")
            print()
            print("本轮 %d 条全部被平台确认：OK=%d，ACK 但无回包=%d" % (len(rows), ok, nr))
            print("单条都不挂 -> 说明触发条件是【并发/组合】，不是单条毒丸。")
            print("此时可加大并发复现，或检查平台侧线程池/连接池耗尽。")
        return 0
    finally:
        probe.close()


if __name__ == "__main__":
    sys.exit(main())
