# -*- coding: utf-8 -*-
"""
make_excel.py —— 测试数据生成器：按接口定义生成 Excel
====================================================
用法：
    python make_excel.py --interface create      # 生成 data/create.xlsx
    python make_excel.py --interface all         # 生成全部接口
    python make_excel.py --interface all --list  # 只列出各接口用例数

生成的 Excel 统一放在 data/ 目录，供 send_test.py 读取。
结构与 datahub_test 一致：表头是两行式 "中文说明\n(english_key)"，
send_test.py 用正则从括号里取 key。

带 Ref 的接口（modify/remove）可用 --ref-spec 把 normal 段替换成
按单号区间生成的行（每行引用一个真实单号），配合 create 造的批单做压测。
"""
import argparse
import importlib
import json
import os
import re
import sys
import traceback

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
INTERFACES_DIR = os.path.join(BASE_DIR, "interfaces")

META_W = {"case_no": 12, "case_type": 16, "case_desc": 46, "expected": 34}

# 自带 Ref 的接口（--ref-spec 只对这些生效）
REF_INTERFACES = ("modify", "remove")


def list_interfaces():
    if not os.path.isdir(INTERFACES_DIR):
        return []
    return sorted(f[:-3] for f in os.listdir(INTERFACES_DIR)
                  if f.endswith(".py") and not f.startswith(("__", "_")))


def load_interface(name):
    sys.path.insert(0, INTERFACES_DIR)
    try:
        mod = importlib.import_module(name)
    except ImportError:
        sys.exit("[FAIL] 接口 %s 不存在。可用接口: %s" % (name, list_interfaces()))
    for attr in ("NAME", "HEADERS", "ROWS", "build_payload"):
        if not hasattr(mod, attr):
            sys.exit("[FAIL] 接口 %s 缺少 %s 定义" % (name, attr))
    return mod


def _parse_ref_spec(spec):
    """把 --ref-spec 解析成 (起始序号, 条数)。

    支持：
      "7,100" / "20260904000001,100"           -> 从第 7 号起共 100 条
      "7-106" / "20260904000001-20260904000100" -> 序号区间
    与 datahub_test 同款：Excel 里存 __REFn__ token，发送时按当天展开。
    """
    s = str(spec).strip()
    if not s:
        raise ValueError("为空")

    def _seq(tok):
        t = tok.strip()
        if re.fullmatch(r"\d{14}", t):
            return int(t[8:])
        if re.fullmatch(r"\d{1,6}", t):
            return int(t)
        raise ValueError("无法解析单号/序号: %r（应为 YYYYMMDD+6位 或 纯1~6位序号）" % tok)

    if "-" in s and "," not in s:
        a, b = s.split("-", 1)
        start, end = _seq(a), _seq(b)
        if end < start:
            raise ValueError("区间结束号 %d 小于起始号 %d" % (end, start))
        return start, end - start + 1
    if "," in s:
        left, cnt = s.split(",", 1)
        try:
            count = int(cnt.strip())
        except ValueError:
            raise ValueError("条数解析失败: %r" % cnt)
        if count <= 0:
            raise ValueError("条数必须 > 0: %d" % count)
        return _seq(left), count
    raise ValueError("格式应为 起始[,条数] 或 起始-结束，如 7,100 或 7-106")


def build_ref_rows(mod, spec):
    """把 normal 行替换为"每行引用一个单号"的引用行。"""
    keys = [k for k, _ in mod.HEADERS]
    ref_key = getattr(mod, "REF_KEY", "")
    for need in ("case_no", "case_type", "case_desc"):
        if need not in keys:
            raise ValueError("接口 %s 表头缺少 %s 列" % (mod.NAME, need))
    if not ref_key or ref_key not in keys:
        raise ValueError("接口 %s 未定义 REF_KEY 或表头无该列" % mod.NAME)

    start, count = _parse_ref_spec(spec)
    if start + count - 1 > 999999:
        raise ValueError("引用结束序号 %d 超过 6 位上限 999999" % (start + count - 1))

    type_i = keys.index("case_type")
    no_i = keys.index("case_no")
    desc_i = keys.index("case_desc")
    ref_i = keys.index(ref_key)

    template = None
    for r in mod.ROWS:
        if isinstance(r, (list, tuple)) and len(r) == len(keys) \
                and str(r[type_i]) == "normal":
            template = list(r)
            break
    if template is None:
        raise ValueError("接口 %s 的 ROWS 中没有 normal 模板行" % mod.NAME)

    prefix = mod.NAME[0].upper() + "G"
    out = []
    for i in range(count):
        seq = start + i
        r = list(template)
        r[no_i] = "%s%04d" % (prefix, i + 1)
        r[type_i] = "normal"
        r[desc_i] = "%s 引用当日第 %d 号" % (mod.NAME, seq)
        r[ref_i] = "__REF%d__" % seq
        out.append(tuple(r))
    return out


