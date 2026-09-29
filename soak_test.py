# -*- coding: utf-8 -*-
"""
稳定性测试编排（Soak Test）—— 策略方向
========================================================
参考 datahub_test/soak_test.py，但判据按【策略方向】重做。

为什么不能照抄 datahub 的判据（重点）
------------------------------------
1) datahub 用「成功率% / 回复率%」当异常判据。策略方向不适用：
   destroy 用例（畸形报文）**本来就大量不回包**（实测 AD232 这种
   Pwd 非法密文的，平台读了、ACK 了、但故意不回）。若用回复率下限，
   正常跑 destroy 会被判成一堆假异常。

2) 策略方向真正要盯的是【平台会不会挂着】。硬信号是目标流消费组的
   两个数（见 README §2.2）：
       lag      = 还没被平台读走的条数（平台不伸手 -> 涨）
       pending  = 读了但没 ACK 的条数（平台读了卡住 -> 涨）
   平台正常时这两个数会稳定在小值；平台挂掉后它们会**单调上涨**。
   所以本脚本把 lag / 未ACK 当成一等指标，并支持 --max-lag / --max-pending
   阈值告警。这比"回复率"可靠得多。

设计（与 datahub 一致）：不改 send_test.py 的发送逻辑，靠"反复调用 + 汇总"：
  每轮调一次 send_test.py（--max <batch> + --no-run-log），
  读本轮 stats JSON，追加到 trend.csv；
  正常轮的明细清掉、异常轮保留；最后产出 soak.log / trend.csv / summary.json。

用法：
  # 8 小时，每轮 500 条（destroy），只监控不清理
  python soak_test.py --assign-id 10 --interface account --type destroy \
      --hours 8 --batch 500 --clean monitor

  # 短测：只跑 5 轮（--rounds 优先于 --hours，便于验证流程）
  python soak_test.py --assign-id 10 --interface account --type normal \
      --rounds 5 --batch 200 --workers 4

  # 长稳时轮换用例（按行号分段、末尾回绕，避免反复发同一批）
  python soak_test.py --assign-id 10 --interface account --type normal \
      --hours 8 --batch 1000 --rotate

  # 打真平台要显式加 --force-live（会透传给 send_test.py）
  python soak_test.py --assign-id 10 --interface account --type normal \
      --rounds 3 --batch 100 --force-live

其它未识别参数原样透传给 send_test.py（--workers / --wait / --rate /
--no-reply / --force-live / --quiet ...）。

后台运行（Linux）：
  nohup python3 soak_test.py --assign-id 10 --type destroy --hours 8 \
        --batch 500 --clean monitor > /dev/null 2>&1 &
  tail -f out/soak/soak_<接口>_<时间>_soak.log

输出（out/soak/）：
  soak_<接口>_<时间>_soak.log      编排日志（每轮一行关键指标 + 异常）
  soak_<接口>_<时间>_trend.csv     每轮指标时间序列（核心产物，可直接画图）
  soak_<接口>_<时间>_summary.json  整体汇总
  soak_<接口>_<时间>_rounds/       异常轮（或 --keep-round-stats 时全部轮）明细
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEND_TEST = os.path.join(BASE_DIR, "send_test.py")
REPLY_STREAM = "DataHub_reply_stream"     # 策略平台 -> 数据中台 的回包流（实测）
GROUP = "user_group"

# 异常判定阈值（只给该轮打"异常"标记并保留明细，绝不中断整体测试）
DEFAULT_MIN_REPLY_RATE = 0.0   # 回复率下限%，0=不判（destroy 本来就不回包）
DEFAULT_MAX_LAG = 0            # lag  超过它算异常，0=不判
DEFAULT_MAX_PENDING = 0        # 未ACK 超过它算异常，0=不判


# ==================== 工具 ====================
def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _as_num(v):
    """把 stats 值安全转成数值；None/''/'N/A'/非法一律 None。

    照搬 datahub 的教训（0919 soak 事故）：stats 里可能有 'N/A' 这种
    【非空字符串 = truthy】，`int(v or 0)` 的 `or 0` 兜不住，int() 会抛
    ValueError，一轮脏数据就能干掉跑了 21.5h 的任务。
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _int_or(v, default=0):
    n = _as_num(v)
    return int(n) if n is not None else default


def _float_or(v, default=0.0):
    n = _as_num(v)
    return float(n) if n is not None else default


