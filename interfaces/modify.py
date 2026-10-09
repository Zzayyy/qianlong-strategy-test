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
from _common import (ZH, REAL_ACCOUNT, REAL_UNIQUE_ACCOUNT,
                     collect, is_blank, put, to_typed, gen_fuzz, gen_cross,
                     add_cases, fmt_account, unique_account, ACCOUNT_START,
                     contract_code, target_stock_code)

NAME = "modify"
TITLE = "修改条件单 (modify, MsgType=11)"
MSG_TYPE = 11
TOP_KEY = "modify"
REF_KEY = "Ref"          # make_excel 的 --ref-spec 用

# ==================== modify 专属的嵌套块 ====================
# ★ 这里【不再复用 create 的 REAL_BLOCKS】，而是 modify 自己的一套。
#   原因：现场给的 modify 目标报文与 create 有两处实质差异，
#   直接复用会让 create 跟着变（create 的既有用例含义就变了）：
#     1) 带的块更多：create 全套 12 个，modify 也要 12 个（原来只带 Entrust+CondPrice）
#     2) 同名块取值不同：MarketOrderType 15→1、
#        CondPrice.TriggerPrice 0.123→0.0675、
#        CondLoss/CondProfit 合约 10011743→可配合约代码、
#        CondTargetLoss/CondTargetProfit 标的 510050→可配标的代码
#        （ExchangeNum / PriceUnit / EntrustAmount 与 create 一致：2 / 0.0001 / 20）
#   ⚠ 字段名大小写【保持与 create 一致的大写】
#     （Op / Method / ValueType / WithdrawType / Withdraw /
#       TriggerPercent / TriggerDate / TriggerTime），
#     不采用目标报文里的小写写法 —— 详见 _common.py 顶部说明（实测大写）。
# ★ 合约代码/标的代码取自 _common.contract_code() / target_stock_code()
#   （环境变量 ST_CONTRACT_CODE / ST_TARGET_STOCK_CODE 可覆盖，GUI 里能填）。
MODIFY_BLOCKS = [
    ("CfgExceedPrice", {"ExchangeNum": 2, "StockCode": contract_code(),
                        "StockName": "50ETF", "PriceStepBuy": -1,
                        "PriceStepSell": 1, "PriceType": 0,
                        "PriceUnit": "0.0001", "Decimals": 4}),
    ("CfgFixedSplit", {"LimitBase": 50, "LimitStep": 50, "LimitInterval": 300,
                       "MarketBase": 10, "MarketStep": 10,
                       "MarketInterval": 300}),
    ("CfgRandSplit", {"LimitBase": 1, "LimitMin": 1, "LimitMax": 5,
                      "LimitInterval": 300, "MarketBase": 1, "MarketMin": 1,
                      "MarketMax": 5, "MarketInterval": 300}),
    ("CfgAppend", {"MarketOrderType": 15, "Tick": 2, "IntervalSec": 300,
                   "Repeat": 2, "EndWithdraw": False}),
    ("Entrust", {"ContractCode": contract_code(), "ExchangeNum": 2,
                 "EntrustPrice": "0.1033", "MarketOrderType": 1,
                 "CoveredType": False, "BSType": 1, "OCType": 1,
                 "PriceUnit": "0.0001", "EntrustAmount": 20}),
    ("CondPrice", {"ContractCode": contract_code(), "ExchangeNum": 2,
                   "Op": ">", "TriggerPrice": "0.0675"}),
    ("CondPercent", {"ContractCode": contract_code(), "ExchangeNum": 2,
                     "Op": ">", "TriggerPercent": "5.25"}),
    # 字段名是 TriggerDate（不是 TriggerData）；时间按现场要求固定。
    ("CondTime", {"ContractCode": contract_code(), "ExchangeNum": 2,
                  "TriggerDate": "20260918", "TriggerTime": "093100"}),
    ("CondLoss", {"ContractCode": contract_code(), "ExchangeNum": 2,
                  "Method": 1, "ValueType": 1, "Value": "0.0675"}),
    ("CondTargetLoss", {"StockCode": target_stock_code(), "ExchangeNum": 2,
                        "Method": 1, "ValueType": 1, "Value": "0.0675"}),
    ("CondProfit", {"ContractCode": contract_code(), "ExchangeNum": 2, "Method": 1,
                    "ValueType": 1, "Value": "0.0675", "WithdrawType": 2,
                    "Withdraw": "0.50"}),
    ("CondTargetProfit", {"StockCode": target_stock_code(), "ExchangeNum": 2,
                          "Method": 1, "ValueType": 1, "Value": "0.0675",
                          "WithdrawType": 2, "Withdraw": "0.50"}),
]
BLOCKS = MODIFY_BLOCKS

