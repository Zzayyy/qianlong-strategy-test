# -*- coding: utf-8 -*-
"""
interfaces/modify.py —— 接口定义：modify（修改条件单, MsgType=11）
================================================================
报文形态（文档 + 真实结构）：
    {"modify":{
        "Account":{"Model":0,"FAccount":"010100011300","AccountType":7,"AccAtt":6},
        "UniqueAccount":"010100011300_7_6","CondType":1,
        "CondName":"name1-modified","CondDesc":"desc1","Validity":0,
        "Ref":"20260924084991",          <-- 要改的那张单
        "Entrust":{...}, "CondPrice":{...}},
     "MsgType":11}

★ Ref 很关键：必须是【平台上真实存在的单号】，否则会被拒（ref not exist）。
  用法二选一：
    * Excel 里写 __REF1__ 之类的 token（发送时按当天+序号展开）—— 需要与 create 对齐
    * 用 send_test.py --ref <单号> 覆盖整列
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (ZH, REAL_ACCOUNT, REAL_UNIQUE_ACCOUNT, REAL_BLOCKS,
                     collect, is_blank, put, to_typed, gen_fuzz, gen_cross,
                     add_cases)

NAME = "modify"
TITLE = "修改条件单 (modify, MsgType=11)"
MSG_TYPE = 11
TOP_KEY = "modify"
REF_KEY = "Ref"          # make_excel 的 --ref-spec 用

# modify 只带部分嵌套块（实测/文档里是 Entrust + CondPrice）
BLOCKS = [("Entrust", dict(REAL_BLOCKS[0][1])),
          ("CondPrice", dict(REAL_BLOCKS[5][1]))]

HEADERS = [
    ("case_no", ZH["case_no"]),
    ("case_type", ZH["case_type"]),
    ("case_desc", ZH["case_desc"]),
    ("Account_Model", ZH["Model"]),
    ("Account_AccountType", ZH["AccountType"]),
    ("Account_AccAtt", ZH["AccAtt"]),
    ("Account_FAccount", ZH["FAccount"]),
    ("CondType", "条件类型"),
    ("CondName", ZH["CondName"]),
    ("CondDesc", ZH["CondDesc"]),
    ("Validity", ZH["Validity"]),
    ("UniqueAccount", ZH["UniqueAccount"]),
    ("Ref", "要修改的条件单号(须真实存在)"),
]
for prefix, sample in BLOCKS:
    for leaf in sample:
        HEADERS.append(("%s_%s" % (prefix, leaf),
                        "%s-%s" % (prefix, ZH.get(leaf, leaf))))
HEADERS += [
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
        "CondType": 1,
        "CondName": "name1-modified",
        "CondDesc": "desc1",
        "Validity": 0,
        "UniqueAccount": REAL_UNIQUE_ACCOUNT,
        "Ref": "__REF1__",
    })
    for prefix, sample in BLOCKS:
        for leaf, val in sample.items():
            r["%s_%s" % (prefix, leaf)] = val
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
    ("M001", "normal", "改 CondName/CondDesc/TriggerPrice（真实结构）", {},
     "模板行（Ref 需真实存在）"),
    ("M002", "normal", "只改 CondName",
     {k: "" for k in _KEYS if k.startswith(("Entrust_", "CondPrice_"))},
     "精简字段"),

    # ---------- error ----------
    ("M101", "error", "Ref 为空", {"Ref": ""}, "期望 Err<0"),
    ("M102", "error", "Ref 不存在（格式合法）",
     {"Ref": "20991231000000"}, "期望 ref not exist"),
    ("M103", "error", "Ref 与账号不匹配", {"Account_FAccount": "9999999"},
     "期望 Err<0"),
    ("M104", "error", "UniqueAccount 与 Account 不一致",
     {"UniqueAccount": "999999_9_9"}, "期望 Err<0"),
    ("M105", "error", "CondType 非法 99", {"CondType": 99}, "期望 Err<0"),

    # ---------- destroy ----------
    ("M201", "destroy", "Ref 超长 1000", {"Ref": "__LONG__"}, "超长"),
    ("M202", "destroy", "CondName 控制字符", {"CondName": "__CTRL_NAME__"}, "控制字符"),
    ("M203", "destroy", "CondName SQL 注入", {"CondName": "__SQL__"}, "注入"),
    ("M204", "destroy", "EntrustPrice 为 nan", {"Entrust_EntrustPrice": "__PRICE_NAN__"}, "NaN"),
    ("M205", "destroy", "task 非 JSON", {"task_raw": "not a json"}, "解析容错"),
    ("M206", "destroy", "task 空对象", {"task_raw": "{}"}, "结构错"),
    ("M207", "destroy", "MsgType=4（与 modify 不匹配）", {"MsgType": 4}, "分发容错"),
    ("M208", "destroy", "Account 塞成字符串",
     {"task_raw": '{"modify":{"Account":"010100011300","Ref":"X"},"MsgType":11}'}, "类型错"),
])

_PREFIX = "M"
_BASE = tuple(TEMPLATE[k] for k in _KEYS)
_BULK = gen_fuzz(HEADERS, _BASE, {
    "Ref": ["", "__EMPTY__", "__LONG__", "__LONG10__", "__SQL__", "__CTRL__",
            "abc", "20991231000000"],
    "Account_FAccount": ["", "__LONG__", "__SQL__", "__UNICODE__", "__NULL__"],
    "Account_AccountType": [99, -1, "abc", "__MAXINT__", "__NULL__"],
    "Account_AccAtt": [9, -1, "abc", "__NULL__"],
    "CondType": [0, 99, -1, "abc", "__NULL__"],
    "CondName": ["", "__EMPTY__", "__LONG__", "__SQL__", "__XSS__", "__EMOJI2__"],
    "CondDesc": ["", "__LONG__", "__CTRL__", "__NEWLINE__"],
    "UniqueAccount": ["", "999999_9_9", "__LONG__", "__SQL__"],
    "Validity": [99, -1, "abc", "__NULL__"],
    "Entrust_EntrustPrice": ["", "abc", "-1", "__PRICE_NAN__", "__PRICE_INF__", "__LONG__"],
    "Entrust_EntrustAmount": [0, -1, "abc", "__HUGE__", "__NULL__"],
    "Entrust_ExchangeNum": [0, 9, -1, "abc", "__NULL__"],
    "CondPrice_TriggerPrice": ["", "abc", "-1", "__PRICE_NAN__", "__LONG__"],
    "CondPrice_Op": ["", ">>", "abc", "__SQL__"],
}, type_tag="destroy", start=200, prefix=_PREFIX)

_BULK += add_cases(HEADERS, _BASE, [
    ("task 是数组", {"task_raw": "[1,2]"}),
    ("task 是 null", {"task_raw": "null"}),
    ("task 是数字", {"task_raw": "999"}),
    ("task 1MB 大对象",
     {"task_raw": '{"MsgType":11,"modify":{"Big":"' + "X" * (1024 * 1024) + '"}}'}),
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
    for leaf in ("CondType", "CondName", "CondDesc", "Validity",
                 "UniqueAccount", "Ref"):
        put(body, leaf, row)
    for prefix, _s in BLOCKS:
        blk = collect(row, prefix)
        if blk:
            body[prefix] = blk
    payload = {TOP_KEY: body, "MsgType": MSG_TYPE}
    mt = row.get("MsgType")
    if not is_blank(mt):
        payload["MsgType"] = to_typed("MsgType", mt)
    return payload
