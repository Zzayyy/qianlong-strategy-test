# -*- coding: utf-8 -*-
"""
interfaces/pwdUpdate.py —— 接口定义：密码更换（MsgType=17）
=========================================================
报文形态：
    {"pwdUpdate":{
        "Account":{"Model":0,"AccountType":7,"AccAtt":6,"FAccount":"010100011300"},
        "Pwd":"<base64 密文>",
        "UniqueAccount":"010100011300_7_6"},
     "MsgType":17}

【实测】Pwd 必须两层加密（账号 + 日期），与 MsgType=18 的 Account 完全一致：
    Pwd = encrypt( encrypt(新密码明文, 账号), 日期字符串 )
    明文 123456   -> 真平台【静默不响应】（不报错、不回包，消息被消费后无下文）
    加密后的密文  -> {"Account":{...},"Errmsg":"update password success","ErrID":0}
所以 Excel 里 Pwd 列填【明文】，发送时由 build_payload 自动加密；
要测畸形密文请用 Pwd_raw 列（原样发出，不再加密）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (ZH, REAL_ACCOUNT, REAL_UNIQUE_ACCOUNT, collect, is_blank,
                     put, to_typed, gen_fuzz, add_cases,
                     fmt_account, unique_account, ACCOUNT_START)

# Pwd 加密（两层 AES-256-CBC + Base64，见 pwd_encode.py）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from pwd_encode import encode_pwd as _encode_pwd
except Exception:
    _encode_pwd = None

# 明文新密码（发送时自动两层加密）
REAL_PWD_PLAIN = "123123"

NAME = "pwdUpdate"
TITLE = "用户密码更换 (pwdUpdate, MsgType=17)"
MSG_TYPE = 17
TOP_KEY = "pwdUpdate"

HEADERS = [
    ("case_no", ZH["case_no"]),
    ("case_type", ZH["case_type"]),
    ("case_desc", ZH["case_desc"]),
    ("Account_Model", ZH["Model"]),
    ("Account_AccountType", ZH["AccountType"]),
    ("Account_AccAtt", ZH["AccAtt"]),
    ("Account_FAccount", ZH["FAccount"]),
    ("Pwd", "新密码【明文】(发送时自动两层加密)"),
    ("Pwd_raw", "原始Pwd密文(非空则直用,不加密;测畸形密文用)"),
    ("UniqueAccount", ZH["UniqueAccount"]),
    ("task_raw", ZH["task_raw"]),
    ("MsgType", ZH["MsgType"]),
    ("expected", ZH["expected"]),
]
_KEYS = [k for k, _ in HEADERS]


def _blank_row():
    return dict.fromkeys(_KEYS, "")


def _real_row():
    r = _blank_row()
    r.update({
        "Account_Model": REAL_ACCOUNT["Model"],
        "Account_AccountType": REAL_ACCOUNT["AccountType"],
        "Account_AccAtt": REAL_ACCOUNT["AccAtt"],
        "Account_FAccount": REAL_ACCOUNT["FAccount"],
        "Pwd": REAL_PWD_PLAIN,        # 明文，build_payload 时加密
        "UniqueAccount": REAL_UNIQUE_ACCOUNT,
    })
    return r


TEMPLATE = _real_row()


def _rows_to_tuples(items):
    out = []
    for no, typ, desc, changes, expected in items:
        r = dict(TEMPLATE)
        r["case_no"] = no
        r["case_type"] = typ
        r["case_desc"] = desc
        r["expected"] = expected
        r.update(changes)
        out.append(tuple(r[k] for k in _KEYS))
    return out


ROWS = _rows_to_tuples([
    ("P001", "normal", "正常改密码（真实账号结构，密文）", {}, "模板行"),
    ("P002", "normal", "不带 Model", {"Account_Model": ""}, "看是否必填"),

    ("P101", "error", "Pwd 为空", {"Pwd": ""}, "期望 Err<0"),
    ("P102", "error", "FAccount 为空", {"Account_FAccount": ""}, "期望 Err<0"),
    ("P103", "error", "AccountType 非法 99", {"Account_AccountType": 99}, "期望 Err<0"),
    ("P104", "error", "UniqueAccount 与 Account 不一致",
     {"UniqueAccount": "999999_9_9"}, "期望 Err<0"),

    # 明文 Pwd 实测会被真平台【静默吞掉】——消费掉但不回包，是重要负例
    ("P105", "error", "Pwd 传明文（真平台静默不响应）",
     {"Pwd_raw": "123456"}, "实测不回包，据此判定"),

    ("P201", "destroy", "Pwd 超长 1000", {"Pwd_raw": "__LONG__"}, "超长密文"),
    ("P202", "destroy", "Pwd 传非 base64 垃圾", {"Pwd_raw": "!!!not-base64!!!"},
     "解密应失败但不崩"),
    ("P203", "destroy", "Pwd SQL 注入", {"Pwd_raw": "__SQL__"}, "注入"),
    ("P204", "destroy", "task 非 JSON", {"task_raw": "not json"}, "解析容错"),
    ("P205", "destroy", "task 空对象", {"task_raw": "{}"}, "结构错"),
    ("P206", "destroy", "MsgType=4 不匹配", {"MsgType": 4}, "分发容错"),
])

_PREFIX = "P"
_BASE = tuple(TEMPLATE[k] for k in _KEYS)
_BULK = gen_fuzz(HEADERS, _BASE, {
    # Pwd 明文会先被两层加密再发出，这些畸形"明文"测的是【加密前的输入】。
    # 要测畸形密文请用 Pwd_raw 列。
    "Pwd": ["", "__EMPTY__", "__SPACE__", "__LONG__", "__LONG10__",
            "__CTRL__", "__SQL__", "__XSS__", "__NEWLINE__", "__EMOJI2__"],
    "Pwd_raw": ["", "123456", "!!!bad!!!", "__LONG__", "__EMPTY__", "YWJj", "__SQL__"],
    "Account_FAccount": ["", "__LONG__", "__SQL__", "__UNICODE__"],
    "Account_AccountType": [99, -1, "abc", "__MAXINT__"],
    "Account_AccAtt": [9, -1, "abc"],
    "UniqueAccount": ["", "999999_9_9", "__LONG__", "__SQL__"],
}, type_tag="destroy", start=200, prefix=_PREFIX)

_BULK += add_cases(HEADERS, _BASE, [
    ("task 是数组", {"task_raw": "[1]"}),
    ("task 是 null", {"task_raw": "null"}),
    ("task 是数字", {"task_raw": "17"}),
], type_tag="destroy", start=400, prefix=_PREFIX)

ROWS = ROWS + _BULK


# ==================== 批量 normal（--bulk-normal）====================
def build_bulk_rows(count, start=0):
    """生成 count 行 normal：每行一个不同账号，把该账号的密码改成同一明文。

    ★ 与 create/account 共用同一号段（默认 11301 起），这样"先 account 登录、
      再 create 下单、再改密码"都能落到同一批账号上。

    ★ 没有 Ref：改密码是按账号维度操作的（MsgType=17 报文里只有 Account +
      UniqueAccount + Pwd），不需要引用具体条件单。

    ★ Pwd 存【明文】，build_payload 时按【本行账号】做两层加密 ——
      账号不同密文就不同，这是对的。
    """
    count = int(count)
    if count <= 0:
        raise ValueError("条数必须 > 0")
    start = int(start) or ACCOUNT_START
    if start + count - 1 > 999999:
        raise ValueError("账号序号 %d 超出 6 位上限 999999" % (start + count - 1))

    keys = list(_KEYS)
    idx = {k: i for i, k in enumerate(keys)}
    template = None
    for r in ROWS:
        if (isinstance(r, (list, tuple)) and len(r) == len(keys)
                and str(r[idx["case_type"]]) == "normal"):
            template = list(r)
            break
    if template is None:
        raise ValueError("ROWS 中没有 normal 模板行")

    at = REAL_ACCOUNT["AccountType"]
    aa = REAL_ACCOUNT["AccAtt"]
    out = []
    for i in range(count):
        seq = start + i
        facct = fmt_account(seq)
        r = list(template)
        r[idx["case_no"]] = "PB%05d" % (i + 1)
        r[idx["case_type"]] = "normal"
        r[idx["case_desc"]] = "压测账号%s 改密码（第 %d/%d 个）" % (facct, i + 1, count)
        r[idx["Account_Model"]] = REAL_ACCOUNT["Model"]
        r[idx["Account_AccountType"]] = at
        r[idx["Account_AccAtt"]] = aa
        r[idx["Account_FAccount"]] = facct
        r[idx["Pwd"]] = REAL_PWD_PLAIN
        r[idx["Pwd_raw"]] = ""
        r[idx["UniqueAccount"]] = unique_account(facct, at, aa)
        out.append(tuple(r))
    return out


def build_payload(row):
    raw = row.get("task_raw")
    if not is_blank(raw):
        return str(raw)
    body = {}
    acct = collect(row, "Account")
    if acct:
        body["Account"] = acct
    put(body, "UniqueAccount", row)

    # ---- Pwd：两层加密（账号 + 日期），与 MsgType=18 的 Account 同一套 ----
    # 【实测】传明文会被真平台静默吞掉（消费掉但不回包），必须加密：
    #   Pwd = encrypt( encrypt(明文新密码, 账号), 日期 )
    # Pwd_raw 非空时直用原值（测畸形密文 / 明文负例走这条）。
    pwd_raw = row.get("Pwd_raw")
    pwd_plain = row.get("Pwd")
    if not is_blank(pwd_raw):
        from _common import expand as _expand
        v = _expand(pwd_raw)
        body["Pwd"] = v if isinstance(v, str) else str(v)
    elif not is_blank(pwd_plain):
        plain = str(pwd_plain)
        facct = (acct or {}).get("FAccount") or ""
        if _encode_pwd is None or not facct:
            body["Pwd"] = plain
        else:
            try:
                body["Pwd"] = _encode_pwd(plain, facct)
            except Exception as e:
                sys.stderr.write("[WARN] pwdUpdate Pwd 加密失败(%s)，退回明文\n" % e)
                body["Pwd"] = plain

    payload = {TOP_KEY: body, "MsgType": MSG_TYPE}
    mt = row.get("MsgType")
    if not is_blank(mt):
        payload["MsgType"] = to_typed("MsgType", mt)
    return payload
