# -*- coding: utf-8 -*-
"""
interfaces/_common.py —— 接口定义的公共工具
==========================================
和 datahub_test/interfaces/_common.py 同源：token 展开、类型转换、
按前缀嵌套、批量用例生成。

约定（与 datahub_test 一致）：
  * 列名用【下划线】表示嵌套层级：Entrust_ContractCode -> payload["Entrust"]["ContractCode"]
  * 单元格留空 = 该字段【不出现】在报文里
  * 想表达"空串"要写 __EMPTY__，想表达 JSON null 写 __NULL__
    （否则留空会被当成"字段不存在"，测不出"值为空"这类用例）
  * 值里可以放 token（__LONG__ / __SQL__ …），发送时才展开
"""
import datetime
import re
import time

# ==================== 破坏 token ====================
TOKEN_MAP = {
    "__EMPTY__": "",              # 空串（区别于"留空=不出现"）
    "__NULL__": None,             # JSON null
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
    "__TRUE__": True,
    "__FALSE__": False,
    "__NEGOBJ__": -1,
    "__EMPTYOBJ__": {},
    "__EMPTYLIST__": [],
}

# 按叶子名区分的 int / bool 字段
INT_LEAVES = {
    "Model", "AccountType", "AccAtt", "ExchangeNum", "CondType",
    "MarketOrderType", "BSType", "OCType", "EntrustAmount", "PriceType",
    "Decimals", "WithdrawSec", "LimitBase", "LimitStep", "LimitInterval",
    "MarketBase", "MarketStep", "MarketInterval", "LimitMin", "LimitMax",
    "MarketMin", "MarketMax", "Tick", "IntervalSec", "Repeat", "Method",
    "ValueType", "WithdrawType", "Mode", "RunCount", "Validity", "Status",
    "MsgType",
}
BOOL_LEAVES = {"CoveredType", "EndWithdraw", "Removed", "FOK"}


def expand(v):
    """token 展开；非 token 原样返回。

    动态日期（两种格式别混）：
        __TODAY__ / __TODAY_D30__ / __TODAY_D_1__  -> YYYY-MM-DD（ValidDate 用）
        __TODAY8__ / __TODAY_PLUS7__               -> YYYYMMDD  （TriggerDate 用）

    动态条件单号（发送时才展开，日期取发送当天）：
        __REF3__    -> 单个，如 20260924000003
        __REF1_10__ -> 范围，展开成逗号分隔的 1~10 号（配合 Refs 列）
    """
    if isinstance(v, str):
        s = v.strip()
        if s == "__TODAY__":
            return time.strftime("%Y-%m-%d")
        if s == "__TODAY8__":
            return time.strftime("%Y%m%d")
        if s.startswith("__TODAY_D") and s.endswith("__"):
            try:
                n = int(s[len("__TODAY_D"):-2].lstrip("_"))
                return (datetime.date.today()
                        + datetime.timedelta(days=n)).strftime("%Y-%m-%d")
            except Exception:
                return v
        if s.startswith("__TODAY_PLUS") and s.endswith("__"):
            try:
                n = int(s[len("__TODAY_PLUS"):-2])
                return (datetime.date.today()
                        + datetime.timedelta(days=n)).strftime("%Y%m%d")
            except Exception:
                return v
        m = re.fullmatch(r"__REF(\d+)__", s)
        if m:
            return make_ref(m.group(1))
        m = re.fullmatch(r"__REF(\d+)_(\d+)__", s)
        if m:
            a, b = sorted((int(m.group(1)), int(m.group(2))))
            return ",".join(make_ref(i) for i in range(a, b + 1))
        if s in TOKEN_MAP:
            return TOKEN_MAP[s]
    return v


def make_ref(n=1):
    """按线上规则生成条件单号：日期(YYYYMMDD) + 6 位顺序号。

    真实样本形如 20260924000136 / 20260924084991。
    __REFn__ 在发送时才展开，日期取发送当天，便于与 create 当天造的单对齐。
    """
    return "%s%06d" % (time.strftime("%Y%m%d"), max(1, int(n)))


def to_typed(leaf, v):
    """按字段名转 int / bool；转不了保留原值（这本身就是一种破坏）。"""
    v = expand(v)
    if v is None or isinstance(v, (dict, list, bool)):
        return v
    s = str(v).strip()
    if s == "":
        return ""
    if leaf in BOOL_LEAVES:
        if s.lower() in ("true", "1", "yes"):
            return True
        if s.lower() in ("false", "0", "no"):
            return False
        return s
    if leaf in INT_LEAVES:
        try:
            return int(s)
        except (ValueError, TypeError):
            return s
    return v


def is_blank(v):
    """Excel 单元格是否视为"未填写"（= 该字段不出现在报文里）。"""
    return v is None or (isinstance(v, str) and v.strip() == "")