def write_excel(mod, rows, out_path):
    wb = Workbook()
    ws = wb.active
    ws.title = mod.NAME[:31]

    for col, (key, zh) in enumerate(mod.HEADERS, 1):
        cell = ws.cell(row=1, column=col, value="%s\n(%s)" % (zh, key))
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center",
                                   wrap_text=True)
        cell.fill = PatternFill("solid", fgColor="D9E1F2")
    ws.row_dimensions[1].height = 32

    def _san(v):
        if isinstance(v, str) and ILLEGAL_CHARACTERS_RE.search(v):
            return ILLEGAL_CHARACTERS_RE.sub("[_]", v)
        return v

    for r, row in enumerate(rows, 2):
        for c, v in enumerate(row, 1):
            ws.cell(row=r, column=c, value=_san(v))

    for c, (key, zh) in enumerate(mod.HEADERS, 1):
        if key in META_W:
            w = META_W[key]
        else:
            try:
                maxlen = max(len(str(zh)), *[len(str(r[c - 1])) for r in rows])
            except ValueError:
                maxlen = len(str(zh))
            w = min(max(11, maxlen + 3), 30)
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A2"

    tmp = out_path + ".tmp"
    wb.save(tmp)
    try:
        os.replace(tmp, out_path)
    except PermissionError:
        # 目标被别的程序占着（最常见：Excel/WPS 正开着这个 xlsx）。
        # 这时【原文件没有被更新】—— 必须说清楚，否则会以为生成成功了，
        # 实际发出去的还是旧表（踩过：数据没变，排查半天）。
        try:
            os.remove(tmp)
        except Exception:
            pass
        alt = out_path.replace(".xlsx", "_v2.xlsx")
        wb.save(alt)
        print("")
        print("!" * 74)
        print("[WARN] %s" % out_path)
        print("       被其它程序占用（Excel / WPS 正开着它？），【原文件没有更新】！")
        print("       新内容已另存到：%s" % alt)
        print("       要生效请二选一：")
        print("         1) 关掉打开该 xlsx 的 Excel/WPS，重新生成；")
        print("         2) 直接用那个 _v2 文件。")
        print("!" * 74)
        print("")
        return alt
    return out_path


def build_bulk_rows(mod, count, start=0, ref_seq=1):
    """调用接口自己的 build_bulk_rows，把 normal 段换成 N 行不同账号。

    ref_seq: 当天全局起始单号（只有 modify/remove 用得到；它们的
             build_bulk_rows 第 3 个参数就是它）。
    """
    fn = getattr(mod, "build_bulk_rows", None)
    if fn is None:
        # 只列名字，不要在这里 load_interface（会造成递归 import 报错）
        supported = []
        for n in list_interfaces():
            try:
                if hasattr(importlib.import_module(n), "build_bulk_rows"):
                    supported.append(n)
            except Exception:
                pass
        raise ValueError(
            "接口 %s 未实现 build_bulk_rows(count, start)，不支持 --bulk-normal。\n"
            "        目前支持：%s" % (mod.NAME, ", ".join(supported) or "（无）"))
    # 只有引用了单号的接口才收 ref_seq，其余接口签名是 (count, start)
    ref_key = getattr(mod, "REF_KEY", "")
    if ref_key:
        return fn(count, start, ref_seq)
    return fn(count, start)


def load_ref_map(path):
    """读 send_test 落盘的 refs JSON，转成 {账号: Ref}。

    文件是 send_test.py 发 create 时自动写的，内容是
        [{"account":"010100011301","case_no":"CB00001","ref":"20260928000001"}, ...]
    """
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    if isinstance(rows, dict):
        rows = rows.get("refs") or []
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        acct, ref = r.get("account"), r.get("ref")
        if acct and ref:
            out[str(acct)] = str(ref)
    return out


