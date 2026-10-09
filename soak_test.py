# -*- coding: utf-8 -*-
"""
稳定性测试编排（Soak Test）—— 策略方向
========================================================
参考 datahub_test/soak_test.py，但判据按【策略方向】重做。

判据怎么定的（重点，改之前先读完）
----------------------------------
用三种信号，各有各的用途，缺一不可：

1) 回复率（默认【按 --type 分档】）：
   - normal = 99%：压测数据本来就该条条有回包（实测 30/30、2000/2000 全回）。
     低于它说明平台漏处理或回包链路断了。
   - error / destroy / all = 0（不判）：畸形报文大量不回包是【平台的正常行为】
     （平台读了、XACK 了、故意不回 —— 如 Pwd 非法密文的 AD232）。

   ⚠️ 但 destroy 的回包行为【不是固定的】，不能想当然。2026-09-29 实测同一天里：
        10:29  account destroy  发 96 回 0    （平台压根没读，lag=96）
        11:26  account destroy  发 96 回 96   （全回）
     所以"destroy 不回包"只是"默认别误报"，真要盯就显式 --min-reply-rate N。

2) lag / 未ACK（目标流消费组，只读）：区分"没读"和"读了卡住"，这是
   回复率【给不出】的信息 —— 回包为 0 时，lag=96 说明平台没伸手，
   未ACK=96 说明读了但卡死。所以即使开了回复率，也建议同时设这两个阈值。
       lag      = 还没被平台读走的条数（平台不伸手 -> 涨）
       pending  = 读了但没 ACK 的条数（平台读了卡住 -> 涨）

3) 超时未回 / 在途未回（单轮结束时）：回复率的补充 —— 它们能指出
   "这一轮积压了多少"，且不像回复率那样受用例类型影响。

★ 回复率的分子只算【能对上本次发送 request_id 的回包】。
  回包流 DataHub_reply_stream 是多条 ST-* 共用的【全局流】，XREAD 会读到
  别人的回包以及 --sync-probe 自己发的那些。旧实现无条件累加，实测出现过
  "发 1 回 21 (2100%)"、"发 50 回 70 (140%)"、"发 2000 回 2020 (101%)"。
  对的算进 reply，对不上的进 reply_foreign（只展示、不参与判据）。

设计（与 datahub 一致）：不改 send_test.py 的发送逻辑，靠"反复调用 + 汇总"：
  每轮调一次 send_test.py（--max <batch> + --no-run-log），
  读本轮 stats JSON，追加到 trend.csv；
  正常轮的明细清掉、异常轮保留；最后产出 soak.log / trend.csv / summary.json。

用法：
  # 8 小时，每轮 500 条（normal），只监控不清理
  python soak_test.py --assign-id 10 --interface account --type normal \
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

  # 业务流模式（一组 = create→modify→remove）；--mid-gap 在 modify 后插一段停顿
  python soak_test.py --assign-id 10 --flow --type normal \
      --rounds 5 --batch 100 --mid-gap 10

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
#
# ★ 回复率默认值【按 --type 分档】，不是一刀切：
#   - normal：压测数据本来就该条条有回包（实测 30/30、2000/2000 全回），
#     所以默认要求 99% —— 低于它说明平台漏处理或回包链路有问题。
#   - destroy / all / error：畸形报文大量不回包是【平台的正常行为】
#     （平台读了、ACK 了、故意不回），拿回复率判会出一堆假异常，默认不判。
#
#   但要注意：destroy 的回包行为【不是固定的】。2026-09-29 实测同一天里，
#   上午 account destroy 出现过 0/96（平台压根没读），下午 96/96 全回。
#   所以这只是"默认不误报"，真要盯 destroy 的回包请显式给 --min-reply-rate。
DEFAULT_MIN_REPLY_RATE_BY_TYPE = {
    "normal": 99.0,
    "error": 0.0,
    "destroy": 0.0,
    "all": 0.0,
}
DEFAULT_MAX_LAG = 0            # lag  超过它算异常，0=不判
DEFAULT_MAX_PENDING = 0        # 未ACK 超过它算异常，0=不判
DEFAULT_MAX_TIMEOUT_REPLY = 0  # 超时未回超过它算异常，0=不判
DEFAULT_MAX_OUTSTANDING = 0    # 在途未回超过它算异常，0=不判
# 业务失败（回包里的 ErrID != 0）超过它算异常，0=不判。
# ★ 为什么要有它：回包"到了"不等于"办成了"。实测过整批 create 里混着
#   {"ErrID":-5,"ref already inserted"}，而回复率仍是 100% —— 光看回复率
#   会把这种批次判成"全绿"。跑一夜长稳时这个盲区尤其危险。
DEFAULT_MAX_BIZ_FAIL = 0


def default_min_reply_rate_for(type_tag):
    """按用例类型给回复率默认下限（%）。"""
    t = (type_tag or "normal").strip().lower()
    return DEFAULT_MIN_REPLY_RATE_BY_TYPE.get(t, 0.0)


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


def _table_contract_code(excel_path, col="Entrust_ContractCode"):
    """读表里第一行 normal 的合约代码，用于检测"表比参数旧"。

    返回 None 表示读不到（表不存在/没这列/没 normal 行）。
    """
    try:
        from openpyxl import load_workbook
        wb = load_workbook(excel_path, read_only=True, data_only=True)
        try:
            ws = wb.active
            it = ws.iter_rows(values_only=True)
            try:
                header = next(it)
            except StopIteration:
                return None
            import re as _re
            keys = []
            for h in header:
                m = _re.search(r"\(([A-Za-z_][A-Za-z0-9_]*)\)", str(h))
                keys.append(m.group(1) if m else str(h).strip())
            if col not in keys:
                return None
            ci = keys.index(col)
            ti = keys.index("case_type") if "case_type" in keys else None
            for raw in it:
                if raw is None or ci >= len(raw):
                    continue
                if ti is not None and ti < len(raw):
                    t = str(raw[ti]).strip().lower()
                    if t and t != "normal":
                        continue
                v = raw[ci]
                if v not in (None, ""):
                    return str(v).strip()
            return None
        finally:
            wb.close()
    except Exception:
        return None


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


# ==================== 单次发送 ====================
def run_send(args, round_dir, interface, label, cases_spec=None, extra=None):
    """跑一次 send_test.py（指定接口 + 标签 + 额外参数）。

    返回 (stats 或 None, 错误信息, tail)。错误信息为 SQL 式的哨兵串：
      "SAFETY_GATE" / "NO_CASES" / "单轮超时 ..." / "未生成 stats ..."
    """
    os.makedirs(round_dir, exist_ok=True)
    # soak 的强制参数放在透传参数之后，确保覆盖（--max / --stats-out）
    cmd = [sys.executable, SEND_TEST] + list(args.passthrough) + [
        "--interface", interface,
        "--max", str(args.batch),
        "--stats-out", round_dir,
        "--label", label,
        "--no-run-log",
    ]
    if extra:
        cmd += list(extra)
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


def run_round(args, round_no, round_dir, cases_spec=None):
    """单接口模式的「一轮」= 跑一次 send_test.py。"""
    return run_send(args, round_dir, args.interface,
                    "soak_r%05d" % round_no, cases_spec)


# ==================== 业务流模式（create -> modify -> remove）====================
MAKE_EXCEL = os.path.join(BASE_DIR, "make_excel.py")

# 一组业务流的固定顺序。顺序是硬约束（见 docs/guide.md 第 6 节）：
#   account 先能登录 -> create 造单 -> modify 改单 -> remove 删单
# 这里不含 account：它是"每个账号一次"的前置动作，不属于每轮循环的业务量。
FLOW_STAGES = ("create", "modify", "remove")


def flow_desc_of(args):
    """一组业务流的流程描述（带 --mid-gap 时把停顿显式画出来）。

    ★ 日志/汇总里必须写清停顿，否则事后看日志分不清
      "这轮慢" 是平台卡了还是自己停的 —— 停顿会明显拉高本组耗时。
    """
    gap = float(getattr(args, "mid_gap", 0.0) or 0.0)
    if gap > 0:
        return "create → modify → 停 %gs → remove" % gap
    return " → ".join(FLOW_STAGES)


def _flow_codes(args):
    """业务流重生成表时要沿用的行情代码（--contract-code / --target-stock-code）。"""
    return {
        "contract": (getattr(args, "contract_code", "") or "").strip(),
        "target": (getattr(args, "target_stock_code", "") or "").strip(),
    }


def gen_table(interface, count, ref_map="", timeout=900, codes=None):
    """调 make_excel.py 生成/重生成一张表。

    返回 (ok, msg, actual_path)。actual_path 为实际写入的表 ——
    ★ 如果原表被 Excel/WPS 占着，make_excel 会【另存 _v2.xlsx 且原表不变】，
      这时候必须让 soak 报错停下，否则会拿旧表接着发（静默发错数据）。

    codes: {"contract": "...", "target": "..."}（可空）
      ★ 业务流每轮都要重生成 modify/remove 表，必须把行情代码一起传下去，
        否则会把用户生成好的表【悄悄换成默认代码】—— 发出去的合约就变了。
    """
    cmd = [sys.executable, MAKE_EXCEL, "--interface", interface,
           "--bulk-normal", str(count)]
    if ref_map:
        cmd += ["--ref-map", ref_map]
    codes = codes or {}
    if codes.get("contract"):
        cmd += ["--contract-code", codes["contract"]]
    if codes.get("target"):
        cmd += ["--target-stock-code", codes["target"]]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 双保险：make_excel 也认环境变量（它在 import 接口前读）
    if codes.get("contract"):
        env["ST_CONTRACT_CODE"] = codes["contract"]
    if codes.get("target"):
        env["ST_TARGET_STOCK_CODE"] = codes["target"]
    try:
        p = subprocess.run(cmd, cwd=BASE_DIR, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "replace")
    except Exception as e:
        return False, "调 make_excel 失败: %s" % e, ""
    tail = " | ".join(out.strip().splitlines()[-2:]) if out.strip() else ""
    if p.returncode != 0:
        return False, "make_excel 退出码 %s: %s" % (p.returncode, tail), ""
    # ★ 原表被占用：make_excel 退出码仍是 0，但原表没更新（会另存 _v2）
    if "原文件没有更新" in out:
        return False, "目标表被 Excel/WPS 占用，原表没更新（新内容另存成了 _v2）：%s" % tail, ""
    if interface in ("modify", "remove"):
        if "[ERROR]" in out:
            return False, "ref 回填失败（全部没命中，发出去会 ref not exist）：%s" % tail, ""
        if "[WARN]" in out and "账号未在 refs 映射里" in out:
            return False, "ref 回填部分未命中：%s" % tail, ""
    return True, tail, os.path.join(BASE_DIR, "data", "%s.xlsx" % interface)


def ensure_create_table(args, log):
    """业务流跑之前，确保 create.xlsx 有 >= batch 行 normal。

    create 的 Ref 是自生成的（__REF{i}__ 当天展开），不依赖 refs.json，
    所以它的表【只需生成一次】；modify/remove 才要每轮按新 refs 重生成。

    ★ 但行情代码（--contract-code）是【写死在表里的字面量】：
      只判行数会漏掉"表是旧的、用的还是上一天的合约"这种情况 ——
      实测踩过：表有 10000 行就直接放行，结果 create 发出去的合约
      与 modify 不是同一个（用户在 GUI 改了代码也不生效）。
      所以这里同时核对表里的合约代码，不一致就重新生成。
    """
    excel = os.path.join(BASE_DIR, "data", "create.xlsx")
    n = len(_rows_of_type(excel, "normal", {"normal"}))
    want = (_flow_codes(args) or {}).get("contract") or ""
    have = _table_contract_code(excel) if want else None
    if want and have and have != want:
        log("create.xlsx 里的合约是 %s，与本次 --contract-code %s 不一致 —— 重新生成"
            % (have, want))
    elif n >= args.batch:
        log("create.xlsx 已有 %d 行 normal（>= 每轮 %d），无需重新生成"
            % (n, args.batch))
        return True
    if n < args.batch:
        log("create.xlsx 只有 %d 行 normal，少于每轮 %d —— 自动生成 --bulk-normal %d"
            % (n, args.batch, args.batch))
    # 重新生成时按 max(现有行数, 每轮条数) 生成：
    # 不能只用 args.batch，否则会把 1 万行的表缩成几十行（用户还得再生成一次）。
    ok, msg, _ = gen_table("create", max(n, args.batch), codes=_flow_codes(args))
    log("  生成 create.xlsx: %s%s" % ("OK" if ok else "失败", "" if ok else " " + msg))
    return ok


def run_flow_cycle(args, round_no, cycle_dir, refs_path, log, on_step=None):
    """跑【一组】业务流：create -> 生成 modify 表 -> modify -> [停顿] -> 生成 remove 表 -> remove。

    on_step(stage, stats, dt) 每完成【一次发送】就回调一次，便于逐步写趋势与进度
    （一组可能有 3×batch 条，等整组跑完才输出会看不到进度）。
    生成表不是"发送"，不走 on_step，只写日志。

    返回中止原因（"" = 整组跑完）。任何一步失败都中止本组：
    create 失败 -> 没有单号，后面无从谈起；生成表失败 -> 拒绝拿旧表发送。

    ★ 为什么 modify/remove 表要每轮重生成：
      Ref 是【当天日期+序号】，同一轮里展开值固定（如 __REF1__ -> 20260929000001）。
      上一轮的 remove 真删掉之后，这一轮 create 才能重新造出同样的号。
      所以每轮必须拿【本轮 create 刚回包的真实单号】重新回填 modify/remove，
      跨天或换号段时也不会错。

    ★ --mid-gap（停顿）：
      在 modify 发完之后、remove 发之前停 N 秒。用于"改单后放一会儿再删单"
      这类时序场景。停顿计入本组耗时（和 --gap 一样）。
      位置刻意选在 modify 之后【紧邻】处，这样"modify 收工 -> remove 开工"
      之间是真的静默 N 秒（生成 remove 表是本地动作，另计）。
    """
    os.makedirs(cycle_dir, exist_ok=True)

    def emit(stage, stats, t0):
        if on_step:
            on_step(stage, stats, time.time() - t0)

    # ---- 1) create：造单，并从回包抓真实单号落盘 ----
    t0 = time.time()
    stats, err, _ = run_send(args, cycle_dir, "create",
                             "soak_c%05d_create" % round_no,
                             extra=["--refs-out", refs_path])
    emit("create", stats, t0)
    if stats is None:
        return "create 未成功（%s），本组中止（后续 modify/remove 无单号可用）" % err

    # ---- 2) modify：用刚抓到的真实单号重生成表 ----
    if not os.path.exists(refs_path):
        log("  生成 modify 表: 失败 —— 没抓到 refs.json（create 没有返回单号）")
        return "create 没有返回任何 Ref，无法生成 modify"
    try:
        with open(refs_path, encoding="utf-8") as f:
            n_refs = len(json.load(f))
    except Exception as e:
        return "读 refs.json 失败: %s" % e
    if n_refs <= 0:
        return "refs.json 里没有单号"
    log("  create 抓到 %d 个真实单号 -> %s" % (n_refs, os.path.basename(refs_path)))

    t0 = time.time()
    ok, msg, _ = gen_table("modify", args.batch, ref_map=refs_path,
                           codes=_flow_codes(args))
    log("  生成 modify 表: %s（%.1fs）%s"
        % ("OK" if ok else "失败", time.time() - t0, "" if ok else " " + msg))
    if not ok:
        return "生成 modify 表失败: %s" % msg

    t0 = time.time()
    stats, err, _ = run_send(args, cycle_dir, "modify",
                             "soak_c%05d_modify" % round_no)
    emit("modify", stats, t0)
    if stats is None:
        return "modify 未成功（%s），本组中止（为保证「删干净」不再发 remove）" % err

    # ---- 2.5) 中场停顿（--mid-gap）：modify 收工后静默 N 秒再发 remove ----
    # 放在这里而不是放在 gen_table 之后：这段时间内【完全不碰 Redis】，
    # 是一个连续的静默窗口，最贴合「改单后停一会儿再删单」的意图。
    # （生成 remove 表是纯本地 Excel 动作，不影响平台侧看到的间隔。）
    mid_gap = float(getattr(args, "mid_gap", 0.0) or 0.0)
    if mid_gap > 0:
        log("  中场停顿 %.1fs（modify 已发完，等平台处理后再发 remove）" % mid_gap)
        time.sleep(mid_gap)

    # ---- 3) remove：同一批单号，删掉（删干净下一轮 create 才能重建同名号）----
    t0 = time.time()
    ok, msg, _ = gen_table("remove", args.batch, ref_map=refs_path,
                           codes=_flow_codes(args))
    log("  生成 remove 表: %s（%.1fs）%s"
        % ("OK" if ok else "失败", time.time() - t0, "" if ok else " " + msg))
    if not ok:
        return "生成 remove 表失败: %s" % msg

    t0 = time.time()
    stats, err, _ = run_send(args, cycle_dir, "remove",
                             "soak_c%05d_remove" % round_no)
    emit("remove", stats, t0)
    if stats is None:
        return "remove 未成功（%s）—— 上一轮的单没删掉，下一轮 create 可能 ref already inserted" % err
    return ""


# ==================== 判据（两种模式共用） ====================
def judge(stats, args, pd=None, lag=None):
    """按当前阈值判一次发送是否异常，返回 reasons 列表（空=正常）。

    抽出来是为了让「单接口模式」和「业务流模式」用【同一套判据】，
    不会出现两处各判一套、标准还不一致的情况。
    """
    reasons = []
    if stats is None:
        return reasons
    sent = _int_or(stats.get("sent"))
    reply = _int_or(stats.get("reply"))
    fail = _int_or(stats.get("send_fail"))
    tout = _int_or(stats.get("timeout_reply"))
    outn = _int_or(stats.get("outstanding"))
    rr = _as_num(stats.get("reply_rate"))
    rep_rate = (rr * 100.0) if rr is not None else \
        ((reply / float(sent) * 100.0) if sent else 0.0)
    if fail > 0:
        reasons.append("发送失败%d" % fail)
    if args.min_reply_rate > 0 and sent and rep_rate < args.min_reply_rate:
        reasons.append("回复率%.1f%%<%.1f%%" % (rep_rate, args.min_reply_rate))
    if args.max_pending > 0 and pd is not None and pd > args.max_pending:
        reasons.append("未ACK%d>%d" % (pd, args.max_pending))
    if args.max_lag > 0 and lag is not None and lag > args.max_lag:
        reasons.append("lag%d>%d" % (lag, args.max_lag))
    if args.max_timeout_reply > 0 and tout > args.max_timeout_reply:
        reasons.append("超时未回%d>%d" % (tout, args.max_timeout_reply))
    if args.max_outstanding > 0 and outn > args.max_outstanding:
        reasons.append("在途%d>%d" % (outn, args.max_outstanding))
    # 业务失败：回复率看不出它（回包到了仍可能被平台拒）
    # 语义：< 0 = 零容忍（一条都不许失败）；0 = 不判；N = 超过 N 条才判
    biz = _int_or(stats.get("biz_fail"))
    if biz > 0 and args.max_biz_fail != 0:
        msgs = stats.get("biz_fail_msgs") or {}
        top = ""
        if isinstance(msgs, dict) and msgs:
            k = max(msgs, key=lambda x: msgs[x])
            top = "(%s×%d)" % (k, msgs[k])
        if args.max_biz_fail < 0:
            reasons.append("业务失败%d(零容忍)%s" % (biz, top))
        elif biz > args.max_biz_fail:
            reasons.append("业务失败%d>%d%s" % (biz, args.max_biz_fail, top))
    return reasons


def _biz_of(stats):
    """取该次发送的业务结果三元组 (成功数, 失败数, 成功率%)。

    成功率分母是"有 ErrID 的回包"，没有 ErrID 的（纯 Mock {"status":"OK"}）
    既不算成功也不算失败，所以分母为 0 时返回 None（显示成空，不报 0%）。
    """
    if not stats:
        return "", "", ""
    ok = _int_or(stats.get("biz_ok"))
    bad = _int_or(stats.get("biz_fail"))
    r = _as_num(stats.get("biz_rate"))
    rate = round(r * 100.0, 2) if r is not None else ""
    return ok, bad, rate


def _reply_rate(stats):
    """取该次发送的回复率%（恒 <= 100）。"""
    if not stats:
        return 0.0
    rr = _as_num(stats.get("reply_rate"))
    if rr is not None:
        return rr * 100.0
    sent = _int_or(stats.get("sent"))
    return (_int_or(stats.get("reply")) / float(sent) * 100.0) if sent else 0.0


# ==================== 主流程 ====================
def main():
    ap = argparse.ArgumentParser(
        description="稳定性测试编排（复用 send_test.py，按轮持续发送并汇总趋势）",
        epilog="未识别参数会原样透传给 send_test.py")
    ap.add_argument("--interface", required=False, default="",
                    help="接口名：create/modify/remove/pwdUpdate/account。"
                         "★ 用 --flow 时忽略它（一组固定跑 create→modify→remove）")
    ap.add_argument("--flow", action="store_true",
                    help="业务流模式：一组 = create→modify→remove（各 batch 条），"
                         "循环跑。每轮 modify/remove 表会用本轮 create 回包抓到的"
                         "真实单号重新生成（Ref 是动态的，不能预先写死）。"
                         "加 --mid-gap SEC 可变成 create→modify→停SEC秒→remove")
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
    ap.add_argument("--mid-gap", type=float, default=0.0, metavar="SEC",
                    help="业务流模式：modify 发完后停 SEC 秒再发 remove，"
                         "即一组 = create→modify→停SEC秒→remove（默认 0=不停）。"
                         "用来观察「改单后停留一段时间再删单」这种时序场景。"
                         "仅 --flow 下有效")
    ap.add_argument("--clean", choices=["monitor", "per-round"],
                    default="monitor",
                    help="monitor=只监控不清理(默认，最安全)；"
                         "per-round=每轮清空回包流（注意：回包流是多条 ST-* 共用的，"
                         "非独占环境别开）")
    ap.add_argument("--rotate", action="store_true",
                    help="每轮轮换用例（按行号分段、末尾回绕），避免反复发同一批数据")
    ap.add_argument("--keep-round-stats", action="store_true",
                    help="保留每一轮的 stats JSON（默认只留异常轮）")
    ap.add_argument("--round-timeout", type=float, default=600.0,
                    help="单轮最长秒数，防卡死（默认 600）")
    ap.add_argument("--soak-out", default="", help="输出根目录（默认 out/soak）")
    # 行情代码（每天在变）：业务流每轮重生成 modify/remove 表时要沿用同一套代码
    ap.add_argument("--contract-code", default="",
                    help="行情合约代码：业务流重生成 modify/remove 表时传给 make_excel。"
                         "留空则用 make_excel 自己的默认值/环境变量")
    ap.add_argument("--target-stock-code", default="",
                    help="止盈止损标的代码：同上，传给 make_excel")

    # ---- 异常判据（默认：发送失败 + 回复率(按类型) + lag/pending/在途/超时）----
    ap.add_argument("--min-reply-rate", type=float, default=None,
                    help="回复率下限%%，低于它算异常。默认按 --type 自动取："
                         "normal=99，error/destroy/all=0（不判）。"
                         "显式给 0 = 不判。destroy 的回包行为不稳定"
                         "（同一天实测过 0%% 和 100%%），要盯就显式指定")
    ap.add_argument("--max-lag", type=int, default=DEFAULT_MAX_LAG,
                    help="目标流 lag 超过它算异常（平台不读的信号）。默认 0=不判")
    ap.add_argument("--max-pending", type=int, default=DEFAULT_MAX_PENDING,
                    help="目标流未ACK 超过它算异常（平台读了卡住的信号）。"
                         "默认 0=不判。建议长稳时设成 batch 的 1~2 倍")
    ap.add_argument("--max-timeout-reply", type=int,
                    default=DEFAULT_MAX_TIMEOUT_REPLY,
                    help="单轮「超时未回」超过它算异常。默认 0=不判。"
                         "建议设成 batch 的 1~2 倍（跑 normal 时它等价于丢失数）")
    ap.add_argument("--max-outstanding", type=int,
                    default=DEFAULT_MAX_OUTSTANDING,
                    help="单轮结束时「在途未回」超过它算异常。默认 0=不判")
    ap.add_argument("--max-biz-fail", type=int,
                    default=DEFAULT_MAX_BIZ_FAIL,
                    help="单轮【业务失败】(回包 ErrID != 0，如 ref already inserted)"
                         "达到多少条算异常。\n"
                         "  0  = 不判（默认）\n"
                         "  -1 = 零容忍：失败 1 条就报（长稳推荐）\n"
                         "  N  = 超过 N 条才报\n"
                         "★ 回复率看不出业务失败：回包到了仍可能被平台拒。"
                         "实测过回复率 100%% 但 30%% 是 ref already inserted。")
    args, unknown = ap.parse_known_args()
    args.passthrough = unknown

    # ---- 回复率下限：没显式给就按 --type 自动取（normal=99，其余=0）----
    if args.min_reply_rate is None:
        args.min_reply_rate = default_min_reply_rate_for(args.type)

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

    # ---- 参数校验（两种模式）----
    if args.flow:
        # 业务流模式：接口固定是 create/modify/remove，--interface 无意义
        if args.rotate:
            print("[FAIL] --flow 与 --rotate 不能同用："
                  "业务流每轮都要重生成 modify/remove 表，行号轮换无意义")
            return 1
        if not args.batch or args.batch <= 0:
            print("[FAIL] --flow 需要 --batch > 0（每段各发这么多条）")
            return 1
        if args.clean != "monitor":
            print("[FAIL] --flow 只支持 --clean monitor："
                  "业务流靠「上一轮 remove 删干净、下一轮 create 重建」循环，"
                  "清空回包流会干扰 refs 抓取判断")
            return 1
        if args.type.strip().lower() not in ("normal", "all", ""):
            print("[FAIL] --flow 只能配 --type normal（业务流发的是合法报文）；"
                  "当前是 %s" % args.type)
            return 1
        if args.interface:
            print("[提示] --flow 已启用，忽略 --interface=%s（一组固定跑 %s）"
                  % (args.interface, "→".join(FLOW_STAGES)))
    else:
        if args.mid_gap:
            print("[FAIL] --mid-gap 只在业务流模式（--flow）下有效："
                  "单接口模式没有 modify→remove 这个中途位置")
            return 1
        if not args.interface:
            print("[FAIL] 必须给 --interface（或改用 --flow 跑业务流）")
            return 1
        if args.interface not in ("create", "modify", "remove",
                                  "pwdUpdate", "account"):
            print("[FAIL] --interface 只能是 create/modify/remove/pwdUpdate/account")
            return 1

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
    flow_tag = "流" if args.flow else ""
    prefix = "soak_%s%s_%s" % (flow_tag, "create-modify-remove" if args.flow
                               else args.interface, run_id)
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

    if args.flow:
        log("稳定性测试开始（业务流模式）: 一组 = %s 各 %d 条，目标流=%s %s"
            % (flow_desc_of(args), args.batch, stream, target_desc))
        log("每轮 modify/remove 表会用【本轮 create 回包抓到的真实单号】重新生成"
            "（Ref 是动态的，不能预先写死）")
    else:
        log("稳定性测试开始: 接口=%s 类型=%s 目标流=%s %s 每轮=%d 清理=%s 轮间隔=%ss"
            % (args.interface, args.type, stream, target_desc, args.batch,
               args.clean, args.gap))
    mbiz = args.max_biz_fail
    biz_txt = ("不判定" if mbiz == 0
               else ("零容忍(失败1条即报)" if mbiz < 0 else ">%d判定" % mbiz))
    log("判据: 回复率下限=%s  发送失败>0   lag>%s判定   未ACK>%s判定   "
        "超时未回>%s判定   在途>%s判定   业务失败%s"
        % (("%g%%" % args.min_reply_rate) if args.min_reply_rate > 0 else "关",
           args.max_lag or "不", args.max_pending or "不",
           args.max_timeout_reply or "不", args.max_outstanding or "不",
           biz_txt))
    if args.min_reply_rate > 0 and args.type.strip().lower() in ("destroy", "all"):
        log("⚠ 类型=%s 却开了回复率下限：destroy 的回包行为不稳定"
            "（实测同一天出现过 0%% 与 100%%），可能产生假异常" % args.type)
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
    _fc = _flow_codes(args)
    if args.flow and (_fc["contract"] or _fc["target"]):
        log("行情代码（重生成 modify/remove 表时沿用）: 合约=%s  止盈止损标的=%s"
            % (_fc["contract"] or "（make_excel 默认）",
               _fc["target"] or "（make_excel 默认）"))
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

    if args.flow:
        header = ["轮次", "时间", "阶段", "发送数", "回包数", "发送失败", "超时未回",
                  "在途", "回复率%", "非本次回包", "业务成功", "业务失败", "业务成功率%",
                  "发送速率(条/s)",
                  "延迟p50(ms)", "延迟p99(ms)", "延迟max(ms)",
                  "目标流XLEN", "已读", "未ACK", "lag", "回复流XLEN",
                  "CPU%", "异常"]
    else:
        header = ["轮次", "时间", "发送数", "回包数", "发送失败", "超时未回", "在途",
                  "回复率%", "非本次回包", "业务成功", "业务失败", "业务成功率%",
                  "发送速率(条/s)",
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

    def append_row(row):
        with open(trend_path, "a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(row)

    def snapshot_probe():
        """取一次服务端视角的快照（目标流/回包流）。"""
        return (probe.xlen(stream),) + probe.group_state(stream) + \
               (probe.xlen(REPLY_STREAM),)

    def log_result(prefix_txt, sent, reply, fail, rep_rate, dt, xlen, rd, pd, lag,
                   foreign, reasons, biz=None):
        biz_txt = ""
        if biz:
            bok, bbad, brate = biz
            if bok != "" or bbad != "":
                biz_txt = " | 业务: 成功=%s 失败=%s%s" % (
                    bok, bbad, (" 成功率=%s%%" % brate) if brate != "" else "")
        log("%s 发送=%d 回包=%d 失败=%d 回复率=%.1f%%%s 耗时=%.1fs | "
            "目标流 XLEN=%s 已读=%s 未ACK=%s lag=%s"
            % (prefix_txt, sent, reply, fail, rep_rate, biz_txt, dt, xlen, rd, pd, lag)
            + ("  [剔除非本次 %d 条]" % foreign if foreign else "")
            + ("  [异常: %s]" % ",".join(reasons) if reasons else ""))

    # ---- 业务流模式：跑之前先确认 create 表够用（modify/remove 每轮现生成）----
    if args.flow:
        if not ensure_create_table(args, log):
            log("[FAIL] create 表不可用，业务流模式无法开始")
            try:
                _log_fp.close()
            except Exception:
                pass
            return 1

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

            round_dir = os.path.join(rounds_dir, "r%05d" % round_no)
            cases_spec = None
            if not args.flow and args.rotate and rotate_rows:
                cases_spec = _rotate_spec(rotate_rows,
                                          (round_no - 1) * args.batch, args.batch)

            if args.flow:
                log("--- 第 %d 组开始（%s）: %s，各 %d 条 ---"
                    % (round_no, remain, flow_desc_of(args), args.batch))
            else:
                log("--- 第 %d 轮开始（%s）---" % (round_no, remain))

            t0 = time.time()
            xlen = rd = pd = lag = rxlen = None
            row = None
            try:
                if args.flow:
                    # ==================== 业务流：一组 = create→modify→remove ====================
                    g_sent = g_reply = g_fail = g_tout = 0
                    g_abnormal = False
                    g_foreign = 0
                    stage_notes = []

                    def on_step(stage, stats, dt):
                        """每跑完一步（一次发送）就立刻写一行趋势 ——
                        一组可能很久（3×batch 条），等整组跑完才输出会看不到进度。"""
                        nonlocal g_sent, g_reply, g_fail, g_tout, g_abnormal, g_foreign
                        x_, r_, p_, l_, rx_ = snapshot_probe()
                        if stats is None:
                            # 发送失败：算异常，记下来继续下一组
                            # 按表头长度算，避免手写 [""]*N 数错（曾少一列）
                            tail = ["ERR:无 stats"]
                            head = [round_no, _now_str(), stage]
                            append_row(head + [""] * (len(header) - len(head)
                                                      - len(tail)) + tail)
                            log("    第 %d 组 %s: 失败（%.1fs）" % (round_no, stage, dt))
                            g_abnormal = True
                            return
                        sent = _int_or(stats.get("sent"))
                        reply = _int_or(stats.get("reply"))
                        fail = _int_or(stats.get("send_fail"))
                        tout = _int_or(stats.get("timeout_reply"))
                        outn = _int_or(stats.get("outstanding"))
                        foreign = _int_or(stats.get("reply_foreign"))
                        rep_rate = _reply_rate(stats)
                        sps = _float_or(stats.get("send_per_sec"))
                        bok, bbad, brate = _biz_of(stats)
                        # 判据与单接口模式【完全同一套】（共用 judge）
                        reasons = judge(stats, args, pd=p_, lag=l_)
                        g_sent += sent
                        g_reply += reply
                        g_fail += fail
                        g_tout += tout
                        g_foreign += foreign
                        if reasons:
                            g_abnormal = True
                            stage_notes.append("%s:%s" % (stage, ",".join(reasons)))
                        append_row([round_no, _now_str(), stage, sent, reply,
                                    fail, tout, outn, round(rep_rate, 2), foreign,
                                    bok, bbad, brate,
                                    round(sps, 1),
                                    round(_float_or(stats.get("lat_p50_ms")), 2),
                                    round(_float_or(stats.get("lat_p99_ms")), 2),
                                    round(_float_or(stats.get("lat_max_ms")), 2),
                                    x_, r_, p_, l_, rx_,
                                    round(_float_or((stats.get("cpu") or {}
                                                     ).get("proc_percent")), 2),
                                    ",".join(reasons)])
                        log_result("    第 %d 组 %s:" % (round_no, stage),
                                   sent, reply, fail, rep_rate, dt,
                                   x_, r_, p_, l_, foreign, reasons,
                                   biz=(bok, bbad, brate))

                    refs_path = os.path.join(round_dir, "refs.json")
                    abort = run_flow_cycle(args, round_no, round_dir, refs_path,
                                           log, on_step=on_step)
                    dt = time.time() - t0
                    xlen, rd, pd, lag, rxlen = snapshot_probe()

                    totals["rounds"] += 1
                    totals["sent"] += g_sent
                    totals["reply"] += g_reply
                    totals["send_fail"] += g_fail
                    totals["timeout_reply"] += g_tout
                    if abort:
                        g_abnormal = True
                        stage_notes.append(abort)
                    if g_abnormal:
                        totals["abnormal"] += 1
                        abnormal_rounds.append(round_no)
                        log("第 %d 组异常: %s（耗时 %.1fs）"
                            % (round_no, "；".join(stage_notes), dt))
                        if abort:
                            # create 失败会让后面全废；继续下一组（组间是独立的）
                            log("    （本组已中止，下一组重新开始）")
                    else:
                        log("第 %d 组完成: 三段共发送=%d 回包=%d（耗时 %.1fs）"
                            % (round_no, g_sent, g_reply, dt))

                    if not args.keep_round_stats and not g_abnormal:
                        try:
                            for fn in os.listdir(round_dir):
                                os.remove(os.path.join(round_dir, fn))
                        except Exception:
                            pass
                    row = None          # 已在 on_step 里逐行写过
                else:
                    # ==================== 单接口：一轮 = 一次发送 ====================
                    stats, err, tail = run_round(args, round_no, round_dir, cases_spec)
                    dt = time.time() - t0

                    xlen, rd, pd, lag, rxlen = snapshot_probe()

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
                        # 按表头长度算，避免手写 [""]*N 数错（曾少一列）
                        tail = [rxlen, "", "ERR:%s" % err]
                        head = [round_no, _now_str()]
                        row = head + [""] * (len(header) - len(head) - len(tail)) + tail
                    else:
                        sent = _int_or(stats.get("sent"))
                        reply = _int_or(stats.get("reply"))
                        fail = _int_or(stats.get("send_fail"))
                        tout = _int_or(stats.get("timeout_reply"))
                        outn = _int_or(stats.get("outstanding"))
                        foreign = _int_or(stats.get("reply_foreign"))
                        rep_rate = _reply_rate(stats)
                        sps = _float_or(stats.get("send_per_sec"))
                        bok, bbad, brate = _biz_of(stats)
                        # 判据统一走 judge()（与业务流模式同一套）
                        reasons = judge(stats, args, pd=pd, lag=lag)
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
                               round(rep_rate, 2), foreign, bok, bbad, brate,
                               round(sps, 1),
                               round(_float_or(stats.get("lat_p50_ms")), 2),
                               round(_float_or(stats.get("lat_p99_ms")), 2),
                               round(_float_or(stats.get("lat_max_ms")), 2),
                               xlen, rd, pd, lag, rxlen,
                               round(_float_or((stats.get("cpu") or {}).get("proc_percent")), 2),
                               ",".join(reasons)]
                        log_result("第 %d 轮完成:" % round_no, sent, reply, fail,
                                   rep_rate, dt, xlen, rd, pd, lag, foreign, reasons,
                                   biz=(bok, bbad, brate))

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
                # 异常行：除了前两列(轮次/时间)、业务流多一列(阶段)、
                # 以及末尾三列(回复流XLEN/CPU/异常)，中间全空。
                # 直接按表头长度算，避免手写 [""]*N 数错（曾少一列）。
                tail = [rxlen, "", "EXC:%s: %s" % (type(e).__name__, e)]
                head = [round_no, _now_str()] + (["(异常)"] if args.flow else [])
                row = head + [""] * (len(header) - len(head) - len(tail)) + tail

            if row is not None:
                append_row(row)

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
            "mode_flow": bool(args.flow),
            "flow_stages": list(FLOW_STAGES) if args.flow else None,
            "flow_desc": flow_desc_of(args) if args.flow else None,
            "mid_gap_s": (float(args.mid_gap) if args.flow else None),
            "interface": "(业务流 create→modify→remove)" if args.flow else args.interface,
            "type": args.type,
            "stream": stream,
            "run_id": run_id,
            "mode": "rounds" if by_rounds else "hours",
            "rounds_planned": args.rounds if by_rounds else None,
            "hours_planned": None if by_rounds else args.hours,
            "batch": args.batch,
            "clean_mode": args.clean,
            "rotate": bool(args.rotate),
            # ---- 本轮实际生效的判据（事后复盘必须知道"用什么判的"）----
            "criteria": {
                "min_reply_rate%": args.min_reply_rate,
                "max_lag": args.max_lag,
                "max_pending": args.max_pending,
                "max_timeout_reply": args.max_timeout_reply,
                "max_outstanding": args.max_outstanding,
                "max_biz_fail": args.max_biz_fail,
            },
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