# ==================== Redis 流监控 ====================
class StreamProbe:
    """只读监控：目标流(ST-<id>)与回包流的长度 + 消费组 lag/pending。

    复用 resp_min.RespClient（纯 socket，不依赖 redis-py）。
    """

    def __init__(self, kw, stream):
        self.kw = kw
        self.stream = stream
        self._conn = None

    def _c(self):
        if self._conn is None:
            from resp_min import RespClient
            self._conn = RespClient(self.kw["host"], self.kw["port"],
                                    self.kw["password"],
                                    self.kw["db"]).connect()
        return self._conn

    def _reset(self):
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def xlen(self, stream):
        try:
            v = self._c().cmd("XLEN", stream)
            return int(v) if v is not None else None
        except Exception:
            self._reset()
            return None

    def group_state(self, stream=None, group=GROUP):
        """返回 (已读, 未ACK, lag)；任何一项取不到就给 None。"""
        stream = stream or self.stream
        try:
            gs = self._c().xinfo_groups(stream)
        except Exception:
            self._reset()
            return None, None, None
        g = next((x for x in (gs or []) if x.get("name") == group), None)
        if not g:
            return None, None, None
        read = _as_num(g.get("entries-read"))
        pend = _as_num(g.get("pending"))
        lag = _as_num(g.get("lag"))          # Redis 7+；取不到是 None（合法值）
        return (int(read) if read is not None else None,
                int(pend) if pend is not None else None,
                int(lag) if lag is not None else None)

    def clean_reply(self, stream=REPLY_STREAM):
        """清理回包流：删消费组 + 清内容 + 补占位。

        ⚠ 回包流 DataHub_reply_stream 是【多条 ST-* 共用】的全局流。
          如果你不是独占环境，别开 per-round 清理，会干扰别人。
          保留流本身（补一条占位），避免下一轮读取因流不存在出问题。
        """
        try:
            c = self._c()
            try:
                c.cmd("XGROUP", "DESTROY", stream, GROUP)
            except Exception:
                pass
            try:
                c.cmd("XTRIM", stream, "MAXLEN", "0")
            except Exception:
                pass
            c.cmd("XADD", stream, "*", "_soak_keepalive_", "1")
            return True
        except Exception:
            self._reset()
            return False