def collect(row, prefix):
    """把 row 里所有 prefix_xxx 的列收成一个嵌套子对象。

    例：prefix="Entrust" -> {"ContractCode":..., "ExchangeNum":...}
    空单元格跳过（= 字段不出现）；__EMPTY__ 会展开成 ""（= 字段出现但为空）。
    """
    out = {}
    pat = prefix + "_"
    for k, v in row.items():
        if not isinstance(k, str) or not k.startswith(pat):
            continue
        if is_blank(v):
            continue
        leaf = k[len(pat):]
        out[leaf] = to_typed(leaf, v)
    return out


def put(body, leaf, row, key=None):
    """把 row[key] 按 leaf 的类型放进 body（空则跳过）。"""
    key = key or leaf
    v = row.get(key)
    if is_blank(v):
        return
    body[leaf] = to_typed(leaf, v)


# ==================== 批量用例生成 ====================

def _col_index(headers, key):
    for i, (k, _) in enumerate(headers):
        if k == key:
            return i
    raise KeyError("列 %s 不在表头中" % key)


def gen_fuzz(headers, template, fuzz_by_key, type_tag="destroy", start=900,
             prefix=""):
    """字段级破坏：对 template 的每个 fuzz 字段逐一替换其值。

    fuzz_by_key: {列名: [值 或 (值, 说明), ...]}
    每 (字段, 值) 生成一行（基于 template 拷贝，只改该列）。
    """
    base = list(template)
    out = []
    n = start
    for col, vals in fuzz_by_key.items():
        ci = _col_index(headers, col)
        for item in vals:
            val, desc = item if isinstance(item, tuple) else (item, "%s=%r" % (col, item))
            row = base[:]
            row[_col_index(headers, "case_no")] = "%s%s%03d" % (prefix, type_tag[0].upper(), n)
            row[_col_index(headers, "case_type")] = type_tag
            row[_col_index(headers, "case_desc")] = desc
            row[ci] = val
            out.append(tuple(row))
            n += 1
    return out


def gen_cross(headers, template, variants, injects=(), type_tag="destroy",
              start=300, prefix=""):
    """交叉破坏：对每个 variant（账号变体）叠加每个 inject（字段注入）生成一行。

    variants: [(FAccount, AccountType, AccAtt, Model), ...]
    injects : [(列名, 值), ...]
    """
    base = list(template)
    keys = dict(headers)
    out = []
    n = start
    for facct, at, aa, model in variants:
        for col, val in injects:
            row = base[:]
            row[_col_index(headers, "case_no")] = "%s%s%03d" % (prefix, type_tag[0].upper(), n)
            row[_col_index(headers, "case_type")] = type_tag
            row[_col_index(headers, "case_desc")] = "账号%s 注入 %s=%s" % (facct, col, val)
            for k, v in (("FAccount", facct), ("AccountType", at),
                         ("AccAtt", aa), ("Model", model)):
                if k in keys:
                    row[_col_index(headers, k)] = v
            row[_col_index(headers, col)] = val
            out.append(tuple(row))
            n += 1
    return out


def add_cases(headers, template, items, type_tag, start, prefix=""):
    """通用：items 是 [(说明, {列: 值}), ...]，基于 template 逐条生成行。"""
    base = list(template)
    out = []
    n = start
    for desc, changes in items:
        row = base[:]
        row[_col_index(headers, "case_no")] = "%s%s%03d" % (prefix, type_tag[0].upper(), n)
        row[_col_index(headers, "case_type")] = type_tag
        row[_col_index(headers, "case_desc")] = desc
        for col, val in changes.items():
            row[_col_index(headers, col)] = val
        out.append(tuple(row))
        n += 1
    return out


# ==================== 批量测试账号号段（--bulk-normal 用）====================
# 真实账号 010100011300 = 前缀 010100 + 6 位序号 011300。
# 批量账号从 ACCOUNT_START 起递增，只保证【格式】与真实样本一致。
# 约定与 datahub_test/interfaces/_common.py 对齐，便于两边造的数据互通。
ACCOUNT_PREFIX = "010100"      # 12 位云单账号的前 6 位固定前缀
ACCOUNT_START = 11301          # 默认起点：紧邻真实账号 010100011300 之后

# ==================== 真实样本（照抄 ST-0 真实流量）====================
REAL_ACCOUNT = {"Model": 0, "FAccount": "010100011300",
                "AccountType": 7, "AccAtt": 6}
REAL_UNIQUE_ACCOUNT = "010100011300_7_6"


def fmt_account(seq):
    """把 6 位序号格式化成云单账号（ACCOUNT_PREFIX + 6位序号）。"""
    return "%s%06d" % (ACCOUNT_PREFIX, int(seq))