def apply_ref_map(rows, fa_i, ref_i, refmap):
    """把行内账号对应的【真实 Ref】写进单号列；未命中保留原 token。"""
    out = []
    miss = 0
    for r in rows:
        row = list(r)
        acct = str(row[fa_i]) if fa_i is not None else ""
        ref = refmap.get(acct)
        if ref:
            row[ref_i] = ref
        else:
            miss += 1
        out.append(tuple(row))
    hit = len(rows) - miss
    if miss:
        print("[WARN] %d/%d 行账号未在 refs 映射里，保留原 __REF__ token"
              % (miss, len(rows)))
    if rows and hit == 0:
        print("[ERROR] 全部行都没命中 —— 生成的单号仍是 __REF__ token，发出去会 ref not exist！\n"
              "        常见原因：账号起点不一致（别用 --bulk-start 改起点），\n"
              "        或 create 发的时候用的不是同一批账号。")
    return out


def build_one(name, ref_spec="", bulk_normal=0, bulk_start=0, ref_seq=1,
              ref_map=""):
    mod = load_interface(name)
    rows = list(mod.ROWS)
    keys = [k for k, _ in mod.HEADERS]
    type_i = keys.index("case_type")

    if ref_spec and bulk_normal:
        sys.exit("[FAIL] --ref-spec 与 --bulk-normal 互斥，不能同时使用")

    if bulk_normal:
        try:
            extra = build_bulk_rows(mod, bulk_normal, bulk_start, ref_seq)
        except ValueError as e:
            sys.exit("[FAIL] %s --bulk-normal: %s" % (name, e))

        # ---- 用 create 实跑落盘的 refs.json 回填真实单号 ----
        # 这比"靠行序猜 __REF{i}__"可靠得多：单号来自平台回包，是确定存在的。
        if ref_map:
            ref_key = getattr(mod, "REF_KEY", "")
            if not ref_key:
                sys.exit("[FAIL] %s 没有 REF_KEY，不需要 --ref-map" % name)
            if ref_key not in keys:
                sys.exit("[FAIL] %s 表头无 %s 列" % (name, ref_key))
            try:
                refmap = load_ref_map(ref_map)
            except Exception as e:
                sys.exit("[FAIL] 读不了 --ref-map %s: %s" % (ref_map, e))
            if not refmap:
                sys.exit("[FAIL] --ref-map %s 里没有可用的 账号->Ref" % ref_map)
            extra = apply_ref_map(extra, keys.index("Account_FAccount"),
                                  keys.index(ref_key), refmap)
            print("[OK] %s: 已用 %s 回填真实单号（映射 %d 条）"
                  % (name, os.path.basename(ref_map), len(refmap)))

        kept = [r for r in rows
                if not (isinstance(r, (list, tuple)) and len(r) == len(keys)
                        and str(r[type_i]) == "normal")]
        rows = list(extra) + kept
        extra_msg = ""
        if getattr(mod, "REF_KEY", "") and ref_seq != 1 and not ref_map:
            extra_msg = "，引用第 %d 号起的单号" % ref_seq
        print("[OK] %s: normal 段替换为 %d 行不同账号（起始序号 %s%s）"
              % (name, len(extra), bulk_start or "默认", extra_msg))

    if ref_spec:
        if name not in REF_INTERFACES:
            print("[SKIP] %s 不支持 --ref-spec（只有 %s 需要引用单号）"
                  % (name, "/".join(REF_INTERFACES)))
        else:
            try:
                extra = build_ref_rows(mod, ref_spec)
            except ValueError as e:
                sys.exit("[FAIL] %s --ref-spec %s" % (name, e))
            kept = [r for r in rows
                    if not (isinstance(r, (list, tuple)) and len(r) == len(keys)
                            and str(r[type_i]) == "normal")]
            rows = list(extra) + kept
            start, count = _parse_ref_spec(ref_spec)
            print("[OK] %s: normal 段替换为 %d 行，引用第 %d~%d 号"
                  % (name, len(extra), start, start + len(extra) - 1))

    os.makedirs(DATA_DIR, exist_ok=True)
    out = os.path.join(DATA_DIR, "%s.xlsx" % mod.NAME)
    actual = write_excel(mod, rows, out)
    from collections import Counter
    cnt = Counter(str(r[type_i]).strip() for r in rows)
    # 注意：被占用时 write_excel 会写到 _v2，这里报【实际写入的路径】，
    # 免得看到"[OK]"却以为原表更新了。
    print("[OK] %s -> %s  共 %d 条  %s"
          % (name, actual or out, len(rows), dict(cnt)))
    return actual or out


