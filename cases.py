# -*- coding: utf-8 -*-
"""
cases.py —— 「数据中台 → 策略平台」测试用例库
==============================================
数据来源分三层，优先级从高到低：
  1. 从 136 db0 的真实流 ST-0（37567 条）里扒出来的真实报文结构
     —— create 的 32 个字段、MsgType 18 的 Account 结构，都是照抄真实流量的。
  2. 数据中台to策略平台.txt / 请求接口doc.txt 里的协议样本。
  3. 类推出来的破坏用例（token 占位，发送时展开）。

用例类型沿用 datahub_test 的分类：
    normal  —— 合法报文，用于性能/联通性基线
    destroy —— 畸形/极端报文，测策略平台健壮性（不崩、不泄漏、不误处理）

用法：
    from cases import build_payload, CASES, filter_cases
    p = build_payload("create", "normal")
    c = filter_cases(type_tag="destroy", interface="create")
"""
import copy
import datetime
import json
import random
import time

import protocol as P

# ================================================================ 破坏 token
# 与 datahub_test/interfaces/_common.py 的 TOKEN_MAP 保持同样的思路：
# Excel/配置里存不下控制字符和超长串，用占位符，发送时才展开。
TOKEN_MAP = {
    "__LONG__": "F" * 1000,
    "__LONG10__": "F" * 10000,
    "__CTRL__": "999993\x00\x01\x02",
    "__CTRL_NAME__": "张\x00\x01三",
    "__NULLBYTE__": "abc\x00def",
    "__TAB__": "a\tb\tc",
    "__NEWLINE__": "line1\nline2\r\nline3",
    "__RTL__": "\u202e\u202dmoc.qq\u202c",
    "__ZWSP__": "a\u200bb\u200cc",
    "__BOM__": "\ufeffhello",
    "__SQL__": "'; DROP TABLE users; --",
    "__SQL2__": "1 OR 1=1 --",
    "__XSS__": "<script>alert(1)</script>",
    "__XSS2__": "\"><img src=x onerror=alert(1)>",
    "__FMT__": "%s%n%p%n",
    "__PATH__": "../../etc/passwd",
    "__ZERO__": "0",
    "__NEG__": "-999999",
    "__HUGE__": "999999999999999999999999999999",
    "__MAXINT__": "2147483647",
    "__NEGINT__": "-2147483648",
    "__HEX__": "0x1F",
    "__SCI__": "1e100",
    "__LEAD0__": "007",
    "__FLOATINF__": "inf",
    "__FLOATNAN__": "NaN",
    "__FLOATNEG__": "-inf",
    "__SPACE__": "   ",
    "__BOOL_WEIRD__": "2",
    "__BOOL_STR__": "TRUE",
    "__BOOL_CN__": "是",
    "__EMOJI__": "\U0001F600" * 10,
    "__EMOJI2__": "\U0001F4A9" * 50,
    "__UNICODE__": "\U0001D54F\U0001D550\U0001D551",
    "__JSON__": '{"bad":"json"}',
    "__REPLACE_NULL__": "null",
    "__XML__": "<?xml version='1.0'?><a>",
    "__DATE_13__": "2026-13-40",
    "__DATE_ZERO__": "00000000",
    "__DATE_NINE__": "99999999",
    "__DATE_SLASH__": "2026/01/01",
    "__DATE_UNIX0__": "19700101",
    "__DATE_FAR__": "99991231",
    "__DATE_YEAR__": "2026-00-00",
    "__DATE_TIME__": "2026-01-01 99:99:99",
    "__PRICE_NEG__": "-0.050",
    "__PRICE_HUGE__": "999999999999.99",
    "__PRICE_SCI__": "1e50",
    "__PRICE_NAN__": "NaN",
    "__PRICE_INF__": "inf",
    "__PRICE_STR__": "abc",
    # 非字符串类型破坏（直接塞 JSON 原生类型）
    "__NULL__": None,
    "__TRUE__": True,
    "__FALSE__": False,
    "__EMPTYOBJ__": {},
    "__EMPTYLIST__": [],
    "__NEGOBJ__": -1,
}