# ==================== 用例轮换 ====================
def _rows_of_type(excel_path, type_tag, want_types=None):
    """返回【匹配该类型】的数据行号列表（1 起始），按表内顺序。

    ★ 为什么不能只数总行数（datahub 的做法在策略方向会失效）：
      本表行序是 normal(10000) -> error -> destroy(96)。
      destroy 只占 10008~10103。若按"总行数 10103"切段，
      头几轮的 --cases 1-500 全是 normal，配 --type destroy 会一条都选不到，
      整场 soak 全变成"跳过"（实测 3 轮全跳过）。
      所以轮换必须落在【本类型真实存在的行号】上。
    """
    try:
        from openpyxl import load_workbook
        wb = load_workbook(excel_path, read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        try:
            header = next(it)
        except StopIteration:
            wb.close()
            return []
        keys = []
        for h in header:
            import re as _re
            m = _re.search(r"\(([A-Za-z_][A-Za-z0-9_]*)\)", str(h))
            keys.append(m.group(1) if m else str(h).strip())
        try:
            ti = keys.index("case_type")
        except ValueError:
            ti = None
        rows = []
        n = 0
        for raw in it:
            if raw is None or all(v is None or str(v).strip() == "" for v in raw):
                continue
            n += 1
            if ti is None:
                rows.append(n)
                continue
            typ = str(raw[ti] if ti < len(raw) else "" or "normal").strip().lower()
            if want_types and typ not in want_types:
                continue
            rows.append(n)
        wb.close()
        return rows
    except Exception:
        return []


def _cases_spec(offset, batch, total):
    """按【数据行号】生成 --cases 规格（1 起始，末尾回绕）。

    total<=0 或 batch>=total 时返回 None（不用筛，全发）。
    ★ 纯数字 = 行号语义，与 datahub 一致（见 excel_loader.parse_case_spec）。
    """
    if total <= 0 or batch >= total:
        return None
    parts, cur, left = [], offset % total + 1, batch
    while left > 0:
        take = min(left, total - cur + 1)
        parts.append("%d-%d" % (cur, cur + take - 1) if take > 1 else str(cur))
        left -= take
        cur = 1
    return ",".join(parts)


def _compress_rows(rows):
    """把行号列表压成 --cases 串：连续段用 a-b，孤点用单个数字。

    例：[1,2,3,7,9,10] -> "1-3,7,9-10"
    这样"本类型的行号"即使不连续也不会拼出超长参数。
    """
    if not rows:
        return ""
    rows = sorted(rows)
    parts = []
    start = prev = rows[0]
    for x in rows[1:]:
        if x == prev + 1:
            prev = x
            continue
        parts.append("%d-%d" % (start, prev) if prev > start else str(start))
        start = prev = x
    parts.append("%d-%d" % (start, prev) if prev > start else str(start))
    return ",".join(parts)


def _rotate_spec(rows, offset, batch):
    """在【给定行号列表】上取 batch 个（末尾回绕），返回 --cases 串或 None。"""
    total = len(rows)
    if total <= 0 or batch >= total:
        return None          # 一轮就能全发完，不用切
    picked = [rows[(offset + i) % total] for i in range(batch)]
    return _compress_rows(picked)


# ==================== 单轮 ====================
def run_round(args, round_no, round_dir, cases_spec=None):
    """跑一轮 send_test.py。返回 (stats 或 None, 错误信息)。"""
    os.makedirs(round_dir, exist_ok=True)
    # soak 的强制参数放在透传参数之后，确保覆盖（--max / --stats-out）
    cmd = [sys.executable, SEND_TEST] + list(args.passthrough) + [
        "--interface", args.interface,
        "--max", str(args.batch),
        "--stats-out", round_dir,
        "--label", "soak_r%05d" % round_no,   # 固定标签，便于定位
        "--no-run-log",
    ]
    if args.type:
        cmd += ["--type", args.type]
    if args.assign_id is not None:
        cmd += ["--assign-id", str(args.assign_id)]
    if args.stream:
        cmd += ["--stream", args.stream]
    if cases_spec:
        cmd += ["--cases", cases_spec]
    if args.host:
        cmd += ["--host", args.host]
    if args.port:
        cmd += ["--port", str(args.port)]
    if args.pwd:
        cmd += ["--pwd", args.pwd]
    if args.db is not None:
        cmd += ["--db", str(args.db)]

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run(cmd, cwd=BASE_DIR, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=args.round_timeout)
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        tail = " | ".join(out.splitlines()[-3:]) if out else ""
        rc = p.returncode
    except subprocess.TimeoutExpired:
        return None, "单轮超时 %ss" % args.round_timeout, None
    except Exception as e:
        return None, "子进程异常: %s" % e, None

    # send_test 退出码 2 = 被安全闸拦下（目标流上有真平台消费者）
    if rc == 2 or ("安全闸" in out and "中止" in out):
        return None, "SAFETY_GATE", tail
    if rc == 1 and ("未匹配到任何用例" in out or "没有匹配的用例" in out):
        return None, "NO_CASES", tail

    stats = None
    try:
        files = sorted(
            (f for f in os.listdir(round_dir) if f.endswith("_stats.json")),
            key=lambda f: os.path.getmtime(os.path.join(round_dir, f)))
        if files:
            with open(os.path.join(round_dir, files[-1]), encoding="utf-8") as fh:
                stats = json.load(fh)
    except Exception as e:
        return None, "读取 stats 失败: %s" % e, tail
    if stats is None:
        return None, "未生成 stats（退出码 %s）%s" % (rc, tail), tail
    return stats, "", tail


# ==================== 主流程 ====================
def main():
    ap = argparse.ArgumentParser(
        description="稳定性测试编排（复用 send_test.py，按轮持续发送并汇总趋势）",
        epilog="未识别参数会原样透传给 send_test.py")
    ap.add_argument("--interface", required=True,
                    help="接口名：create/modify/remove/pwdUpdate/account")
    ap.add_argument("--type", default="normal",
                    help="用例类型 normal/error/destroy/all（默认 normal）")
    ap.add_argument("--assign-id", type=int, default=None,
                    help="策略平台分配编号，目标流 = ST-<id>（必填其一，或用 --stream）")
    ap.add_argument("--stream", default="", help="直接指定目标流名（覆盖 --assign-id）")
    ap.add_argument("--host", default="", help="Redis 主机（默认取 config.ini）")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--pwd", default="")
    ap.add_argument("--db", type=int, default=None)

    ap.add_argument("--hours", type=float, default=8.0, help="总时长小时（默认 8）")
    ap.add_argument("--rounds", type=int, default=0,
                    help="按轮数跑（如 5）；指定后忽略 --hours。便于短测/回归")
    ap.add_argument("--batch", type=int, default=500, help="每轮条数（默认 500）")
    ap.add_argument("--gap", type=float, default=0.0, help="轮间间隔秒（默认 0）")
    ap.add_argument("--clean", choices=["monitor", "per-round"],
                    default="monitor",
                    help="monitor=只监控不清理(默认，最安全)；"
                         "per-round=每轮清空回包流（⚠ 回包流是多条 ST-* 共用的，"
                         "非独占环境别开）")
    ap.add_argument("--rotate", action="store_true",
                    help="每轮轮换用例（按行号分段、末尾回绕），避免反复发同一批数据")
    ap.add_argument("--keep-round-stats", action="store_true",
                    help="保留每一轮的 stats JSON（默认只留异常轮）")
    ap.add_argument("--round-timeout", type=float, default=600.0,
                    help="单轮最长秒数，防卡死（默认 600）")
    ap.add_argument("--soak-out", default="", help="输出根目录（默认 out/soak）")

    # ---- 异常判据（策略方向：默认只看发送失败 + lag/pending 增长）----
    ap.add_argument("--min-reply-rate", type=float, default=DEFAULT_MIN_REPLY_RATE,
                    help="回复率下限%%，低于它算异常。默认 0=不判"
                         "（destroy 用例本来就大量不回包，别拿它判）")
    ap.add_argument("--max-lag", type=int, default=DEFAULT_MAX_LAG,
                    help="目标流 lag 超过它算异常（平台不读的信号）。默认 0=不判")
    ap.add_argument("--max-pending", type=int, default=DEFAULT_MAX_PENDING,
                    help="目标流未ACK 超过它算异常（平台读了卡住的信号）。"
                         "默认 0=不判。建议长稳时设成 batch 的 1~2 倍")
    args, unknown = ap.parse_known_args()
    args.passthrough = unknown

    # ---- Redis 连接参数：命令行优先，其次 config.ini ----
    # ★ 这里不能把 args 直接传给 redis_kwargs：本脚本的 --port 默认值是 0，
    #   而 config.redis_kwargs 的 pick() 判据是 `v is not None and v != ""`，
    #   0 能通过 -> 端口被覆盖成 0，连接全失败（monitor 静默返回 None）。
    #   所以先只读 config，再在下面按需覆盖。
    try:
        import config as cfgmod
        cp = cfgmod.load()
        kw = cfgmod.redis_kwargs(cp)
    except Exception:
        kw = {"host": "192.168.1.137", "port": 6379,
              "password": "QianLong@2026&", "db": 0}
    if args.host:
        kw["host"] = args.host
    if args.port:
        kw["port"] = args.port
    if args.pwd:
        kw["password"] = args.pwd
    if args.db is not None:
        kw["db"] = args.db

    # ---- 目标流 ----
    stream = args.stream
    if not stream:
        if args.assign_id is None:
            print("[FAIL] 必须给 --assign-id 或 --stream"
                  "（目标流 = ST-<编号>）")
            return 1
        stream = "ST-%d" % args.assign_id

    probe = StreamProbe(kw, stream)

    root = args.soak_out or os.path.join(BASE_DIR, "out", "soak")
    run_id = time.strftime("%Y%m%d_%H%M%S")
    prefix = "soak_%s_%s" % (args.interface, run_id)
    os.makedirs(root, exist_ok=True)
    rounds_dir = os.path.join(root, prefix + "_rounds")
    os.makedirs(rounds_dir, exist_ok=True)

    trend_path = os.path.join(root, prefix + "_trend.csv")
    soak_log_path = os.path.join(root, prefix + "_soak.log")
    summary_path = os.path.join(root, prefix + "_summary.json")
    _log_fp = open(soak_log_path, "w", encoding="utf-8")

    def log(msg):
        line = "[%s] %s" % (_now_str(), msg)
        print(line, flush=True)
        _log_fp.write(line + "\n")
        _log_fp.flush()

    # ---- 轮换用的行号池：只取【匹配 --type】的行 ----
    rotate_rows = []
    if args.rotate:
        excel = os.path.join(BASE_DIR, "data", "%s.xlsx" % args.interface)
        want = None
        if args.type and args.type != "all":
            want = {t.strip().lower() for t in args.type.split(",") if t.strip()}
        rotate_rows = _rows_of_type(excel, args.type, want)

    by_rounds = args.rounds > 0
    deadline = None if by_rounds else time.time() + args.hours * 3600
    target_desc = ("轮数=%d" % args.rounds) if by_rounds else ("时长=%sh" % args.hours)

    log("稳定性测试开始: 接口=%s 类型=%s 目标流=%s %s 每轮=%d 清理=%s 轮间隔=%ss"
        % (args.interface, args.type, stream, target_desc, args.batch,
           args.clean, args.gap))
    log("Redis=%s:%s db=%s  输出目录=%s（前缀 %s）"
        % (kw["host"], kw["port"], kw["db"], root, prefix))
    if args.rotate:
        log("用例轮换: %s.xlsx 中 type=%s 共 %d 行可轮换%s"
            % (args.interface, args.type, len(rotate_rows),
               "（每轮取一段，末尾回绕）" if rotate_rows else ""))
        if not rotate_rows:
            log("⚠ 该类型没有可轮换的行（--rotate 失效，将每轮发同一批）")
    if args.passthrough:
        log("透传 send_test.py 参数: %s" % " ".join(args.passthrough))
    if args.clean == "per-round":
        log("⚠ --clean per-round：会清空回包流 %s。该流是多条 ST-* 共用的，"
            "非独占环境请改用 monitor" % REPLY_STREAM)

    # ---- 开跑前先看一眼目标流（解释性信息，不拦） ----
    xl0 = probe.xlen(stream)
    rd0, pd0, lg0 = probe.group_state(stream)
    log("开跑前目标流: XLEN=%s 已读=%s 未ACK=%s lag=%s"
        % (xl0, rd0, pd0, lg0))
    if rd0 is None:
        log("提示：%s 上还没有消费组 %s —— 平台可能没在读这条流。"
            "这时每轮都会收不到回包（不等于平台挂了）" % (stream, GROUP))

    header = ["轮次", "时间", "发送数", "回包数", "发送失败", "超时未回", "在途",
              "回复率%", "发送速率(条/s)",
              "延迟p50(ms)", "延迟p99(ms)", "延迟max(ms)",
              "目标流XLEN", "已读", "未ACK", "lag", "回复流XLEN",
              "CPU%", "异常"]
    with open(trend_path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow(header)

    totals = {"rounds": 0, "abnormal": 0, "skipped": 0,
              "sent": 0, "reply": 0, "send_fail": 0, "timeout_reply": 0}
    abnormal_rounds = []
    round_no = 0
    start_ts = time.time()

    try:
        while True:
            if by_rounds:
                if round_no >= args.rounds:
                    break
            elif time.time() >= deadline:
                break
            round_no += 1
            remain = ("剩余 %d 轮" % (args.rounds - round_no)) if by_rounds \
                else ("剩余 %.1f 分钟" % ((deadline - time.time()) / 60.0))
            log("--- 第 %d 轮开始（%s）---" % (round_no, remain))

            round_dir = os.path.join(rounds_dir, "r%05d" % round_no)
            cases_spec = None
            if args.rotate and rotate_rows:
                cases_spec = _rotate_spec(rotate_rows,
                                          (round_no - 1) * args.batch, args.batch)

            t0 = time.time()
            xlen = rd = pd = lag = rxlen = None
            row = None
            try:
                stats, err, tail = run_round(args, round_no, round_dir, cases_spec)
                dt = time.time() - t0

                xlen = probe.xlen(stream)
                rd, pd, lag = probe.group_state(stream)
                rxlen = probe.xlen(REPLY_STREAM)

                if stats is None:
                    # 这两类不是"系统异常"，是配置/数据问题，不该污染趋势
                    if err == "SAFETY_GATE":
                        log("第 %d 轮被安全闸拦下（目标流上有真平台消费者）。"
                            "确认无害后加 --force-live（会透传给 send_test）" % round_no)
                        totals["skipped"] += 1
                        continue
                    if err == "NO_CASES":
                        log("第 %d 轮跳过（该轮用例不匹配 --type 过滤）" % round_no)
                        totals["skipped"] += 1
                        continue
                    log("第 %d 轮失败: %s（耗时 %.1fs）" % (round_no, err, dt))
                    totals["rounds"] += 1
                    totals["abnormal"] += 1
                    abnormal_rounds.append(round_no)
                    row = [round_no, _now_str()] + [""] * 15 + \
                          [rxlen, "", "ERR:%s" % err]
                else:
                    sent = _int_or(stats.get("sent"))
                    reply = _int_or(stats.get("reply"))
                    fail = _int_or(stats.get("send_fail"))
                    tout = _int_or(stats.get("timeout_reply"))
                    outn = _int_or(stats.get("outstanding"))
                    rep_rate = (reply / float(sent) * 100.0) if sent else 0.0
                    sps = _float_or(stats.get("send_per_sec"))

                    # ---- 异常判定（策略方向）----
                    reasons = []
                    if fail > 0:
                        reasons.append("发送失败%d" % fail)
                    if args.min_reply_rate > 0 and sent and rep_rate < args.min_reply_rate:
                        reasons.append("回复率%.1f%%<%.1f%%" % (rep_rate, args.min_reply_rate))
                    if args.max_pending > 0 and pd is not None and pd > args.max_pending:
                        reasons.append("未ACK%d>%d" % (pd, args.max_pending))
                    if args.max_lag > 0 and lag is not None and lag > args.max_lag:
                        reasons.append("lag%d>%d" % (lag, args.max_lag))
                    bad = bool(reasons)

                    totals["rounds"] += 1
                    totals["sent"] += sent
                    totals["reply"] += reply
                    totals["send_fail"] += fail
                    totals["timeout_reply"] += tout
                    if bad:
                        totals["abnormal"] += 1
                        abnormal_rounds.append(round_no)

                    row = [round_no, _now_str(), sent, reply, fail, tout, outn,
                           round(rep_rate, 2), round(sps, 1),
                           round(_float_or(stats.get("lat_p50_ms")), 2),
                           round(_float_or(stats.get("lat_p99_ms")), 2),
                           round(_float_or(stats.get("lat_max_ms")), 2),
                           xlen, rd, pd, lag, rxlen,
                           round(_float_or((stats.get("cpu") or {}).get("proc_percent")), 2),
                           ",".join(reasons)]
                    log("第 %d 轮完成: 发送=%d 回包=%d 失败=%d 回复率=%.1f%% "
                        "耗时=%.1fs | 目标流 XLEN=%s 已读=%s 未ACK=%s lag=%s"
                        % (round_no, sent, reply, fail, rep_rate, dt,
                           xlen, rd, pd, lag)
                        + ("  [异常: %s]" % ",".join(reasons) if bad else ""))

                    if not args.keep_round_stats and not bad:
                        try:
                            for fn in os.listdir(round_dir):
                                os.remove(os.path.join(round_dir, fn))
                        except Exception:
                            pass
            except Exception as e:
                # 本轮意外 -> 只记异常，继续下一轮（绝不 re-raise）。
                # 0919 事故教训：一轮的脏数据干掉了跑了 21.5h 的任务。
                dt = time.time() - t0
                totals["rounds"] += 1
                totals["abnormal"] += 1
                abnormal_rounds.append(round_no)
                log("第 %d 轮异常（已隔离，继续后续轮次）: %s: %s（耗时 %.1fs）"
                    % (round_no, type(e).__name__, e, dt))
                row = [round_no, _now_str()] + [""] * 15 + \
                      [rxlen, "", "EXC:%s: %s" % (type(e).__name__, e)]

            with open(trend_path, "a", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(row)

            if args.clean == "per-round":
                ok = probe.clean_reply(REPLY_STREAM)
                log("清理回包流 %s: %s" % (REPLY_STREAM, "OK" if ok else "失败"))

            if args.gap > 0:
                time.sleep(args.gap)
    except KeyboardInterrupt:
        log("收到中断信号，提前结束...")
    finally:
        elapsed = time.time() - start_ts
        summary = {
            "interface": args.interface,
            "type": args.type,
            "stream": stream,
            "run_id": run_id,
            "mode": "rounds" if by_rounds else "hours",
            "rounds_planned": args.rounds if by_rounds else None,
            "hours_planned": None if by_rounds else args.hours,
            "batch": args.batch,
            "clean_mode": args.clean,
            "rotate": bool(args.rotate),
            "rounds_total": totals["rounds"],
            "rounds_abnormal": totals["abnormal"],
            "rounds_skipped": totals["skipped"],
            "abnormal_rounds": abnormal_rounds,
            "sent_total": totals["sent"],
            "reply_total": totals["reply"],
            "send_fail_total": totals["send_fail"],
            "timeout_reply_total": totals["timeout_reply"],
            "reply_rate%": (round(totals["reply"] / float(totals["sent"]) * 100, 2)
                            if totals["sent"] else "N/A"),
            "elapsed_s": round(elapsed, 1),
        }
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        log("稳定性测试结束，汇总:")
        for k, v in summary.items():
            log("  %s: %s" % (k, v))
        log("趋势文件: %s" % trend_path)
        try:
            _log_fp.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
