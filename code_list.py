# -*- coding: utf-8 -*-
"""
code_list.py —— 行情 code 列表（日志）→ csv
==========================================
同事给的是策略服务器自己往外打的日志，一行一条行情推送：

    CHQSHOpt::OnPushData code=10012532 latest price=873 DateTimeStamp=111031410

字段含义：
  * `code`          8 位期权合约代码（1001xxxx = 沪市 50ETF 期权）
  * `latest price`  最新价，**整数，单位 0.0001 元**（873 → 0.0873 元）
  * `DateTimeStamp` 9 位 HHMMSSmmm（111031410 → 11:10:31.410）

★ 这个文件**不是**干净的 code 列表，有两个坑：
  1) 同一个 code 会反复出现（每来一笔行情就打一行）——
     实测 1057 行只有 466 个不同 code，是 4 个时点的增量快照。
     ⇒ 必须按 code 去重，取 DateTimeStamp 最大的那条（最新价）。
  2) 中间夹着几行**乱码分隔行**：`µ±ǰѐʱ¼⸠11:03:50`。
     那是对方工具用 GBK 打的"当前保存时间"被 UTF-8 二次编码的产物，
     与本任务无关 ⇒ 直接跳过，但要把跳过的行数报出来（免得以为解析漏了）。

本脚本只做三件事：解析 → 按 code 去重取最新 → 写成 csv。
之后 `make_excel.py --codes-file <那个csv>` 就能按 code 生成条件单。

用法：
    python code_list.py --txt latestprice_SH.txt
    python code_list.py --txt latestprice_SH.txt --csv data/codes_SH.csv
    python code_list.py --txt latestprice_SH.txt --list        # 只看统计，不写文件

产物 csv 列（make_excel 只认 code / price_yuan / exchange，其余是给人看的）：

    code,price_raw,price_yuan,exchange,timestamp
    10011425,1308,0.1308,1,111031410

  * price_raw  原始整数价（0.0001 元为单位），保真，便于对账
  * price_yuan 换算成元的字符串（4 位小数），生成报文时用这个
  * exchange   交易所编号（1 沪 / 2 深），默认按文件名里的 _SH/_SZ 自动判
  * timestamp  该 code 最新一条行情的时间戳（原样保留）
"""
import argparse
import os
import re
import sys

# Windows 控制台默认 GBK，而本文件要打印"跳过的乱码行"（本身就是非 GBK 字符），
# 不切 UTF-8 会 UnicodeEncodeError 直接崩（check_env.py 同样处理）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# 一行行情推送。刻意**不**匹配 "CHQSHOpt::" 前缀：
# 这样沪深（CHQSHOpt / CHQSZOpt）、以及将来别的品种日志都能用同一套规则。
LINE_RE = re.compile(
    r"code=(\d+)\s+latest\s+price=(-?\d+)\s+DateTimeStamp=(\d+)")

# 价格单位：日志里的整数 = 0.0001 元
PRICE_UNIT = 10000.0


def guess_exchange(path):
    """从文件名猜交易所：1=沪 2=深（口径同 account.py 的股东号样本）。

    只认明确的 SH/SZ 标记，认不出返回 1（当前现场是沪市）。
    """
    name = os.path.basename(path).upper()
    if "_SZ" in name or "SZ" in name:
        return 2
    return 1


def parse_code_log(path):
    """解析日志文件。

    返回 (quotes, bad_lines)：
      quotes    [{code, price_raw, price_yuan, timestamp}, ...]，按 code 排序
      bad_lines [(行号, 原文), ...]，解析不了的行（乱码分隔行等）
    """
    if not os.path.exists(path):
        raise IOError("找不到文件：%s" % path)

    # errors="replace"：乱码行是合法 UTF-8（是**二次编码**的结果），正常读没问题；
    # 但万一碰上真坏字节，也别让整个文件读不出来 —— 那些行反正会被跳过。
    with open(path, encoding="utf-8", errors="replace") as f:
        raw_lines = f.read().splitlines()

    latest = {}          # code -> (timestamp, price_raw)
    bad_lines = []
    for i, line in enumerate(raw_lines, 1):
        s = line.strip()
        if not s:
            continue
        m = LINE_RE.search(s)
        if not m:
            bad_lines.append((i, s))
            continue
        code, price_raw, ts = m.group(1), int(m.group(2)), int(m.group(3))
        # 同一个 code 出现多次 → 只留时间戳最大的（最新价）
        old = latest.get(code)
        if old is None or ts > old[0]:
            latest[code] = (ts, price_raw)

    quotes = []
    for code in sorted(latest):
        ts, price_raw = latest[code]
        quotes.append({
            "code": code,
            "price_raw": price_raw,
            "price_yuan": "%.4f" % (price_raw / PRICE_UNIT),
            "timestamp": str(ts),
        })
    return quotes, bad_lines


def write_csv(quotes, out_path, exchange=1):
    """写 csv。手写而不用 csv 模块：列固定、没有引号/逗号风险，省得依赖。"""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write("code,price_raw,price_yuan,exchange,timestamp\n")
        for q in quotes:
            f.write("%s,%d,%s,%d,%s\n" % (
                q["code"], q["price_raw"], q["price_yuan"],
                exchange, q["timestamp"]))
    os.replace(tmp, out_path)
    return out_path


