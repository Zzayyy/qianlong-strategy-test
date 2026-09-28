# -*- coding: utf-8 -*-
"""
interfaces/remove.py —— 接口定义：remove（删除条件单, MsgType=8）
================================================================
报文形态：
    {"remove":{
        "Account":{"AccountType":7,"AccAtt":7,"FAccount":"12345679"},
        "UniqueAccount":"1234569_7_7",
        "Ref":"20260604000001"},
     "MsgType":8}

★ Ref 必须是平台上真实存在的单号。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (ZH, REAL_ACCOUNT, REAL_UNIQUE_ACCOUNT, collect, is_blank,
                     put, to_typed, gen_fuzz, add_cases)

NAME = "remove"
TITLE = "删除条件单 (remove, MsgType=8)"
MSG_TYPE = 8
TOP_KEY = "remove"
REF_KEY = "Ref"

HEADERS = [
    ("case_no", ZH["case_no"]),
    ("case_type", ZH["case_type"]),
    ("case_desc", ZH["case_desc"]),
    ("Account_Model", ZH["Model"]),
    ("Account_AccountType", ZH["AccountType"]),
    ("Account_AccAtt", ZH["AccAtt"]),
    ("Account_FAccount", ZH["FAccount"]),
    ("UniqueAccount", ZH["UniqueAccount"]),
    ("Ref", "要删除的条件单号(须真实存在)"),
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
        "UniqueAccount": REAL_UNIQUE_ACCOUNT,
        "Ref": "__REF1__",
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
    ("R001", "normal", "删除一张真实单（Ref 需存在）", {}, "模板行"),
    ("R002", "normal", "不带 UniqueAccount", {"UniqueAccount": ""}, "看是否必填"),

    # ---------- error ----------
    ("R101", "error", "Ref 为空", {"Ref": ""}, "期望 Err<0"),
    ("R102", "error", "Ref 不存在", {"Ref": "20991231000000"}, "期望 ref not exist"),
    ("R103", "error", "FAccount 为空", {"Account_FAccount": ""}, "期望 Err<0"),
    ("R104", "error", "AccountType 非法 99", {"Account_AccountType": 99}, "期望 Err<0"),
    ("R105", "error", "UniqueAccount 与 Account 不一致",
     {"UniqueAccount": "999999_9_9"}, "期望 Err<0"),

    # ---------- destroy ----------
    ("R201", "destroy", "Ref 超长 1000", {"Ref": "__LONG__"}, "超长"),
    ("R202", "destroy", "Ref SQL 注入", {"Ref": "__SQL__"}, "注入"),
    ("R203", "destroy", "task 非 JSON", {"task_raw": "not json"}, "解析容错"),
    ("R204", "destroy", "task 空对象", {"task_raw": "{}"}, "结构错"),
    ("R205", "destroy", "MsgType=4 不匹配", {"MsgType": 4}, "分发容错"),
    ("R206", "destroy", "Account 是数组",
     {"task_raw": '{"remove":{"Account":[1,2],"Ref":"X"},"MsgType":8}'}, "类型错"),
])

_PREFIX = "R"
_BASE = tuple(TEMPLATE[k] for k in _KEYS)
_BULK = gen_fuzz(HEADERS, _BASE, {
    "Ref": ["", "__EMPTY__", "__LONG__", "__LONG10__", "__SQL__", "__CTRL__",
            "abc", "__EMOJI__", "20991231000000"],
    "Account_FAccount": ["", "__LONG__", "__SQL__", "__UNICODE__"],
    "Account_AccountType": [99, -1, "abc", "__MAXINT__"],
    "Account_AccAtt": [9, -1, "abc"],
    "UniqueAccount": ["", "999999_9_9", "__LONG__", "__SQL__"],
}, type_tag="destroy", start=200, prefix=_PREFIX)

_BULK += add_cases(HEADERS, _BASE, [
    ("task 是数组", {"task_raw": "[1]"}),
    ("task 是 null", {"task_raw": "null"}),
    ("task 是数字", {"task_raw": "0"}),
    ("task 1MB 大对象",
     {"task_raw": '{"MsgType":8,"remove":{"Big":"' + "X" * (1024 * 1024) + '"}}'}),
], type_tag="destroy", start=400, prefix=_PREFIX)

ROWS = ROWS + _BULK


def build_payload(row):
    raw = row.get("task_raw")
    if not is_blank(raw):
        return str(raw)
    body = {}
    acct = collect(row, "Account")
    if acct:
        body["Account"] = acct
    for leaf in ("UniqueAccount", "Ref"):
        put(body, leaf, row)
    payload = {TOP_KEY: body, "MsgType": MSG_TYPE}
    mt = row.get("MsgType")
    if not is_blank(mt):
        payload["MsgType"] = to_typed("MsgType", mt)
    return payload