def unique_account(facct, account_type=None, acc_att=None):
    """按线上规则拼 UniqueAccount：<FAccount>_<AccountType>_<AccAtt>。

    【坑】批量造账号时它必须跟着 FAccount 一起变。若照抄模板的
    `010100011300_7_6`，平台会判"账号与唯一账号不一致"直接拒。
    """
    at = REAL_ACCOUNT["AccountType"] if account_type is None else account_type
    aa = REAL_ACCOUNT["AccAtt"] if acc_att is None else acc_att
    return "%s_%s_%s" % (facct, at, aa)


# 股东号真实格式（实测 136 ST-0 的 12 条 MsgType=18）：
#   沪市  A442523077      = 'A' + 9 位数字，共 10 字符
#   深市  0199908393      = 10 位数字（首位 0）
# 【坑】datahub_test 里写的是 'A' + '%08d'（8 位，共 9 字符），比真实样本短一位。
# 这里按实测修正为 9 位。
SH_BASE_SH = 442000000      # 沪市数字部分基数（9 位）
SH_BASE_SZ = 199000000      # 深市数字部分基数（10 位，首位 0）


def fmt_shareholders(seq):
    """按序号生成一对股东号 (沪, 深)，格式对齐真实样本。

    返回 ([("A442011301", 1), ("0199011301", 2)]) 这样的列表。
    """
    n = int(seq)
    sh = "A%09d" % (SH_BASE_SH + n)
    sz = "%010d" % (SH_BASE_SZ + n)
    return [(sh, 1), (sz, 2)]

REAL_ENTRUST = {
    "ContractCode": "90008169", "ExchangeNum": 2, "EntrustPrice": "0.1033",
    "MarketOrderType": 15, "CoveredType": False, "BSType": 1, "OCType": 1,
    "PriceUnit": "0.0001", "EntrustAmount": 20, "FOK": False,
}
REAL_CFG_EXCEED = {
    "ExchangeNum": 2, "StockCode": "90008169", "StockName": "50ETF",
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
REAL_COND_PRICE = {"ContractCode": "90008169", "ExchangeNum": 2,
                   "Op": ">", "TriggerPrice": "0.123"}
REAL_COND_PERCENT = {"ContractCode": "90008169", "ExchangeNum": 2,
                     "Op": ">", "TriggerPercent": "5.25"}
REAL_COND_TIME = {"ContractCode": "90008169", "ExchangeNum": 2,
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

# 嵌套块定义：prefix -> 真实样本（生成表头与模板行时共用）
REAL_BLOCKS = [
    ("Entrust", REAL_ENTRUST),
    ("CfgExceedPrice", REAL_CFG_EXCEED),
    ("CfgFixedSplit", REAL_CFG_FIXED_SPLIT),
    ("CfgRandSplit", REAL_CFG_RAND_SPLIT),
    ("CfgAppend", REAL_CFG_APPEND),
    ("CondPrice", REAL_COND_PRICE),
    ("CondPercent", REAL_COND_PERCENT),
    ("CondTime", REAL_COND_TIME),
    ("CondLoss", REAL_COND_LOSS),
    ("CondTargetLoss", REAL_COND_TARGET_LOSS),
    ("CondProfit", REAL_COND_PROFIT),
    ("CondTargetProfit", REAL_COND_TARGET_PROFIT),
]

# 中文表头（列名 -> 说明）
ZH = {
    "case_no": "用例编号", "case_type": "用例类型(normal/error/destroy)",
    "case_desc": "用例说明", "expected": "预期结果(备注)",
    "task_raw": "原始task(非空则整条直用,测非法JSON)",
    "MsgType": "MsgType(默认按接口)",
    "Model": "云单运行模式(0内嵌/1独立)", "AccountType": "账号类型(1资金/7客户号)",
    "AccAtt": "渠道(0股票/6期权)", "FAccount": "云单账号",
    "CondType": "条件类型", "CondName": "条件名", "CondDesc": "条件描述",
    "ValidDate": "有效日期", "UniqueAccount": "唯一账号", "Ref": "条件单号",
    "Validity": "有效期类型", "Refs": "条件单号列表(逗号分隔)",
    "RunCount": "运行次数", "Mode": "模式",
    "CreateDateTime": "创建时间", "RunDateTime": "运行时间",
    "CloseDateTime": "关闭时间", "BeginDateTime": "开始时间",
    "EndDateTime": "结束时间", "Status": "状态", "Removed": "是否删除",
    "ModifyDateTime": "修改时间", "TriggerDateTime": "触发时间",
    "Reason": "原因",
    "Pwd": "密码", "ClientName": "客户名称", "TradeAccount": "交易账号",
    "LoginIP": "登录IP", "LoginMAC": "登录MAC", "LoginOS": "登录OS",
    "BranchNo": "营业部号", "BranchNO": "营业部号",
    "SH_SAccount1": "股东号1", "SH_ExchangeNum1": "市场1(1沪/2深)",
    "SH_SAccount2": "股东号2", "SH_ExchangeNum2": "市场2(1沪/2深)",
}
