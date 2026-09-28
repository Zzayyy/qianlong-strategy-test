# -*- coding: utf-8 -*-
"""
excel_loader.py —— 从 Excel 读用例（与 datahub_test 同口径）
==========================================================
表头形如 "中文说明\n(english_key)"，用正则从括号里取 key。
返回的每条用例是 (case_no, case_type, case_desc, payload_text) 四元组，
其中 payload_text 是【最终要写进 task 字段的字符串】：
    build_payload 返回 dict  -> json.dumps
    build_payload 返回 str   -> 原样用（测非法 JSON 的用例走这条）

过滤（与 datahub_test 一致，且都在读取阶段完成，支持提前终止）：
    want_types  : {'normal'} / {'destroy'} / None
    cases_spec  : "C001,C003-C010,25"（C 编号匹配 case_no，纯数字按数据行号）
"""
import importlib
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
INTERFACES_DIR = os.path.join(BASE_DIR, "interfaces")


def list_interfaces():
    if not os.path.isdir(INTERFACES_DIR):
        return []
    return sorted(f[:-3] for f in os.listdir(INTERFACES_DIR)
                  if f.endswith(".py") and not f.startswith(("__", "_")))


def load_interface(name):
    sys.path.insert(0, INTERFACES_DIR)
    try:
        return importlib.import_module(name)
    except ImportError as e:
        sys.exit("[FAIL] 接口 %s 不存在（%s）。可用: %s"
                 % (name, e, list_interfaces()))


def _key(h):
    """从 "中文\\n(key)" 取 key；取不到就用原文本。"""
    m = re.search(r"\(([A-Za-z_][A-Za-z0-9_]*)\)", str(h))
    return m.group(1) if m else str(h).strip()


def _payload_text(mod, rec):
    """rec(字典) -> 最终 task 字符串"""
    p = mod.build_payload(rec)
    if isinstance(p, str):
        return p
    import json
    return json.dumps(p, ensure_ascii=False, separators=(",", ":"))


def _parse_cases_spec(spec):
    """"C001,C003-C010,25" -> (set_of_nos, set_of_rows)

    C 开头按 case_no 匹配；纯数字按【数据行号】（1 起始）。
    """
    nos, rows = set(), set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            a, b = a.strip(), b.strip()
            ma = re.fullmatch(r"([A-Za-z]*)(\d+)", a)
            mb = re.fullmatch(r"([A-Za-z]*)(\d+)", b)
            if not (ma and mb):
                continue
            prefix = ma.group(1)
            lo, hi = int(ma.group(2)), int(mb.group(2))
            for i in range(min(lo, hi), max(lo, hi) + 1):
                if prefix:
                    nos.add("%s%03d" % (prefix, i) if len(ma.group(2)) >= 3
                            else "%s%d" % (prefix, i))
                else:
                    rows.add(i)
        else:
            m = re.fullmatch(r"([A-Za-z]*)(\d+)", part)
            if not m:
                continue
            if m.group(1):
                nos.add(part)
            else:
                rows.add(int(m.group(2)))
    return nos, rows


def load_cases(excel, want_types=None, cases_spec="", max_cases=0, quiet=False):
    """读 Excel，返回 [(case_no, case_type, case_desc, payload_text, row_no), ...]

    max_cases > 0 时读够就停（提前终止，避免万行表白读）。
    """
    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit("缺少 openpyxl，请先执行: pip install openpyxl")
    if not os.path.exists(excel):
        sys.exit("[FAIL] 找不到 %s\n       先生成：python make_excel.py --interface %s"
                 % (excel, os.path.splitext(os.path.basename(excel))[0]))

    mod = load_interface(os.path.splitext(os.path.basename(excel))[0])
    want_nos, want_rows = _parse_cases_spec(cases_spec) if cases_spec else (set(), set())

    wb = load_workbook(excel, read_only=True, data_only=True)
    try:
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        try:
            header_row = next(it)
        except StopIteration:
            sys.exit("[FAIL] %s 为空" % excel)
        headers = [_key(h) for h in header_row]
        ncol = len(headers)

        out = []
        n_rows = 0
        for raw in it:
            if raw is None or all(v is None or str(v).strip() == "" for v in raw):
                continue
            n_rows += 1
            rec = {headers[i]: ("" if raw[i] is None else raw[i])
                   for i in range(min(ncol, len(raw)))}
            no = str(rec.get("case_no") or ("C%d" % n_rows)).strip()
            typ = str(rec.get("case_type") or "normal").strip().lower()
            desc = str(rec.get("case_desc") or "").strip()

            # 过滤
            if want_types and typ not in want_types:
                continue
            if want_nos or want_rows:
                if no not in want_nos and n_rows not in want_rows:
                    continue

            try:
                txt = _payload_text(mod, rec)
            except Exception as e:
                if not quiet:
                    print("[WARN] 用例 %s 构造失败，跳过: %s" % (no, e))
                continue
            out.append((no, typ, desc, txt, n_rows))
            if max_cases and len(out) >= max_cases:
                break
    finally:
        wb.close()
    return out


def default_excel(interface):
    return os.path.join(DATA_DIR, "%s.xlsx" % interface)