def main():
    ap = argparse.ArgumentParser(description="生成策略平台测试数据 Excel")
    ap.add_argument("--interface", default="all",
                    help="接口名（create/modify/remove/pwdUpdate/account）或 all")
    ap.add_argument("--ref-spec", default="",
                    help="仅 modify/remove：把 normal 段替换为按单号区间生成的引用行。"
                         "格式：起始[,条数] 或 起始-结束（可填完整单号或纯序号）。"
                         "例：7,100 或 20260904000001-20260904000100")
    ap.add_argument("--bulk-normal", type=int, default=0,
                    help="把 normal 段替换为 N 行【不同账号】的正常数据（压测用、不循环）。"
                         "五个接口都已实现。例：--bulk-normal 10000")
    ap.add_argument("--bulk-start", type=int, default=0,
                    help="批量账号的 6 位序号起点，默认 11301（紧邻真实账号 010100011300）")
    ap.add_argument("--ref-seq", type=int, default=1,
                    help="仅 modify/remove：批量行引用的【当天全局起始单号】。"
                         "默认 1（配合 create --bulk-normal 的默认号段）。"
                         "若当天已经建过 N 张单，要填 N+1，否则会引用到不存在的单号。")
    ap.add_argument("--ref-map", default="",
                    help="★推荐：用 send_test 发 create 时自动落盘的 refs.json 回填【真实单号】。"
                         "比 --ref-seq 靠行序猜可靠得多（单号来自平台回包）。"
                         "例：--bulk-normal 10000 --ref-map out/performance/create_xxx_refs.json")
    # 行情代码每天都在换，做成参数就不必改源码（GUI 里有输入框）
    ap.add_argument("--contract-code", default="",
                    help="行情合约代码（Entrust/CondPrice/CondPercent/CondTime/"
                         "CondLoss/CondProfit 用），默认取环境变量 ST_CONTRACT_CODE "
                         "或 interfaces/_common.py 里的 DEFAULT_CONTRACT_CODE")
    ap.add_argument("--target-stock-code", default="",
                    help="止盈止损标的代码（CondTargetLoss/CondTargetProfit 用），"
                         "默认取环境变量 ST_TARGET_STOCK_CODE 或默认值")
    ap.add_argument("--list", action="store_true", help="只列出各接口用例数，不生成")
    args = ap.parse_args()

    # ★ 必须在 load_interface 之前注入环境变量：
    #   接口模块是在 import 时把 contract_code() 求值进 REAL_BLOCKS / MODIFY_BLOCKS 的，
    #   晚了就还是旧值（表里会静默写成上一天的代码）。
    if args.contract_code.strip():
        os.environ["ST_CONTRACT_CODE"] = args.contract_code.strip()
    if args.target_stock_code.strip():
        os.environ["ST_TARGET_STOCK_CODE"] = args.target_stock_code.strip()

    names = list_interfaces() if args.interface == "all" else [args.interface]
    if not names:
        sys.exit("[FAIL] 没有可用接口定义")

    # 显式告知本次用了哪个合约代码，避免"改完参数表里还是旧值"这种静默问题
    if args.contract_code.strip() or args.target_stock_code.strip():
        sys.path.insert(0, INTERFACES_DIR)
        from _common import contract_code as _cc, target_stock_code as _tc
        print("[OK] 行情代码：合约=%s  止盈止损标的=%s" % (_cc(), _tc()))

    if args.list:
        print("%-12s %-38s %8s %8s %8s" % ("接口", "说明", "normal", "error", "destroy"))
        for n in names:
            mod = load_interface(n)
            from collections import Counter
            keys = [k for k, _ in mod.HEADERS]
            ti = keys.index("case_type")
            c = Counter(str(r[ti]).strip() for r in mod.ROWS)
            print("%-12s %-38s %8d %8d %8d"
                  % (n, getattr(mod, "TITLE", "")[:36], c.get("normal", 0),
                     c.get("error", 0), c.get("destroy", 0)))
        total = sum(len(load_interface(n).ROWS) for n in names)
        print("-" * 76)
        print("%-12s %-38s %8s" % ("合计", "", total))
        print()
        print("注：上表是【接口定义里写死】的规模。五个接口都可用 --bulk-normal N")
        print("    把 normal 段扩成 N 行不同账号的压测数据"
              "（详见 docs/guide.md 第 6 节），例如：")
        print("      python make_excel.py --interface account   --bulk-normal 10000")
        print("      python make_excel.py --interface create    --bulk-normal 10000")
        print("      python make_excel.py --interface modify    --bulk-normal 10000")
        print("      python make_excel.py --interface remove    --bulk-normal 10000")
        print("      python make_excel.py --interface pwdUpdate --bulk-normal 10000")
        print("    账号号段一致；modify/remove 的单号靠行序对齐 create，须按 create 先发。")
        return

    for n in names:
        build_one(n, args.ref_spec, args.bulk_normal, args.bulk_start,
                  args.ref_seq, args.ref_map)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
