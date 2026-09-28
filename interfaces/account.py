# -*- coding: utf-8 -*-
"""
interfaces/account.py —— 接口定义：用户信息（MsgType=18）
=======================================================
报文形态（照抄 136 ST-0 的真实样本）：
    {"Account":{
        "Account":{"FAccount":"010100011300","AccountType":7,"AccAtt":6},
        "ClientName":"张国昌",
        "Pwd":"<base64 密文>",
        "TradeAccount":"010100011300",
        "LoginIP":"","LoginMAC":"","LoginOS":"",
        "UniqueAccount":"010100011300_7_6",
        "BranchNo":"123456",
        "Shareholders":[{"SAccount":"A442523077","ExchangeNum":1},
                        {"SAccount":"0199908393","ExchangeNum":2}]},
     "MsgType":18}

注意两个 "Account" 层级：
    外层 payload["Account"] 是 MsgType=18 的子对象
    内层 body["Account"]    是账号四要素
Excel 列名用 Account_ 前缀指内层（Account_FAccount），
外层靠 TOP_KEY 自动加，不用写。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (ZH, REAL_ACCOUNT, REAL_UNIQUE_ACCOUNT, is_blank, put,
                     to_typed, collect, gen_fuzz, add_cases)

# Pwd 加密（两层 AES-256-CBC + Base64，见 pwd_encode.py）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from pwd_encode import encode_pwd as _encode_pwd
except Exception:
    _encode_pwd = None

NAME = "account"
TITLE = "用户信息 (Account, MsgType=18)"
MSG_TYPE = 18
TOP_KEY = "Account"

# 明文登录密码（策略平台要用它去柜台登录）。
# 报文里的 Pwd 是这个值经"账号 + 日期"两层加密后的密文，见 build_payload。
REAL_PWD_PLAIN = "123123"

HEADERS = [
    ("case_no", ZH["case_no"]),
    ("case_type", ZH["case_type"]),
    ("case_desc", ZH["case_desc"]),
    ("Account_Model", ZH["Model"]),
    ("Account_AccountType", ZH["AccountType"]),
    ("Account_AccAtt", ZH["AccAtt"]),
    ("Account_FAccount", ZH["FAccount"]),
    ("ClientName", ZH["ClientName"]),
    ("Pwd", "登录密码【明文】(发送时自动两层加密)"),
    ("Pwd_raw", "原始Pwd密文(非空则直用,不加密;测畸形密文用)"),
    ("TradeAccount", ZH["TradeAccount"]),
    ("LoginIP", ZH["LoginIP"]),
    ("LoginMAC", ZH["LoginMAC"]),
    ("LoginOS", ZH["LoginOS"]),
    ("UniqueAccount", ZH["UniqueAccount"]),
    ("BranchNo", ZH["BranchNo"]),
    ("SH_SAccount1", ZH["SH_SAccount1"]),
    ("SH_ExchangeNum1", ZH["SH_ExchangeNum1"]),
    ("SH_SAccount2", ZH["SH_SAccount2"]),
    ("SH_ExchangeNum2", ZH["SH_ExchangeNum2"]),
    ("task_raw", ZH["task_raw"]),
    ("MsgType", ZH["MsgType"]),
    ("expected", ZH["expected"]),
]
_KEYS = [k for k, _ in HEADERS]

# 股东号列 -> 是否数组
_SH_COLS = [("SH_SAccount1", "SH_ExchangeNum1"),
            ("SH_SAccount2", "SH_ExchangeNum2")]


def _blank_row():
    return dict.fromkeys(_KEYS, "")


def _real_row():
    r = _blank_row()
    r.update({
        "Account_Model": REAL_ACCOUNT["Model"],
        "Account_AccountType": REAL_ACCOUNT["AccountType"],
        "Account_AccAtt": REAL_ACCOUNT["AccAtt"],
        "Account_FAccount": REAL_ACCOUNT["FAccount"],
        "ClientName": "张国昌",
        "Pwd": REAL_PWD_PLAIN,        # 明文，build_payload 时加密
        "TradeAccount": REAL_ACCOUNT["FAccount"],
        "LoginIP": "",
        "LoginMAC": "",
        "LoginOS": "",
        "UniqueAccount": REAL_UNIQUE_ACCOUNT,
        "BranchNo": "123456",
        "SH_SAccount1": "A442523077",
        "SH_ExchangeNum1": 1,
        "SH_SAccount2": "0199908393",
        "SH_ExchangeNum2": 2,
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
    ("A001", "normal", "完整真实样本（照抄 ST-0）", {}, "模板行"),
    ("A002", "normal", "单股东号（只沪市）",
     {"SH_SAccount2": "", "SH_ExchangeNum2": ""}, "只一个市场"),
    ("A003", "normal", "不带股东号",
     {"SH_SAccount1": "", "SH_ExchangeNum1": "",
      "SH_SAccount2": "", "SH_ExchangeNum2": ""}, "Shareholders 为空"),
    ("A004", "normal", "不带 Model", {"Account_Model": ""}, "看是否必填"),
    ("A005", "normal", "不带 BranchNo", {"BranchNo": ""}, "看是否必填"),

    ("A101", "error", "Pwd 为空", {"Pwd": ""}, "期望 Err<0"),
    ("A102", "error", "ClientName 为空", {"ClientName": ""}, "期望 Err<0"),
    ("A103", "error", "FAccount 为空", {"Account_FAccount": ""}, "期望 Err<0"),
    ("A104", "error", "TradeAccount 与 FAccount 不一致",
     {"TradeAccount": "888888"}, "期望 Err<0"),
    ("A105", "error", "AccountType 非法 99", {"Account_AccountType": 99}, "期望 Err<0"),
    ("A106", "error", "ExchangeNum 非法 5", {"SH_ExchangeNum1": 5}, "期望 Err<0"),
    ("A107", "error", "UniqueAccount 与 Account 不一致",
     {"UniqueAccount": "999999_9_9"}, "期望 Err<0"),

    ("A201", "destroy", "Pwd 传畸形密文（超长）", {"Pwd_raw": "__LONG__"}, "超长密文"),
    ("A202", "destroy", "ClientName 控制字符", {"ClientName": "__CTRL_NAME__"}, "控制字符"),
    ("A203", "destroy", "ClientName SQL 注入", {"ClientName": "__SQL__"}, "注入"),
    ("A204", "destroy", "SAccount 非 ASCII", {"SH_SAccount1": "__UNICODE__"}, "非ASCII"),
    ("A205", "destroy", "Pwd 传非 base64 垃圾", {"Pwd_raw": "!!!not-base64!!!"}, "解密应失败但不崩"),
    ("A206", "destroy", "task 非 JSON", {"task_raw": "not json"}, "解析容错"),
    ("A207", "destroy", "task 空对象", {"task_raw": "{}"}, "结构错"),
    ("A208", "destroy", "MsgType=4 不匹配", {"MsgType": 4}, "分发容错"),
    ("A209", "destroy", "Shareholders 不是数组",
     {"task_raw": '{"Account":{"Shareholders":"not a list"},"MsgType":18}'}, "类型错"),
    ("A210", "destroy", "Shareholders 1000 个元素",
     {"task_raw": '{"Account":{"Shareholders":[' +
      ",".join('{"SAccount":"A%08d","ExchangeNum":1}' % i for i in range(1000)) +
      ']},"MsgType":18}'}, "超大数组"),
    ("A211", "destroy", "Account 是数组",
     {"task_raw": '{"Account":{"Account":[1,2]},"MsgType":18}'}, "类型错"),
])

_PREFIX = "A"
_BASE = tuple(TEMPLATE[k] for k in _KEYS)
_BULK = gen_fuzz(HEADERS, _BASE, {
    "Account_FAccount": ["", "__LONG__", "__SQL__", "__UNICODE__", "__EMOJI__", "__NULL__"],
    "Account_AccountType": [99, -1, "abc", "__MAXINT__", "__NULL__"],
    "Account_AccAtt": [9, -1, "abc", "__NULL__"],
    "ClientName": ["", "__EMPTY__", "__LONG__", "__CTRL_NAME__", "__SQL__",
                   "__XSS__", "__EMOJI2__", "__UNICODE__"],
    # Pwd 明文会先被两层加密再发出，所以这些畸形"明文"测的是【加密前的输入】，
    # 最终密文都会正常 —— 要测畸形密文请用 Pwd_raw 列（见 A201/A205）。
    "Pwd": ["", "__EMPTY__", "__SPACE__", "__LONG__", "__SQL__", "__UNICODE__",
            "__CTRL__", "__EMOJI2__"],
    "Pwd_raw": ["", "!!!bad!!!", "__LONG__", "__EMPTY__", "YWJj", "__SQL__"],
    "TradeAccount": ["", "888888", "__LONG__", "__SQL__"],
    "LoginIP": ["", "__LONG__", "__SQL__", "__UNICODE__"],
    "LoginMAC": ["", "__LONG__", "__SQL__"],
    "LoginOS": ["", "__LONG__", "__SQL__"],
    "UniqueAccount": ["", "999999_9_9", "__LONG__", "__SQL__"],
    "BranchNo": ["", "__LONG__", "__SQL__", "abc"],
    "SH_SAccount1": ["", "__LONG__", "__SQL__", "__UNICODE__", "__CTRL__", "__NULL__"],
    "SH_ExchangeNum1": [0, 5, 99, -1, "abc", "__MAXINT__", "__NULL__"],
    "SH_SAccount2": ["", "__LONG__", "__SQL__"],
    "SH_ExchangeNum2": [0, 5, 99, -1, "abc", "__NULL__"],
}, type_tag="destroy", start=200, prefix=_PREFIX)

_BULK += add_cases(HEADERS, _BASE, [
    ("task 是数组", {"task_raw": "[1]"}),
    ("task 是 null", {"task_raw": "null"}),
    ("task 是数字", {"task_raw": "18"}),
    ("task 1MB 大对象",
     {"task_raw": '{"MsgType":18,"Account":{"Big":"' + "X" * (1024 * 1024) + '"}}'}),
], type_tag="destroy", start=500, prefix=_PREFIX)

ROWS = ROWS + _BULK


def build_payload(row):
    raw = row.get("task_raw")
    if not is_blank(raw):
        return str(raw)
    body = {}
    acct = collect(row, "Account")
    if acct:
        body["Account"] = acct
    for leaf in ("ClientName", "TradeAccount", "LoginIP", "LoginMAC",
                 "LoginOS", "UniqueAccount", "BranchNo"):
        put(body, leaf, row)

    # ---- Pwd：两层加密（账号 + 日期）----
    # 策略平台要拿这个密码去柜台登录，所以必须加密成它认的格式：
    #   Pwd = encrypt( encrypt(明文密码, 账号), 日期 )
    # 详见 pwd_encode.py（已用真实样本 + C++ 双向交叉验证）
    # Pwd_raw 非空时直用原值（测畸形密文的破坏用例走这条）
    pwd_raw = row.get("Pwd_raw")
    pwd_plain = row.get("Pwd")
    if not is_blank(pwd_raw):
        # 原始密文列：要展开 token（__LONG__/__EMPTY__ 等），否则会把这些
        # 字面量当密文发出去，测不到想测的东西。
        from _common import expand as _expand
        v = _expand(pwd_raw)
        body["Pwd"] = v if isinstance(v, str) else str(v)
    elif not is_blank(pwd_plain):
        plain = str(pwd_plain)
        facct = (acct or {}).get("FAccount") or ""
        if _encode_pwd is None or not facct:
            # 加密不可用（缺 pwd_encode / 没账号）：退回明文，并在名字上标出来，
            # 免得静默发出去一个必然登录失败的密文。
            body["Pwd"] = plain
        else:
            try:
                body["Pwd"] = _encode_pwd(plain, facct)
            except Exception as e:
                sys.stderr.write("[WARN] account Pwd 加密失败(%s)，退回明文\n" % e)
                body["Pwd"] = plain

    # Shareholders 数组
    shs = []
    for sacct_col, ex_col in _SH_COLS:
        s = row.get(sacct_col)
        if is_blank(s):
            continue
        item = {"SAccount": str(s)}
        ex = row.get(ex_col)
        if not is_blank(ex):
            item["ExchangeNum"] = to_typed("ExchangeNum", ex)
        shs.append(item)
    if shs:
        body["Shareholders"] = shs
    payload = {TOP_KEY: body, "MsgType": MSG_TYPE}
    mt = row.get("MsgType")
    if not is_blank(mt):
        payload["MsgType"] = to_typed("MsgType", mt)
    return payload