HEADERS = [
    ("case_no", ZH["case_no"]),
    ("case_type", ZH["case_type"]),
    ("case_desc", ZH["case_desc"]),
    ("Account_AccountType", ZH["AccountType"]),
    ("Account_AccAtt", ZH["AccAtt"]),
    ("Account_FAccount", ZH["FAccount"]),
    ("CondType", "条件类型"),
    ("CondName", ZH["CondName"]),
    ("CondDesc", ZH["CondDesc"]),
    ("Validity", ZH["Validity"]),
    ("UniqueAccount", ZH["UniqueAccount"]),
    ("Ref", "要修改的条件单号(须真实存在)"),
    ("Status", ZH["Status"]),
    ("Mode", ZH["Mode"]),
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
    # ★ 不再写 Account_Model：现场给的 modify 目标报文里 Account 只有
    #   FAccount/AccountType/AccAtt（create 才带 Model）。
    r.update({
        "Account_AccountType": REAL_ACCOUNT["AccountType"],
        "Account_AccAtt": REAL_ACCOUNT["AccAtt"],
        "Account_FAccount": REAL_ACCOUNT["FAccount"],
        "CondType": 1,
        "CondName": "strategy_name",
        "CondDesc": "strategy_desc",
        "Validity": 0,
        "UniqueAccount": REAL_UNIQUE_ACCOUNT,
        "Ref": "__REF1__",
        "Status": 0,
        "Mode": 1,
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


# ==================== 批量 normal（--bulk-normal）====================
def build_bulk_rows(count, start=0, ref_seq=1):
    """生成 count 行 normal：每行一个不同账号 + 引用一个【当天已存在的单号】。

    ★ 为什么必须和 create 对齐：
      modify 的 Ref 必须是平台上真实存在的单号，否则得到 `ref not exist`。
      单号规则是「当天日期 + 全局递增序号」，create 批量第 i 行建出来的单
      就是当天第 i 号。所以这里第 i 行引用 __REF{i}__，只要
      **create 按行顺序先发一遍**，这些单号就都真实存在了。

    参数：
      start   : 账号 6 位序号起点（默认 _common.ACCOUNT_START，与 create 对齐）
      ref_seq : 当天全局起始单号。当日已经建过 ref_seq-1 张单时传入它，
                这样本表第 i 行引用的就是 __REF{ref_seq+i-1}__。
                ⚠ 若 create 用的是默认（从 1 开始），这里保持 1 即可。

    发送顺序：create 全部 -> 再发本表（见 docs/guide.md 第 6 节）。
    """
    count = int(count)
    if count <= 0:
        raise ValueError("条数必须 > 0")
    start = int(start) or ACCOUNT_START
    ref_seq = max(1, int(ref_seq))
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
        r[idx["case_no"]] = "MB%05d" % (i + 1)
        r[idx["case_type"]] = "normal"
        r[idx["case_desc"]] = ("账号%s 修改当日第 %d 号单（需先 create）"
                               % (facct, ref_seq + i))
        r[idx["Account_AccountType"]] = at
        r[idx["Account_AccAtt"]] = aa
        r[idx["Account_FAccount"]] = facct
        r[idx["UniqueAccount"]] = unique_account(facct, at, aa)
        r[idx["Ref"]] = "__REF%d__" % (ref_seq + i)
        r[idx["CondName"]] = "mod%d" % (i + 1)
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
    for leaf in ("CondType", "CondName", "CondDesc", "Validity",
                 "UniqueAccount", "Ref", "Status", "Mode"):
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