_INT_LEAVES = {
    "Model", "AccountType", "AccAtt", "ExchangeNum", "CondType",
    "MarketOrderType", "BSType", "OCType", "EntrustAmount", "PriceType",
    "Decimals", "WithdrawSec", "LimitBase", "LimitStep", "LimitInterval",
    "MarketBase", "MarketStep", "MarketInterval", "LimitMin", "LimitMax",
    "MarketMin", "MarketMax", "Tick", "IntervalSec", "Repeat", "Method",
    "ValueType", "WithdrawType", "Mode", "RunCount", "Validity", "Status",
    "NtType", "Id",
}
_BOOL_LEAVES = {"CoveredType", "EndWithdraw", "Removed", "FOK"}


def expand(v):
    """把 token 展开成真实内容；非 token 原样返回。

    动态日期（两种格式，别混用）：
        __TODAY__ / __TODAY_D30__ / __TODAY_D_1__  -> YYYY-MM-DD（带横杠）
        __TODAY8__ / __TODAY_PLUS7__               -> YYYYMMDD（不带横杠）
    真实数据里 ValidDate 是 "2026-09-30"（带横杠），
    CondTime.TriggerDate 是 "20260910"（不带），所以必须分开。
    """
    if isinstance(v, str):
        s = v.strip()
        if s == "__TODAY__":
            return time.strftime("%Y-%m-%d")
        if s == "__TODAY8__":
            return time.strftime("%Y%m%d")
        # __TODAY_D30__ / __TODAY_D_1__ : 带横杠
        if s.startswith("__TODAY_D") and s.endswith("__"):
            try:
                n = int(s[len("__TODAY_D"):-2].lstrip("_"))
                d = datetime.date.today() + datetime.timedelta(days=n)
                return d.strftime("%Y-%m-%d")
            except Exception:
                return v
        # __TODAY_PLUS7__ : 不带横杠
        if s.startswith("__TODAY_PLUS") and s.endswith("__"):
            try:
                n = int(s[len("__TODAY_PLUS"):-2])
                d = datetime.date.today() + datetime.timedelta(days=n)
                return d.strftime("%Y%m%d")
            except Exception:
                return v
        if s in TOKEN_MAP:
            return TOKEN_MAP[s]
    return v


def to_typed(leaf, v):
    """按字段名把值转成 int / bool，转不了就保留原值（这本身就是一种破坏）。"""
    v = expand(v)
    if v is None or isinstance(v, (dict, list, bool)):
        return v
    s = str(v).strip()
    if s == "":
        return ""
    if leaf in _BOOL_LEAVES:
        if s.lower() in ("true", "1", "yes"):
            return True
        if s.lower() in ("false", "0", "no"):
            return False
        return s
    if leaf in _INT_LEAVES:
        try:
            return int(s)
        except (ValueError, TypeError):
            return s
    return v


# ================================================================ 基础模板
# 以下三块是从真实流量抄下来的（值做了脱敏/顺延），normal 用例以它们为底。

# --- Account 四要素：真实 010100011300 / 7 / 6 / Model=0 ---
REAL_ACCOUNT = {
    "Model": 0,
    "FAccount": "010100011300",
    "AccountType": 7,
    "AccAtt": 6,
}
REAL_UNIQUE_ACCOUNT = "010100011300_7_6"

# --- create 的 Entrust / 各 Cfg / 各 Cond（照抄 ST-0 真实样本）---
REAL_ENTRUST = {
    "ContractCode": "90007461", "ExchangeNum": 2, "EntrustPrice": "0.1033",
    "MarketOrderType": 15, "CoveredType": False, "BSType": 1, "OCType": 1,
    "PriceUnit": "0.0001", "EntrustAmount": 20, "FOK": False,
}
REAL_CFG_EXCEED = {
    "ExchangeNum": 2, "StockCode": "90007461", "StockName": "50ETF",
    "PriceStepBuy": -1, "PriceStepSell": 1, "PriceType": 0,
    "PriceUnit": "0.0001", "Decimals": 4,
}
REAL_CFG_FIXED_SPLIT = {
    "LimitBase": 50, "LimitStep": 50, "LimitInterval": 300,
    "MarketBase": 10, "MarketStep": 10, "MarketInterval": 300,
}
REAL_CFG_RAND_SPLIT = {
    "LimitBase": 1, "LimitMin": 1, "LimitMax": 5, "LimitInterval": 300,
    "MarketBase": 1, "MarketMin": 1, "MarketMax": 5, "MarketInterval": 300,
}
REAL_CFG_APPEND = {
    "MarketOrderType": 15, "Tick": 2, "IntervalSec": 3, "Repeat": 2,
    "EndWithdraw": False,
}
REAL_COND_PRICE = {"ContractCode": "90007461", "ExchangeNum": 2,
                   "Op": ">", "TriggerPrice": "0.234"}
