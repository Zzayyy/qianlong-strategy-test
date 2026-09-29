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
    cases_spec  : "C001,C003-C010,25"（带字母前缀按 case_no，纯数字按数据行号）

★ --cases 的语义与 datahub_test 完全对齐（parse_case_spec / filter_cases_by_spec
  照搬，含报错行为）：分隔符支持 , ， ; 和空白；前缀支持多字母；
  匹配时忽略大小写与前导零；语法错误直接报错，而不是静默忽略
  （静默忽略会让筛选条件失效 -> 变成"发全表"，曾踩过）。
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


# ==================== --cases 表达式解析（照搬 datahub_test） ====================
def parse_case_spec(spec):
    """解析用例编号筛选表达式，如 'C1,C3-C10,25-30' 或 'QG0001,QG3-QG10'。
    逗号/分号/空白分隔，可混用。返回 (singles, ranges)：
      singles: {(前缀大写或'', 数字), ...}   前缀 '' 表示按 Excel 数据行号（1 起始）
      ranges:  [(前缀大写或'', 起始, 结束), ...]

    前缀支持【多个字母】（如 query 表的 QG0001、create 表的 CG0001）。
    解析不出来直接 raise ValueError —— 不静默忽略（忽略会让筛选失效，
    退化成"发全表"，2026-09-28 对比 datahub 时发现并修正）。
    """
    singles, ranges = set(), []
    for tok in re.split(r"[,，;\s]+", str(spec).strip()):
        if not tok:
            continue
        m = re.fullmatch(r"([A-Za-z]*)(\d+)(?:-([A-Za-z]*)(\d+))?", tok)
        if not m:
            raise ValueError("无法识别的用例编号: %s"
                             "（示例: C1 / QG0001 / C3-C10 / 5-20）" % tok)
        p1, n1 = (m.group(1) or "").upper(), int(m.group(2))
        if m.group(3) is None and m.group(4) is None:
            singles.add((p1, n1))
        else:
            # 右端省略前缀时沿用左端（如 C3-10、QG3-10）
            p2 = (m.group(3) or p1).upper()
            if p1 != p2:
                raise ValueError("范围前后编号前缀不一致: %s" % tok)
            a, b = sorted((n1, int(m.group(4))))
            ranges.append((p1, a, b))
    return singles, ranges


def row_stop_of_spec(spec):
    """若 --cases 只含【纯数字行号】条件，返回需要读到的最大行号（提前终止用）；
    否则返回 0（含字母前缀编号时必须读全表——编号可能在任意位置）。

    例：'10001-10100' -> 10100 / '100,200-300' -> 300 / 'C1' -> 0
    """
    try:
        singles, ranges = parse_case_spec(spec)
    except ValueError:
        return 0
    if any(p for p, _ in singles) or any(p for p, _, _ in ranges):
        return 0
    hi = 0
    for _, n in singles:
        hi = max(hi, n)
    for _, _, b in ranges:
        hi = max(hi, b)
    return hi


def _no_key(no):
    """用例编号 -> (前缀大写, 整数)，忽略前导零。取不出返回 None。

    有了它，'a201' / 'A0201' / 'A201' 都指向同一条用例（datahub 同款行为）。
    """
    m = re.match(r"([A-Za-z]*)0*(\d+)$", str(no).strip())
    return (m.group(1).upper(), int(m.group(2))) if m else None


def _spec_hits(no, row_no, singles, ranges):
    """该行是否命中筛选条件（编号命中 或 行号命中）。"""
    key = _no_key(no)
    if key is not None:
        if key in singles:
            return True
        if any(p == key[0] and a <= key[1] <= b for p, a, b in ranges):
            return True
    if ("", row_no) in singles:
        return True
    if any(p == "" and a <= row_no <= b for p, a, b in ranges):
        return True
    return False


def load_cases(excel, want_types=None, cases_spec="", max_cases=0, quiet=False):
    """读 Excel，返回 [(case_no, case_type, case_desc, payload_text, row_no), ...]

    max_cases > 0 时读够就停（提前终止，避免万行表白读）。
    cases_spec 写了但一条都没命中 -> 直接报错退出（绝不静默发全表）。
    """
    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit("缺少 openpyxl，请先执行: pip install openpyxl")
    if not os.path.exists(excel):
        sys.exit("[FAIL] 找不到 %s\n       先生成：python make_excel.py --interface %s"
                 % (excel, os.path.splitext(os.path.basename(excel))[0]))

    # 先解析筛选表达式：语法错误立刻报，别读完整表才发现
    try:
        spec_singles, spec_ranges = (parse_case_spec(cases_spec)
                                     if cases_spec else (set(), []))
    except ValueError as e:
        sys.exit("[FAIL] %s" % e)
    # 只有纯数字行号条件才启用提前终止；带字母前缀必须全读
    row_stop = row_stop_of_spec(cases_spec) if cases_spec else 0

    mod = load_interface(os.path.splitext(os.path.basename(excel))[0])

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
        n_rows = 0        # 已读到的【原始数据行号】（1 起始，与 --cases 纯数字对齐）
        for raw in it:
            if raw is None or all(v is None or str(v).strip() == "" for v in raw):
                continue
            n_rows += 1
            rec = {headers[i]: ("" if raw[i] is None else raw[i])
                   for i in range(min(ncol, len(raw)))}
            no = str(rec.get("case_no") or ("C%d" % n_rows)).strip()
            typ = str(rec.get("case_type") or "normal").strip().lower()
            desc = str(rec.get("case_desc") or "").strip()

            # 过滤：类型 与 编号/行号 都要过
            keep = True
            if want_types and typ not in want_types:
                keep = False
            elif cases_spec and not _spec_hits(no, n_rows, spec_singles,
                                               spec_ranges):
                keep = False

            if keep:
                try:
                    txt = _payload_text(mod, rec)
                except Exception as e:
                    if not quiet:
                        print("[WARN] 用例 %s 构造失败，跳过: %s" % (no, e))
                    txt = None
                if txt is not None:
                    out.append((no, typ, desc, txt, n_rows))
                    if max_cases and len(out) >= max_cases:
                        break
            # 提前终止：只要读到最大行号即可（纯数字行号筛选时）
            if row_stop and n_rows >= row_stop:
                break
    finally:
        wb.close()

    if cases_spec and not out:
        sys.exit("[FAIL] 用例编号 %s 未匹配到任何用例"
                 "（在 %s 中，已按 type=%s 过滤）\n"
                 "       请核对该表的用例编号列或数据行号"
                 % (cases_spec, os.path.basename(excel),
                    ",".join(sorted(want_types)) if want_types else "全部"))
    return out


def default_excel(interface):
    return os.path.join(DATA_DIR, "%s.xlsx" % interface)