def read_csv(path):
    """读回 codes csv（供 make_excel 用）。

    返回 [{code, price_raw, price_yuan, exchange, timestamp}, ...]
    容错：允许只有 code 一列（同事直接手写一个 code 列表也行）；
    允许 # 开头的注释行和空行。
    """
    if not os.path.exists(path):
        raise IOError("找不到 codes 文件：%s" % path)
    rows = []
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        header = None
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = [p.strip() for p in s.split(",")]
            if header is None:
                # 表头：认出来就按名字取，认不出就当它是数据行
                low = [p.lower() for p in parts]
                if "code" in low:
                    header = low
                    continue
            if header:
                rec = dict(zip(header, parts))
                code = (rec.get("code") or "").strip()
                praw = (rec.get("price_raw") or "").strip()
                pyuan = (rec.get("price_yuan") or "").strip()
                ex = (rec.get("exchange") or "").strip()
                ts = (rec.get("timestamp") or "").strip()
            else:
                code = parts[0] if parts else ""
                praw = parts[1] if len(parts) > 1 else ""
                pyuan = parts[2] if len(parts) > 2 else ""
                ex = parts[3] if len(parts) > 3 else ""
                ts = parts[4] if len(parts) > 4 else ""
            code = code.strip()
            if not code:
                continue
            # 价格优先级：price_yuan（已换算）> price_raw（原始整数）> 0
            try:
                price_yuan = float(pyuan) if pyuan else (
                    int(praw) / PRICE_UNIT if praw else 0.0)
            except ValueError:
                price_yuan = 0.0
            try:
                exchange = int(ex) if ex else 1
            except ValueError:
                exchange = 1
            rows.append({"code": code,
                         "price_raw": praw,
                         "price_yuan": "%.4f" % price_yuan,
                         "exchange": exchange,
                         "timestamp": ts})
    # 去重（同一个 code 只留第一条）
    seen, out = set(), []
    for r in rows:
        if r["code"] in seen:
            continue
        seen.add(r["code"])
        out.append(r)
    return out


def _print_stats(quotes, bad_lines, src):
    print("=" * 74)
    print("来源: %s" % src)
    print("=" * 74)
    print("  不同 code : %d" % len(quotes))
    if quotes:
        print("  code 区间 : %s ~ %s" % (quotes[0]["code"], quotes[-1]["code"]))
        prices = [float(q["price_yuan"]) for q in quotes]
        nz = [p for p in prices if p > 0]
        print("  最新价    : %s ~ %s 元" % (min(prices), max(prices)))
        print("  零价 code : %d 个（无成交，生成触发价时走兜底）"
              % (len(prices) - len(nz)))
    if bad_lines:
        print("  跳过的行  : %d 行（非行情行，如乱码的'当前保存时间'）"
              % len(bad_lines))
        for no, txt in bad_lines[:3]:
            print("      第 %d 行: %s" % (no, txt[:60]))
        if len(bad_lines) > 3:
            print("      ...（其余 %d 行略）" % (len(bad_lines) - 3))


def main():
    ap = argparse.ArgumentParser(
        description="行情 code 日志 → csv（去重取最新价）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--txt", required=True, help="同事给的日志文件，如 latestprice_SH.txt")
    ap.add_argument("--csv", default="",
                    help="输出 csv 路径，默认 data/codes_<文件名主干>.csv")
    ap.add_argument("--exchange", type=int, default=0,
                    help="交易所编号 1=沪 2=深；0=按文件名自动判（_SH→1，_SZ→2）")
    ap.add_argument("--list", action="store_true", help="只打印统计，不写文件")
    a = ap.parse_args()

    src = a.txt if os.path.isabs(a.txt) else os.path.join(BASE_DIR, a.txt)
    try:
        quotes, bad = parse_code_log(src)
    except IOError as e:
        sys.exit("[FAIL] %s" % e)
    if not quotes:
        sys.exit("[FAIL] %s 里没解析出任何 code（格式变了？）" % src)

    _print_stats(quotes, bad, src)
    if a.list:
        return 0

    exchange = a.exchange or guess_exchange(src)
    out = a.csv or os.path.join(
        DATA_DIR, "codes_%s.csv" % os.path.splitext(os.path.basename(src))[0])
    if not os.path.isabs(out):
        out = os.path.join(BASE_DIR, out)
    try:
        write_csv(quotes, out, exchange)
    except Exception as e:
        sys.exit("[FAIL] 写 csv 失败: %s" % e)
    print()
    print("[OK] 已写入 %s" % out)
    print("     %d 个 code，exchange=%d" % (len(quotes), exchange))
    print()
    print("     下一步生成条件单表（示例）：")
    print("       python make_excel.py --interface create \\")
    print("           --codes-file %s \\" % os.path.relpath(out, BASE_DIR))
    print("           --cond-mix 1:200,2:600,3:400,4:400,5:400 --accounts 100")
    return 0


if __name__ == "__main__":
    sys.exit(main())