REAL_COND_PERCENT = {"ContractCode": "90007461", "ExchangeNum": 2,
                     "Op": ">", "TriggerPercent": "5.25"}
REAL_COND_TIME = {"ContractCode": "90007461", "ExchangeNum": 2,
                  "TriggerDate": "__TODAY_PLUS7__", "TriggerTime": "093240"}
REAL_COND_LOSS = {"ContractCode": "10011743", "ExchangeNum": 1,
                  "Method": 1, "ValueType": 1, "Value": "0.0675"}
REAL_COND_TARGET_LOSS = {"StockCode": "510050", "ExchangeNum": 1,
                         "Method": 1, "ValueType": 1, "Value": "3.216"}
REAL_COND_PROFIT = {"ContractCode": "10011743", "ExchangeNum": 1, "Method": 1,
                    "ValueType": 1, "Value": "0.0675", "WithdrawType": 2,
                    "Withdraw": "0.50"}
REAL_COND_TARGET_PROFIT = {"StockCode": "510050", "ExchangeNum": 1,
                           "Method": 1, "ValueType": 1, "Value": "3.216",
                           "WithdrawType": 2, "Withdraw": "0.50"}

# --- MsgType 18 的 Account（照抄真实 ST-0）---
REAL_ACCOUNT_INFO = {
    "Account": dict(REAL_ACCOUNT),
    "ClientName": "张国昌",
    "Pwd": "caQ0awvz26RmuxLzhAWJVkkfyqrX3tQWs2rGT6xSmxnYi+0DttbRAjU1UO0W2DBlWMKMo8Cc8fJs6dAHgLdrKQ==",
    "TradeAccount": "010100011300",
    "LoginIP": "",
    "LoginMAC": "",
    "LoginOS": "",
    "UniqueAccount": REAL_UNIQUE_ACCOUNT,
    "BranchNo": "123456",
    "Shareholders": [{"SAccount": "A442523077", "ExchangeNum": 1},
                     {"SAccount": "0199908393", "ExchangeNum": 2}],
}


def _ref(n=1):
    """条件单号：YYYYMMDD + 6 位序号（真实样本形如 20260924000136）。"""
    return "%s%06d" % (time.strftime("%Y%m%d"), max(1, int(n)))


def _rand_ref():
    return _ref(random.randint(1, 999999))


# ================================================================ 各 MsgType 构造
def make_create(ref=None, account=None, unique_account=None, cond_type=2,
                ts_first=False, extra=None):
    """MsgType=4 插入条件单。

    真实流量的键顺序是 {"create":{...},"MsgType":4}（create 在前）。
    文档写的是 {"MsgType":4,"create":{...}}。
    ts_first=True 时用文档那种顺序（测解析器是否依赖键顺序）。
    """
    body = {
        "Account": dict(account or REAL_ACCOUNT),
        "CondType": cond_type,
        "CondName": "name1",
        "CondDesc": "desc1",
        "ValidDate": "__TODAY_D30__",
        "Entrust": dict(REAL_ENTRUST),
        "CfgExceedPrice": dict(REAL_CFG_EXCEED),
        "CfgFixedSplit": dict(REAL_CFG_FIXED_SPLIT),
        "CfgRandSplit": dict(REAL_CFG_RAND_SPLIT),
        "CfgAppend": dict(REAL_CFG_APPEND),
        "CondPrice": dict(REAL_COND_PRICE),
        "CondPercent": dict(REAL_COND_PERCENT),
        "CondTime": dict(REAL_COND_TIME),
        "CondLoss": dict(REAL_COND_LOSS),
        "CondTargetLoss": dict(REAL_COND_TARGET_LOSS),
        "CondProfit": dict(REAL_COND_PROFIT),
        "CondTargetProfit": dict(REAL_COND_TARGET_PROFIT),
        "UniqueAccount": unique_account or REAL_UNIQUE_ACCOUNT,
        "Ref": ref or _rand_ref(),
        "Validity": 0,
        "CreateDateTime": time.strftime("%Y-%m-%d %H:%M:%S"),
        "RunDateTime": time.strftime("%Y-%m-%d %H:%M:%S"),
        "CloseDateTime": "",
        "RunCount": 0,
        "BeginDateTime": "",
        "EndDateTime": "",
        "Status": 0,
        "Removed": False,
        "ModifyDateTime": time.strftime("%Y-%m-%d %H:%M:%S"),
        "TriggerDateTime": "",
        "Reason": "",
        "Mode": 1,
    }
    if extra:
        body.update(extra)
    p = {"create": body, "MsgType": P.MSG_CREATE}
    if ts_first:
        p = {"MsgType": P.MSG_CREATE, "create": body}
    return p


