# -*- coding: utf-8 -*-
"""
interfaces/create.py —— 接口定义：create（数据中台 → 策略平台，插入条件单）
==========================================================================
字段来源：从 136 db0 的真实 ST-0 流里扒出来的结构 + 文档样本。

报文形态（实测真实流量）：
    {"create":{
        "Account":{"Model":0,"FAccount":"010100011300","AccountType":7,"AccAtt":6},
        "CondType":1, "CondName":"name1", "CondDesc":"desc1",
        "ValidDate":"2026-10-24", "UniqueAccount":"010100011300_7_6",
        "Ref":"20260924000136", "Validity":0,
        "Entrust":{...}, "CfgExceedPrice":{...}, ... "CondTargetProfit":{...},
        "RunCount":0, "Mode":1, ...},
     "MsgType":4}

★ CondType 实测【现场全是 1】：2026-09-28 扫 136 ST-0 的 3269 条 create，
  CondType=1 占 3262 条（99.8%），且每条都带全套 Cond* 块。
  早先写的 2 已按实时流量纠正为 1。

列名约定（同 datahub_test）：
    Account_FAccount / Entrust_EntrustPrice / CondPrice_TriggerPrice ...
    下划线表示嵌套层级；留空 = 该字段不出现；__EMPTY__ = 出现但为空串。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (ZH, REAL_ACCOUNT, REAL_UNIQUE_ACCOUNT, REAL_BLOCKS,
                     collect, is_blank, put, to_typed, gen_fuzz, gen_cross,
                     add_cases)

NAME = "create"
TITLE = "插入条件单 (create, MsgType=4)"
MSG_TYPE = 4
TOP_KEY = "create"

# ==================== 表头 ====================
HEADERS = [
    ("case_no", ZH["case_no"]),
    ("case_type", ZH["case_type"]),
    ("case_desc", ZH["case_desc"]),
    # 账号
    ("Account_Model", ZH["Model"]),
    ("Account_AccountType", ZH["AccountType"]),
    ("Account_AccAtt", ZH["AccAtt"]),
    ("Account_FAccount", ZH["FAccount"]),
    # 条件单主字段
    ("CondType", "条件类型(1价格/2时间/3百分比/4合约止盈止损/5标的止盈止损)"),
    ("CondName", ZH["CondName"]),
    ("CondDesc", ZH["CondDesc"]),
    ("ValidDate", ZH["ValidDate"]),
    ("UniqueAccount", ZH["UniqueAccount"]),
    ("Ref", ZH["Ref"]),
    ("Validity", ZH["Validity"]),
    ("Mode", ZH["Mode"]),
    ("RunCount", ZH["RunCount"]),
    ("Status", ZH["Status"]),
    ("Removed", ZH["Removed"]),
    ("BeginDateTime", ZH["BeginDateTime"]),
    ("EndDateTime", ZH["EndDateTime"]),
    ("CreateDateTime", ZH["CreateDateTime"]),
    ("RunDateTime", ZH["RunDateTime"]),
    ("CloseDateTime", ZH["CloseDateTime"]),
    ("ModifyDateTime", ZH["ModifyDateTime"]),
    ("TriggerDateTime", ZH["TriggerDateTime"]),
    ("Reason", ZH["Reason"]),
]

# 嵌套块：Entrust_xxx / CfgExceedPrice_xxx / CondPrice_xxx ...
for prefix, sample in REAL_BLOCKS:
    for leaf, val in sample.items():
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
    """一条完整的真实样本行（normal 模板）。"""
    r = _blank_row()
    r.update({
        "Account_Model": REAL_ACCOUNT["Model"],
        "Account_AccountType": REAL_ACCOUNT["AccountType"],
        "Account_AccAtt": REAL_ACCOUNT["AccAtt"],
        "Account_FAccount": REAL_ACCOUNT["FAccount"],
        "CondType": 1,
        "CondName": "name1",
        "CondDesc": "desc1",
        "ValidDate": "__TODAY_D30__",
        "UniqueAccount": REAL_UNIQUE_ACCOUNT,
        "Ref": "__REF1__",
        "Validity": 0,
        "Mode": 1,
        "RunCount": 0,
        "Status": 0,
        "Removed": False,
    })
    for prefix, sample in REAL_BLOCKS:
        for leaf, val in sample.items():
            r["%s_%s" % (prefix, leaf)] = val
    return r


TEMPLATE = _real_row()

# ==================== 手写用例 ====================
def _rows_to_tuples(items):
    """[(case_no, type, desc, {列:值}), ...] -> 完整宽度元组列表"""
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
    # ---------- normal ----------
    ("C001", "normal", "完整真实样本（照抄 ST-0 实时流量，CondType=1）", {},
     "模板行，字段值勿改"),
    ("C002", "normal", "CondType=1 且只保留 CondPrice（其余条件块都不带）",
     dict({k: "" for k in _KEYS if k.startswith(
         ("Cfg", "CondPercent", "CondTime", "CondLoss", "CondTarget",
          "CondProfit"))}, CondType=1),
     "看平台是否接受'只声明一个条件块'"),
    ("C003", "normal", "精简字段（只留 Entrust + CondPrice）",
     {k: "" for k in _KEYS if k.startswith(("Cfg", "CondTarget"))
      and not k.startswith("CondPrice")},
     "去掉各种 Cfg 与 Target 块，看是否接受"),
    ("C004", "normal", "不带 Mode / Status / RunCount",
     {"Mode": "", "Status": "", "RunCount": ""}, "省略可选字段"),
    ("C005", "normal", "不带 Ref（让服务端分配）",
     {"Ref": ""}, "看 Ref 是否必填"),

    # ---------- error：应被拒绝 ----------
    ("C101", "error", "FAccount 为空", {"Account_FAccount": ""},
     "期望 Err<0"),
    ("C102", "error", "AccountType 非法 99", {"Account_AccountType": 99},
     "期望 Err<0"),
    ("C103", "error", "AccAtt 非法 9", {"Account_AccAtt": 9}, "期望 Err<0"),
    ("C104", "error", "ExchangeNum 非法 5",
     {"Entrust_ExchangeNum": 5, "CondPrice_ExchangeNum": 5}, "期望 Err<0"),
    ("C105", "error", "EntrustAmount 为 0", {"Entrust_EntrustAmount": 0},
     "期望 Err<0"),
    ("C106", "error", "EntrustPrice 为负", {"Entrust_EntrustPrice": "-0.05"},
     "期望 Err<0"),
    ("C107", "error", "UniqueAccount 与 Account 不一致",
     {"UniqueAccount": "999999_9_9"}, "期望 Err<0"),
    ("C108", "error", "ValidDate 为过去日期",
     {"ValidDate": "2020-01-01"}, "期望 Err<0"),
    ("C109", "error", "Ref 重复（已存在的单号）",
     {"Ref": "__REF1__"}, "期望 ref already inserted"),

    # ---------- destroy：畸形/极端 ----------
    ("C201", "destroy", "FAccount 超长 1000", {"Account_FAccount": "__LONG__"},
     "超长账号"),
    ("C202", "destroy", "CondName 含控制字符", {"CondName": "__CTRL_NAME__"},
     "控制字符"),
    ("C203", "destroy", "CondName SQL 注入", {"CondName": "__SQL__"}, "注入"),
    ("C204", "destroy", "CondName XSS", {"CondName": "__XSS__"}, "注入"),
    ("C205", "destroy", "EntrustPrice 传字符串 abc",
     {"Entrust_EntrustPrice": "abc"}, "类型错"),
    ("C206", "destroy", "EntrustAmount 传 1e100",
     {"Entrust_EntrustAmount": "__SCI__"}, "科学计数"),
    ("C207", "destroy", "CondType 传字符串 'two'", {"CondType": "two"}, "类型错"),
    ("C208", "destroy", "Ref 超长 10000", {"Ref": "__LONG10__"}, "超长单号"),
    ("C209", "destroy", "整条 task 不是 JSON（task_raw 直用）",
     {"task_raw": "this is not json at all"}, "解析容错"),
    ("C210", "destroy", "task 是数组不是对象",
     {"task_raw": "[1,2,3]"}, "结构错"),
    ("C211", "destroy", "task 是空对象",
     {"task_raw": "{}"}, "结构错"),
    ("C212", "destroy", "MsgType 改成 999（与子对象不匹配）",
     {"MsgType": 999}, "分发容错"),
    ("C213", "destroy", "MsgType 改成字符串 four", {"MsgType": "four"}, "类型错"),
    ("C214", "destroy", "全字段塞超长（约 10 万字）",
     {k: "__LONG10__" for k in _KEYS
      if k.startswith(("Cond", "Entrust", "Cfg", "Account_")) and k not in ("CondType",)},
     "超大报文"),
])

# ==================== 批量生成 ====================
_PREFIX = "C"
_BASE = tuple(TEMPLATE[k] for k in _KEYS)
_BULK = []

# 字段级 fuzz：对关键列逐个灌畸形值
_BULK += gen_fuzz(HEADERS, _BASE, {
    "Account_FAccount": ["", "__EMPTY__", "__LONG__", "__SQL__", "__UNICODE__",
                         "__EMOJI__", "__NULL__", "__CTRL__"],
    "Account_AccountType": [99, -1, "abc", "__MAXINT__", "__HUGE__", "__HEX__", "__SCI__", "__NULL__"],
    "Account_AccAtt": [9, -1, "abc", "__MAXINT__", "__NULL__"],
    "Account_Model": [1, 2, -1, "abc", "__MAXINT__"],
    "CondType": [0, 99, -1, "abc", "__MAXINT__", "__NULL__", "__HUGE__"],
    "CondName": ["", "__EMPTY__", "__LONG__", "__CTRL_NAME__", "__SQL__",
                 "__XSS__", "__EMOJI2__", "__UNICODE__", "__JSON__"],
    "CondDesc": ["", "__LONG__", "__SQL__", "__XSS__", "__NEWLINE__", "__TAB__", "__NULLBYTE__"],
    "ValidDate": ["", "2020-01-01", "2026-13-40", "00000000", "99999999",
                  "2026/01/01", "99991231", "abc", "__LONG__"],
    "UniqueAccount": ["", "999999_9_9", "__LONG__", "__SQL__", "__UNI__"],
    "Ref": ["", "__LONG__", "__LONG10__", "__SQL__", "__CTRL__", "abc", "__EMOJI__"],
    "Validity": [99, -1, "abc", "__MAXINT__", "__NULL__"],
    "Mode": [99, -1, "abc", "__NULL__"],
    "Entrust_ContractCode": ["", "__LONG__", "__SQL__", "__UNI__", "__NULL__"],
    "Entrust_ExchangeNum": [0, 9, -1, "abc", "__MAXINT__", "__NULL__"],
    "Entrust_EntrustPrice": ["", "0", "-0.05", "abc", "__PRICE_HUGE__",
                             "__PRICE_SCI__", "__PRICE_NAN__", "__PRICE_INF__",
                             "__LONG__", "__EMPTY__"],
    "Entrust_MarketOrderType": [0, 99, -1, "abc", "__MAXINT__", "__NULL__"],
    "Entrust_BSType": [0, 3, 99, -1, "abc", "__NULL__"],
    "Entrust_OCType": [0, 3, 99, -1, "abc", "__NULL__"],
    "Entrust_EntrustAmount": [0, -100, "abc", "__MAXINT__", "__HUGE__",
                              "__SCI__", "__NULL__", "__FLOATNAN__"],
    "Entrust_CoveredType": ["abc", 2, "__NULL__", "__TRUE__", "__EMPTY__"],
    "Entrust_FOK": ["abc", 2, "__NULL__", "__TRUE__"],
    "CfgExceedPrice_PriceStepBuy": ["abc", "__MAXINT__", "__NEGINT__", "__NULL__"],
    "CfgExceedPrice_Decimals": [0, -1, 99, "abc", "__NULL__"],
    "CfgFixedSplit_LimitStep": [0, -1, "abc", "__HUGE__", "__NULL__"],
    "CfgRandSplit_LimitMax": [-1, 0, "abc", "__HUGE__", "__NULL__"],
    "CfgAppend_IntervalSec": [0, -1, "abc", "__MAXINT__", "__NULL__"],
    "CfgAppend_Repeat": [0, -1, 999999, "abc", "__NULL__"],
    "CondPrice_Op": ["", ">>", "abc", "__SQL__", "__LONG__"],
    "CondPrice_TriggerPrice": ["", "-1", "abc", "__PRICE_HUGE__", "__PRICE_NAN__"],
    "CondPercent_TriggerPercent": ["", "-1", "200", "abc", "__PRICE_NAN__"],
    "CondTime_TriggerDate": ["", "20200101", "99999999", "abc", "__LONG__"],
    "CondTime_TriggerTime": ["", "999999", "abc", "__LONG__"],
    "CondLoss_Method": [0, 99, -1, "abc", "__NULL__"],
    "CondLoss_ValueType": [0, 99, -1, "abc", "__NULL__"],
    "CondLoss_Value": ["", "-1", "abc", "__PRICE_NAN__"],
    "CondProfit_WithdrawType": [0, 99, -1, "abc", "__NULL__"],
    "CondProfit_Withdraw": ["", "-1", "abc", "__PRICE_NAN__"],
    "CondTargetProfit_StockCode": ["", "__LONG__", "__SQL__", "__UNI__"],
    "Status": [99, -1, "abc", "__NULL__"],
    "Removed": ["abc", 2, "__NULL__"],
}, type_tag="destroy", start=200, prefix=_PREFIX)

# 结构级破坏用例
_BULK += add_cases(HEADERS, _BASE, [
    ("task 是数字 12345", {"task_raw": "12345"}),
    ("task 是字符串 hello", {"task_raw": '"hello"'}),
    ("task 是 null", {"task_raw": "null"}),
    ("task 是数组空", {"task_raw": "[]"}),
    ("task 是 true", {"task_raw": "true"}),
    ("task 含重复键", {"task_raw": '{"MsgType":4,"create":{"Ref":"A"},"Ref":"B","create":{"Ref":"C"}}'}),
    ("task 含 200 层嵌套",
     {"task_raw": '{"MsgType":4,' + '{"create":' * 200 + '{}' + '}' * 200 + '}'}),
    ("task 是 500 个垃圾字段",
     {"task_raw": '{"MsgType":4,"create":{},"' + '","'.join('junk%d' % i for i in range(500)) + '":"x"}'}),
    ("task 是 1MB 大对象",
     {"task_raw": '{"MsgType":4,"create":{"Big":"' + "X" * (1024 * 1024) + '"}}'}),
], type_tag="destroy", start=600, prefix=_PREFIX)

# 交叉破坏：账号变体 × 字段注入
_ACCT_VARIANTS = [
    ("010100011300", 7, 6, "0"),
    ("010100011300", 7, 6, ""),
    ("010100011300", 7, 0, "0"),
    ("010100011300", 1, 6, "0"),
    (" 010100011300", 7, 6, "0"),
    ("999999", 1, 6, ""),
    ("", 7, 6, "0"),
]
_BULK += gen_cross(HEADERS, _BASE, _ACCT_VARIANTS, injects=[
    ("Account_FAccount", "__LONG__"),
    ("Account_FAccount", "__SQL__"),
    ("Ref", "__LONG__"),
    ("CondName", "__CTRL_NAME__"),
    ("Account_AccountType", "__HUGE__"),
    ("Entrust_EntrustPrice", "__PRICE_SCI__"),
], type_tag="destroy", start=300, prefix=_PREFIX)

ROWS = ROWS + _BULK


# ==================== 报文构造 ====================
def build_payload(row):
    """Excel 一行 -> create 报文字典。"""
    # task_raw 非空：整条直用（用于测非法 JSON），返回原始字符串
    raw = row.get("task_raw")
    if not is_blank(raw):
        return str(raw)

    body = {}
    # Account 子对象
    acct = collect(row, "Account")
    if acct:
        body["Account"] = acct
    # 条件单主字段
    for leaf in ("CondType", "CondName", "CondDesc", "ValidDate", "UniqueAccount",
                 "Ref", "Validity", "Mode", "RunCount", "Status", "Removed",
                 "BeginDateTime", "EndDateTime", "CreateDateTime", "RunDateTime",
                 "CloseDateTime", "ModifyDateTime", "TriggerDateTime", "Reason"):
        put(body, leaf, row)
    # 嵌套块
    for prefix, _sample in REAL_BLOCKS:
        blk = collect(row, prefix)
        if blk:
            body[prefix] = blk

    payload = {TOP_KEY: body, "MsgType": MSG_TYPE}
    mt = row.get("MsgType")
    if not is_blank(mt):
        payload["MsgType"] = to_typed("MsgType", mt)
    return payload
