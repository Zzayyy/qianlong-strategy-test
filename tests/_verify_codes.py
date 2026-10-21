# -*- coding: utf-8 -*-
"""校验「按行情 code 列表生成的条件单表」（--codes-file）。

只读 data/create.xlsx，不发送。这是发之前最后一道自动挡板：
表一旦生成错误，2000 条真实条件单就白白注册（还可能把不该触发的单触发了）。

检查点（每条都对应一个具体的踩坑点）：
  A. 结构    normal 条数 == 配比总和；error/destroy 原样保留
  B. 配比    每个 CondType 的条数与 --cond-mix 完全一致
  C. 覆盖    所有 code 都出现过；每 code 出现次数尽量均匀
  D. 账号    轮换账号数 == --accounts；UniqueAccount 跟随 FAccount
  E. 交易所  所有块的 ExchangeNum 都等于期望值（SH=1）
  F. 代码    Entrust / CfgExceed / CondPrice / CondPercent / CondTime /
            CondLoss / CondProfit 七处的 code 全部等于「本行的 code」
            ★ CondLoss/CondProfit 必须同步（用户 2026-10-20 明确要求）
  G. 单号    Ref 逐行唯一且为 __REFn__ 或 14 位数字
  H. 触发价  broken 模式下 CondPrice 触发价必须低于本行现价（否则永远不触发）
  I. 报文    build_payload 出来的结构完整，能被 json 解析

用法：
    python tests/_verify_codes.py                     # 用默认期望值校验
    python tests/_verify_codes.py --expect 1:200,2:600,3:400,4:400,5:400
    python tests/_verify_codes.py --excel data/create_v2.xlsx --accounts 100
    python tests/_verify_codes.py --codes data/codes_latestprice_SH.csv
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "interfaces"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import code_list

OK = FAIL = 0
FAILS = []


def check(cond, label, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  [OK]   %s%s" % (label, ("  " + str(extra)) if extra else ""))
    else:
        FAIL += 1
        FAILS.append(label)
        print("  [FAIL] %s%s" % (label, ("  " + str(extra)) if extra else ""))


def read_excel(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        header = [re.search(r"\(([A-Za-z_][A-Za-z0-9_]*)\)", str(h)).group(1)
                  for h in next(it)]
        out = []
        for r in it:
            if r is None or all(v is None or str(v).strip() == "" for v in r):
                continue
            out.append(dict(zip(header, r)))
        return out
    finally:
        wb.close()


# 必须等于「本行 code」的嵌套块（ContractCode 或 StockCode）
CODE_BLOCKS = ("Entrust", "CfgExceedPrice", "CondPrice", "CondPercent",
               "CondTime", "CondLoss", "CondProfit")
# 比较强的两组：止盈止损标的是【标的代码】，不等于期权 code，单独看
TARGET_BLOCKS = ("CondTargetLoss", "CondTargetProfit")
EXCHANGE_BLOCKS = ("Entrust", "CfgExceedPrice", "CondPrice", "CondPercent",
                   "CondTime", "CondLoss", "CondTargetLoss", "CondProfit",
                   "CondTargetProfit")


def code_of(block):
    return block.get("ContractCode") or block.get("StockCode") or ""


def main():
    ap = argparse.ArgumentParser(description="校验按 code 列表生成的条件单表")
    ap.add_argument("--excel", default=os.path.join(ROOT, "data", "create.xlsx"))
    ap.add_argument("--codes", default=os.path.join(
        ROOT, "data", "codes_latestprice_SH.csv"))
    ap.add_argument("--expect", default="1:200,2:600,3:400,4:400,5:400",
                    help="期望的 CondType 配比")
    ap.add_argument("--accounts", type=int, default=100)
    ap.add_argument("--exchange", type=int, default=1)
    ap.add_argument("--trigger", default="broken", choices=("broken", "safe"))
    a = ap.parse_args()

    import create as C
    if not os.path.exists(a.excel):
        sys.exit("[FAIL] 找不到 %s（先用 make_excel.py --codes-file 生成）"
                 % a.excel)
    if not os.path.exists(a.codes):
        sys.exit("[FAIL] 找不到 codes 文件 %s（先跑 code_list.py）" % a.codes)

    quotes = code_list.read_csv(a.codes)
    price = {q["code"]: float(q["price_yuan"]) for q in quotes}
    rows = read_excel(a.excel)
    types = Counter(str(r.get("case_type")).strip() for r in rows)
    normals = [r for r in rows if str(r.get("case_type")).strip() == "normal"]
    expect = dict(C.parse_cond_mix(a.expect))
    total_expect = sum(expect.values())

    print("=" * 74)
    print("表: %s" % a.excel)
    print("codes: %s  (%d 个)" % (a.codes, len(quotes)))
    print("=" * 74)

    # ---- A. 结构 ----
    print("\nA) 结构")
    check(len(normals) == total_expect,
          "normal 条数 == 配比总和 %d" % total_expect, len(normals))
    check(types.get("error", 0) == 9, "error 原样保留 9 条", types.get("error", 0))
    check(types.get("destroy", 0) == 287, "destroy 原样保留 287 条",
          types.get("destroy", 0))

    # ---- 逐行解析报文 ----
    bodies = []
    bad_json = 0
    for r in normals:
        try:
            b = C.build_payload(r)["create"]
            if not isinstance(b, dict):
                raise ValueError("not dict")
            bodies.append((r, b))
        except Exception:
            bad_json += 1
    check(bad_json == 0, "每行都能构造出合法报文", "异常 %d" % bad_json)

    # ---- B. 配比 ----
    print("\nB) CondType 配比")
    got = Counter(b["CondType"] for _, b in bodies)
    for ct in sorted(expect):
        check(got.get(ct, 0) == expect[ct],
              "CondType=%d 条数 == %d" % (ct, expect[ct]), got.get(ct, 0))
    check(set(got) <= set(expect),
          "没有出现配比之外的 CondType", sorted(set(got) - set(expect)))

    # ---- C. 覆盖 ----
    print("\nC) code 覆盖")
    seen = Counter(code_of(b["Entrust"]) for _, b in bodies)
    missing = [c for c in price if c not in seen]
    extra = [c for c in seen if c not in price]
    check(not missing, "codes 文件里每个 code 都出现过",
          "缺 %d 个: %s" % (len(missing), missing[:5]))
    check(not extra, "没有 codes 文件之外的 code",
          "多 %d 个: %s" % (len(extra), extra[:5]))
    if seen:
        check(min(seen.values()) >= 1,
              "每个 code 至少 1 条", "min=%d" % min(seen.values()))
        check(max(seen.values()) - min(seen.values()) <= 1,
              "各 code 条数均匀（极差 <= 1）",
              "min=%d max=%d" % (min(seen.values()), max(seen.values())))

    # ---- D. 账号 ----
    print("\nD) 账号")
    faccts = {str(b["Account"]["FAccount"]) for _, b in bodies}
    check(len(faccts) == a.accounts, "轮换账号数 == %d" % a.accounts, len(faccts))
    bad_ua = 0
    for _, b in bodies:
        acc = b["Account"]
        want = "%s_%s_%s" % (acc["FAccount"], acc["AccountType"], acc["AccAtt"])
        if b.get("UniqueAccount") != want:
            bad_ua += 1
    check(bad_ua == 0, "UniqueAccount 全部跟随 FAccount", "错 %d 行" % bad_ua)

    # ---- E. 交易所 ----
    print("\nE) 交易所")
    bad_ex = Counter()
    for _, b in bodies:
        for name in EXCHANGE_BLOCKS:
            blk = b.get(name)
            if isinstance(blk, dict) and "ExchangeNum" in blk:
                if blk["ExchangeNum"] != a.exchange:
                    bad_ex["%s=%s" % (name, blk["ExchangeNum"])] += 1
    check(not bad_ex, "所有块 ExchangeNum == %d" % a.exchange, dict(bad_ex))

    # ---- F. 代码一致性 ----
    print("\nF) 代码一致性（★ CondLoss/CondProfit 必须同步）")
    bad_blk = Counter()
    for _, b in bodies:
        ent = code_of(b["Entrust"])
        for name in CODE_BLOCKS:
            blk = b.get(name)
            if not isinstance(blk, dict):
                bad_blk["%s缺块" % name] += 1
            elif code_of(blk) != ent:
                bad_blk["%s=%s" % (name, code_of(blk))] += 1
    check(not bad_blk,
          "Entrust/CfgExceed/CondPrice/CondPercent/CondTime/CondLoss/CondProfit "
          "七处 code 均等于本行 code", dict(bad_blk))
    # 止盈止损标的不该被期权 code 覆盖
    bad_tgt = 0
    for _, b in bodies:
        ent = code_of(b["Entrust"])
        for name in TARGET_BLOCKS:
            blk = b.get(name)
            if isinstance(blk, dict) and code_of(blk) == ent:
                bad_tgt += 1
    check(bad_tgt == 0,
          "CondTarget* 的标的是【标的代码】，没有被期权 code 覆盖",
          "错 %d 行" % bad_tgt)

    # ---- G. 单号 ----
    print("\nG) 条件单号")
    refs = [str(b.get("Ref") or "") for _, b in bodies]
    check(len(set(refs)) == len(refs), "Ref 逐行唯一",
          "%d 个唯一 / %d 行" % (len(set(refs)), len(refs)))
    bad_ref = [x for x in refs
               if not (re.fullmatch(r"__REF\d+__", x)
                       or re.fullmatch(r"\d{14}", x))]
    check(not bad_ref, "Ref 都是 __REFn__ 或 14 位单号", bad_ref[:3])

    # ---- H. 触发价 ----
    print("\nH) 触发价方向（broken = 必触发）")
    if a.trigger == "broken":
        bad_trig = []
        for _, b in bodies:
            ent = code_of(b["Entrust"])
            p = price.get(ent, 0.0) or C.ZERO_PRICE_FALLBACK
            try:
                t = float(b["CondPrice"]["TriggerPrice"])
            except (KeyError, TypeError, ValueError):
                bad_trig.append((ent, "缺少/非法"))
                continue
            if not (t < p):
                bad_trig.append((ent, t, p))
        check(not bad_trig,
              "每条 CondPrice 触发价都低于本行现价（必然触发）",
              "异常 %d 条: %s" % (len(bad_trig), bad_trig[:3]))
    else:
        print("  （safe 模式，跳过触发价方向校验）")

    # ---- I. 报文完整性 ----
    print("\nI) 报文完整性")
    need_top = ("Account", "CondType", "CondName", "Ref", "UniqueAccount",
                "ValidDate")
    bad_top = Counter()
    for _, b in bodies:
        for k in need_top:
            if k not in b:
                bad_top[k] += 1
        if not isinstance(b.get("Account"), dict):
            bad_top["Account非对象"] += 1
    check(not bad_top, "顶层必备字段齐全", dict(bad_top))
    sample = bodies[0][1] if bodies else {}
    printable = json.dumps(sample, ensure_ascii=False)
    check(len(printable) > 100, "报文可 JSON 序列化", "%d 字节" % len(printable))

    # ---- J. 不许有负值（2026-10-20 用户要求）----
    # 只查 normal 行；error/destroy 里的负数是【故意构造的非法值】，
    # 用来测平台容错（如 AccountType=-1 应被拒），必须保留。
    print("\nJ) normal 行不许出现负数")
    neg_found = Counter()
    for _, b in bodies:
        for name, blk in b.items():
            if not isinstance(blk, dict):
                continue
            for leaf, val in blk.items():
                try:
                    if float(str(val)) < 0:
                        neg_found["%s.%s=%s" % (name, leaf, val)] += 1
                except (TypeError, ValueError):
                    continue
    check(not neg_found,
          "normal 行所有数值字段都 >= 0", dict(neg_found))

    # ---- 汇总 ----
    print("\n" + "=" * 74)
    if FAIL:
        print("结果: 有失败   通过 %d / 失败 %d" % (OK, FAIL))
        print("失败项:")
        for f in FAILS:
            print("  - %s" % f)
    else:
        print("结果: 全部通过   通过 %d / 失败 0" % OK)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