def make_remove(ref=None, account=None, unique_account=None, ts_first=False, extra=None):
    """MsgType=8 删除条件单（文档格式）。"""
    body = {
        "Account": dict(account or REAL_ACCOUNT),
        "UniqueAccount": unique_account or REAL_UNIQUE_ACCOUNT,
        "Ref": ref or _rand_ref(),
    }
    if extra:
        body.update(extra)
    p = {"remove": body, "MsgType": P.MSG_REMOVE}
    if ts_first:
        p = {"MsgType": P.MSG_REMOVE, "remove": body}
    return p


def make_modify(ref=None, account=None, unique_account=None, ts_first=False, extra=None):
    """MsgType=11 修改条件单（文档格式）。"""
    body = {
        "Account": dict(account or REAL_ACCOUNT),
        "UniqueAccount": unique_account or REAL_UNIQUE_ACCOUNT,
        "CondType": 1,
        "CondName": "name1-modified",
        "CondDesc": "desc1",
        "Validity": 0,
        "Ref": ref or _rand_ref(),
        "Entrust": dict(REAL_ENTRUST),
        "CondPrice": {"ContractCode": "90007461", "ExchangeNum": 2,
                      "Op": ">", "TriggerPrice": "0.500"},
    }
    if extra:
        body.update(extra)
    p = {"modify": body, "MsgType": P.MSG_MODIFY}
    if ts_first:
        p = {"MsgType": P.MSG_MODIFY, "modify": body}
    return p


def make_pwd_update(account=None, unique_account=None, pwd="123456",
                    ts_first=False, extra=None):
    """MsgType=17 用户密码更换（文档格式）。"""
    body = {
        "Account": dict(account or REAL_ACCOUNT),
        "Pwd": pwd,
        "UniqueAccount": unique_account or REAL_UNIQUE_ACCOUNT,
    }
    if extra:
        body.update(extra)
    p = {"pwdUpdate": body, "MsgType": P.MSG_PWD_UPDATE}
    if ts_first:
        p = {"MsgType": P.MSG_PWD_UPDATE, "pwdUpdate": body}
    return p


def make_account(account=None, unique_account=None, account_info=None,
                 ts_first=False, extra=None):
    """MsgType=18 用户信息（照抄真实 ST-0 结构）。

    注意：真实报文里这个子对象键名就是 "Account"（大写），
    而 MsgType=4 的子对象也叫 "Account"（那是 create 里的字段），
    两者同名但不同层，别混。
    """
    body = copy.deepcopy(account_info or REAL_ACCOUNT_INFO)
    if account:
        body["Account"] = dict(account)
    if unique_account:
        body["UniqueAccount"] = unique_account
    if extra:
        body.update(extra)
    p = {"Account": body, "MsgType": P.MSG_ACCOUNT}
    if ts_first:
        p = {"MsgType": P.MSG_ACCOUNT, "Account": body}
    return p


BUILDERS = {
    "create": make_create,
    "remove": make_remove,
    "modify": make_modify,
    "pwdUpdate": make_pwd_update,
    "account": make_account,
}

MSG_TYPE_OF = {
    "create": P.MSG_CREATE,
    "remove": P.MSG_REMOVE,
    "modify": P.MSG_MODIFY,
    "pwdUpdate": P.MSG_PWD_UPDATE,
    "account": P.MSG_ACCOUNT,
}

INTERFACES = ["create", "modify", "remove", "pwdUpdate", "account"]


