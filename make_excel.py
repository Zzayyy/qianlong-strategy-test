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
        alt = out_path.replace(".xlsx", "_v2.xlsx")
        wb.save(alt)
        print("[WARN] %s 被占用(Excel 可能开着)，已存到 %s" % (out_path, alt))
        return alt
    return out_path


def build_one(name, ref_spec=""):
    mod = load_interface(name)
    rows = list(mod.ROWS)

    if ref_spec:
        if name not in REF_INTERFACES:
            print("[SKIP] %s 不支持 --ref-spec（只有 %s 需要引用单号）"
                  % (name, "/".join(REF_INTERFACES)))
        else:
            try:
                extra = build_ref_rows(mod, ref_spec)
            except ValueError as e:
                sys.exit("[FAIL] %s --ref-spec %s" % (name, e))
            keys = [k for k, _ in mod.HEADERS]
            type_i = keys.index("case_type")
            kept = [r for r in rows
                    if not (isinstance(r, (list, tuple)) and len(r) == len(keys)
                            and str(r[type_i]) == "normal")]
            rows = list(extra) + kept
            start, count = _parse_ref_spec(ref_spec)
            print("[OK] %s: normal 段替换为 %d 行，引用第 %d~%d 号"
                  % (name, len(extra), start, start + len(extra) - 1))

    os.makedirs(DATA_DIR, exist_ok=True)
    out = os.path.join(DATA_DIR, "%s.xlsx" % mod.NAME)
    write_excel(mod, rows, out)
    from collections import Counter
    cnt = Counter(str(r[[k for k, _ in mod.HEADERS].index("case_type")]).strip()
                  for r in rows)
    print("[OK] %s -> %s  共 %d 条  %s" % (name, out, len(rows), dict(cnt)))
    return out


def main():
    ap = argparse.ArgumentParser(description="生成策略平台测试数据 Excel")
    ap.add_argument("--interface", default="all",
                    help="接口名（create/modify/remove/pwdUpdate/account）或 all")
    ap.add_argument("--ref-spec", default="",
                    help="仅 modify/remove：把 normal 段替换为按单号区间生成的引用行。"
                         "格式：起始[,条数] 或 起始-结束（可填完整单号或纯序号）。"
                         "例：7,100 或 20260904000001-20260904000100")
    ap.add_argument("--list", action="store_true", help="只列出各接口用例数，不生成")
    args = ap.parse_args()

    names = list_interfaces() if args.interface == "all" else [args.interface]
    if not names:
        sys.exit("[FAIL] 没有可用接口定义")

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
        return

    for n in names:
        build_one(n, args.ref_spec)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