def build_payload(interface="create", type_tag="normal", **kw):
    """构造一条报文字典。interface 见 INTERFACES。"""
    if interface not in BUILDERS:
        raise ValueError("未知 interface: %s（可选 %s）" % (interface, INTERFACES))
    p = BUILDERS[interface](**kw)
    return _apply_tokens(p, type_tag)


def _apply_tokens(obj, type_tag):
    """把 token 展开、按字段名做类型转换（递归）。

    normal 用例里的 __TODAY_PLUSn__ 等动态占位也要展开，所以无条件跑一遍。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                out[k] = _apply_tokens(v, type_tag)
            else:
                out[k] = to_typed(k, v)
        return out
    if isinstance(obj, list):
        return [_apply_tokens(v, type_tag) for v in obj]
    return expand(obj)


# ================================================================ 破坏用例
# 每个破坏用例：(编号, interface, 说明, 构造函数)
# 构造函数返回一个 dict 报文（已经可以直接 XADD）。
def _mutate(base_fn, path, value):
    """在 base_fn() 的报文上按点路径改一个值，返回新报文。"""
    p = base_fn()
    node = p
    parts = path.split(".")
    for k in parts[:-1]:
        if not isinstance(node, dict) or k not in node:
            # 路径不存在（比如 create 下的字段被删了）就直接放到顶层
            node = node.setdefault(k, {}) if isinstance(node, dict) else node
        else:
            node = node[k]
    if isinstance(node, dict):
        node[parts[-1]] = value
    return p


def _drop(base_fn, path):
    """删掉一个字段。"""
    p = base_fn()
    node = p
    parts = path.split(".")
    for k in parts[:-1]:
        node = node.get(k, {}) if isinstance(node, dict) else {}
    if isinstance(node, dict):
        node.pop(parts[-1], None)
    return p


def _raw(text):
    """返回一个"不是合法 JSON"的原始串（由调用方放进 task）。"""
    return text


# --- 通用破坏：把一个字段替换成各种畸形值 ---
FUZZ_VALUES = [
    ("", "空串"),
    ("__SPACE__", "全空格"),
    ("__NULL__", "JSON null"),
    ("__LONG__", "超长 1000"),
    ("__LONG10__", "超长 10000"),
    ("__CTRL__", "控制字符"),
    ("__NULLBYTE__", "NUL 字节"),
    ("__SQL__", "SQL 注入"),
    ("__XSS__", "XSS"),
    ("__FMT__", "格式化串"),
    ("__EMOJI2__", "emoji 长串"),
    ("__UNICODE__", "数学符号"),
    ("__JSON__", "嵌套 JSON 串"),
    ("__HUGE__", "超大整数"),
    ("__NEGINT__", "极小整数"),
    ("__SCI__", "科学计数"),
    ("__FLOATNAN__", "NaN"),
    ("__FLOATINF__", "inf"),
    ("__HEX__", "十六进制"),
    ("__BOOL_WEIRD__", "非法布尔 2"),
    ("__NEGOBJ__", "负数 -1"),
    ("__TRUE__", "JSON true"),
    ("__FALSE__", "JSON false"),
    ("__EMPTYOBJ__", "空对象"),
    ("__EMPTYLIST__", "空数组"),
]


def gen_fuzz_cases(interface, fields, start=100):
    """对若干字段逐个灌畸形值，生成破坏用例。

    fields: [点路径, ...]，如 ["create.Account.FAccount", "create.Entrust.EntrustPrice"]
    """
    base_fn = BUILDERS[interface]
    cases = []
    n = start
    for path in fields:
        for val, desc in FUZZ_VALUES:
            leaf = path.rsplit(".", 1)[-1]
            cases.append((
                "D%03d" % n, interface,
                "%s = %s(%r)" % (path, desc, val),
                (lambda bf=base_fn, p=path, v=val: _mutate(bf, p, v)),
            ))
            n += 1
    return cases


# --- create 的破坏字段（真实报文里的关键路径）---
CREATE_FUZZ_FIELDS = [
    "create.Account.FAccount",
    "create.Account.AccountType",
    "create.Account.AccAtt",
    "create.Account.Model",
    "create.UniqueAccount",
    "create.CondType",
    "create.CondName",
    "create.CondDesc",
    "create.ValidDate",
    "create.Ref",
    "create.Validity",
    "create.Entrust.ContractCode",
    "create.Entrust.ExchangeNum",
    "create.Entrust.EntrustPrice",
    "create.Entrust.MarketOrderType",
    "create.Entrust.CoveredType",
    "create.Entrust.BSType",
    "create.Entrust.OCType",
    "create.Entrust.EntrustAmount",
    "create.CfgExceedPrice.PriceStepBuy",
    "create.CfgExceedPrice.Decimals",
    "create.CfgFixedSplit.LimitStep",
    "create.CfgRandSplit.LimitMax",
    "create.CfgAppend.IntervalSec",
    "create.CfgAppend.Repeat",
    "create.CondPrice.Op",
    "create.CondPrice.TriggerPrice",
    "create.CondPercent.TriggerPercent",
    "create.CondTime.TriggerDate",
    "create.CondTime.TriggerTime",
    "create.CondLoss.Method",
    "create.CondLoss.ValueType",
    "create.CondLoss.Value",
    "create.CondProfit.WithdrawType",
    "create.CondProfit.Withdraw",
    "create.CondTargetProfit.StockCode",
    "create.RunCount",
    "create.Mode",
]

# --- 删字段（测必填校验）---
CREATE_DROP_FIELDS = [
    "create.Account", "create.Account.FAccount", "create.Account.AccountType",
    "create.Account.AccAtt", "create.UniqueAccount", "create.Ref",
    "create.CondType", "create.Entrust", "create.Entrust.ContractCode",
    "create.Entrust.EntrustPrice", "create.Entrust.EntrustAmount",
    "create.ValidDate", "create.CondPrice", "create.MsgType",
]

# --- 结构级破坏（不是改字段值，而是改整体形状）---
def _struct_cases():
    out = []

    def add(no, desc, fn, iface="create"):
        out.append((no, iface, desc, fn))

    add("D001", "task 不是 JSON（纯文本）",
        lambda: _raw("this is not json at all"))
    add("D002", "task 是 JSON 但非对象（数组）",
        lambda: _apply_tokens([1, 2, 3], "destroy"))
    add("D003", "task 是 JSON 但非对象（字符串）",
        lambda: _apply_tokens("hello", "destroy"))
    add("D004", "task 是 JSON 但非对象（数字）",
        lambda: _apply_tokens(12345, "destroy"))
    add("D005", "空对象 {}",
        lambda: {})
    add("D006", "有 MsgType 但无 create 子对象",
        lambda: {"MsgType": P.MSG_CREATE})
    add("D007", "有 create 但无 MsgType",
        lambda: {"create": {"Account": dict(REAL_ACCOUNT)}})
    add("D008", "MsgType 是未知值 999",
        lambda: {"MsgType": 999, "create": {"Account": dict(REAL_ACCOUNT)}})
    add("D009", "MsgType 是负数 -1",
        lambda: {"MsgType": -1, "create": {"Account": dict(REAL_ACCOUNT)}})
    add("D010", "MsgType 是字符串 'four'",
        lambda: {"MsgType": "four", "create": {"Account": dict(REAL_ACCOUNT)}})
    add("D011", "MsgType 是 null",
        lambda: {"MsgType": None, "create": {"Account": dict(REAL_ACCOUNT)}})
    add("D012", "create 是数组不是对象",
        lambda: {"MsgType": P.MSG_CREATE, "create": [1, 2, 3]})
    add("D013", "create 是字符串",
        lambda: {"MsgType": P.MSG_CREATE, "create": "not an object"})
    add("D014", "create 是 null",
        lambda: {"MsgType": P.MSG_CREATE, "create": None})
    add("D015", "同时带 create+modify+remove 三个子对象",
        lambda: {"MsgType": P.MSG_CREATE,
                 "create": {"Account": dict(REAL_ACCOUNT)},
                 "modify": {"Account": dict(REAL_ACCOUNT)},
                 "remove": {"Account": dict(REAL_ACCOUNT)}})
    add("D016", "MsgType 与子对象不匹配（MsgType=4 但只有 remove）",
        lambda: {"MsgType": P.MSG_CREATE,
                 "remove": {"Account": dict(REAL_ACCOUNT)}})
    add("D017", "顶层塞 500 个无用字段",
        lambda: dict({"MsgType": P.MSG_CREATE,
                      "create": {"Account": dict(REAL_ACCOUNT)}},
                     **{"junk%d" % i: "x" * 20 for i in range(500)}))
    add("D018", "超深嵌套（200 层）",
        lambda: _deep(200))
    add("D019", "task 含 JSON 注入（重复键）",
        lambda: _raw('{"MsgType":4,"create":{"Ref":"A"},"Ref":"B","create":{"Ref":"C"}}'))
    add("D020", "task 是超大 JSON（约 1MB）",
        lambda: {"MsgType": P.MSG_CREATE,
                 "create": {"Account": dict(REAL_ACCOUNT),
                            "BigBlob": "X" * (1024 * 1024)}})
    add("D021", "Account 是数组",
        lambda: _mutate(make_create, "create.Account", [1, 2, 3]))
    add("D022", "Account 是 null",
        lambda: _mutate(make_create, "create.Account", None))
    add("D023", "Account 是字符串",
        lambda: _mutate(make_create, "create.Account", "010100011300"))
    add("D024", "Ref 是空串",
        lambda: _mutate(make_create, "create.Ref", ""))
    add("D025", "Ref 是数字（类型错）",
        lambda: _mutate(make_create, "create.Ref", 20260924000136))
    add("D026", "Entrust 是数组",
        lambda: _mutate(make_create, "create.Entrust", []))
    add("D027", "CfgExceedPrice 是 null",
        lambda: _mutate(make_create, "create.CfgExceedPrice", None))
    add("D028", "Shareholders 不是数组（MsgType=18）",
        _acct_shareholders_bad)
    add("D029", "Shareholders 元素缺 SAccount（MsgType=18）",
        lambda: _acct_shareholders([{"ExchangeNum": 1}]))
    add("D030", "Shareholders 有 1000 个（MsgType=18）",
        lambda: _acct_shareholders(
            [{"SAccount": "A%08d" % i, "ExchangeNum": 1} for i in range(1000)]))
    add("D031", "UniqueAccount 与 Account 不一致",
        lambda: _mutate(make_create, "create.UniqueAccount", "999999_9_9"))
    add("D032", "Account 四要素互相矛盾（Type=1 但 UniqueAccount 是 _7_6）",
        lambda: _mutate(make_create, "create.Account.AccountType", 1))
    add("D033", "ValidDate 是过去日期",
        lambda: _mutate(make_create, "create.ValidDate", "2020-01-01"))
    add("D034", "CondTime.TriggerDate 是过去日期",
        lambda: _mutate(make_create, "create.CondTime.TriggerDate", "20200101"))
    add("D035", "EntrustPrice 为负",
        lambda: _mutate(make_create, "create.Entrust.EntrustPrice", "-0.050"))
    add("D036", "EntrustAmount 为 0",
        lambda: _mutate(make_create, "create.Entrust.EntrustAmount", 0))
    add("D037", "EntrustAmount 为负",
        lambda: _mutate(make_create, "create.Entrust.EntrustAmount", -100))
    add("D038", "ExchangeNum 非法值 9",
        lambda: _mutate(make_create, "create.Entrust.ExchangeNum", 9))
    add("D039", "BSType/OCType 非法组合（BSType=99）",
        lambda: _mutate(make_create, "create.Entrust.BSType", 99))
    add("D040", "全是 __LONG10__ 的 create（约 10 万字）",
        _all_long)

    # 每个 MsgType 都来一条"最小合法"和"完全空"
    for i, iface in enumerate(INTERFACES):
        add("D%03d" % (60 + i), "%s 最小合法报文" % iface,
            (lambda f=iface: {"MsgType": MSG_TYPE_OF[f],
                              P.MSG_PAYLOAD_KEY[MSG_TYPE_OF[f]]: {}}), iface)
    for i, iface in enumerate(INTERFACES):
        add("D%03d" % (70 + i), "%s 空对象" % iface, (lambda: {}), iface)

    return out


def _deep(n):
    d = {"MsgType": P.MSG_CREATE}
    node = d
    for _ in range(n):
        node["create"] = {}
        node = node["create"]
    return d


def _acct_shareholders(val):
    p = make_account()
    p["Account"]["Shareholders"] = val
    return p


def _acct_shareholders_bad():
    return _acct_shareholders("not a list")


def _all_long():
    """把 create 里所有字符串字段都塞超长串。"""
    def fill(o, depth=0):
        if depth > 6:
            return o
        if isinstance(o, dict):
            return {k: (fill(v, depth + 1) if isinstance(v, (dict, list))
                        else ("F" * 10000 if isinstance(v, str) else v))
                    for k, v in o.items()}
        if isinstance(o, list):
            return [fill(v, depth + 1) for v in o]
        return o
    return fill(make_create())


# 组装 ALL_CASES
ALL_CASES = []
ALL_CASES += _struct_cases()
ALL_CASES += gen_fuzz_cases("create", CREATE_FUZZ_FIELDS, start=200)
ALL_CASES += gen_fuzz_cases("modify",
                            ["modify.Account.FAccount", "modify.UniqueAccount",
                             "modify.Ref", "modify.Entrust.EntrustPrice",
                             "modify.CondPrice.TriggerPrice"],
                            start=600)
ALL_CASES += gen_fuzz_cases("remove",
                            ["remove.Account.FAccount", "remove.UniqueAccount",
                             "remove.Ref"],
                            start=700)
ALL_CASES += gen_fuzz_cases("pwdUpdate",
                            ["pwdUpdate.Account.FAccount", "pwdUpdate.Pwd",
                             "pwdUpdate.UniqueAccount"],
                            start=800)
ALL_CASES += gen_fuzz_cases("account",
                            ["Account.Account.FAccount", "Account.Pwd",
                             "Account.TradeAccount", "Account.ClientName",
                             "Account.UniqueAccount", "Account.BranchNo"],
                            start=900)

# 去重（同一个编号只留一条）
_seen = set()
_dedup = []
for c in ALL_CASES:
    if c[0] in _seen:
        continue
    _seen.add(c[0])
    _dedup.append(c)
ALL_CASES = _dedup

# normal 用例（性能基线用）
NORMAL_CASES = []
for i, iface in enumerate(INTERFACES):
    NORMAL_CASES.append((
        "N%03d" % (i + 1), iface, "%s 正常报文（真实结构）" % iface,
        (lambda f=iface: build_payload(f, "normal")),
    ))


def filter_cases(type_tag="all", interface="all", cases=""):
    """按类型/接口/编号筛选用例。

    type_tag : all / normal / destroy
    interface: all / create / modify / ...
    cases    : "" 或 "D001,D003-D010,N001"（支持区间）
    """
    pool = []
    if type_tag in ("all", "normal"):
        pool += NORMAL_CASES
    if type_tag in ("all", "destroy"):
        pool += ALL_CASES
    if interface and interface != "all":
        pool = [c for c in pool if c[1] == interface]
    if cases:
        want = _parse_case_spec(cases)
        pool = [c for c in pool if c[0] in want]
    return pool


def _parse_case_spec(spec):
    want = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                pre = "".join(ch for ch in a if not ch.isdigit())
                na, nb = int("".join(ch for ch in a if ch.isdigit())), \
                         int("".join(ch for ch in b if ch.isdigit()))
                for i in range(min(na, nb), max(na, nb) + 1):
                    want.add("%s%03d" % (pre or "D", i))
            except ValueError:
                want.add(part)
        else:
            want.add(part)
    return want


def payload_text(p):
    """用例返回值 -> 真正要写进 task 字段的字符串。

    normal 用例返回 dict -> json.dumps
    破坏用例可能返回 str（非法 JSON），原样用。
    """
    if isinstance(p, str):
        return p
    return json.dumps(p, ensure_ascii=False, separators=(",", ":"))


def summarize():
    from collections import Counter
    c = Counter(x[1] for x in ALL_CASES)
    d = Counter(x[1] for x in NORMAL_CASES)
    print("normal 用例 : %d  %s" % (len(NORMAL_CASES), dict(d)))
    print("destroy 用例: %d  %s" % (len(ALL_CASES), dict(c)))
    print("合计        : %d" % (len(NORMAL_CASES) + len(ALL_CASES)))


if __name__ == "__main__":
    summarize()
    print()
    for no, iface, desc, fn in NORMAL_CASES:
        t = payload_text(fn())
        print("[%s] %-9s %-28s len=%d" % (no, iface, desc, len(t)))
    print()
    for no, iface, desc, fn in ALL_CASES[:12]:
        t = payload_text(fn())
        print("[%s] %-9s %-40s len=%d" % (no, iface, desc, len(t)))
    print("...")
