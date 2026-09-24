# -*- coding: utf-8 -*-
"""
protocol.py —— 「数据中台 → 策略平台」协议常量与报文构造
========================================================
本文件里的每一条常量都不是抄文档来的，而是在 192.168.1.136 上
跑真的 libdatahub_strategy_plug.so + Redis MONITOR 抓包实测确认的。

==================== 实测确认的完整时序 ====================

【1】策略平台上线（策略平台 → 数据中台）
    策略插件 CreateMQ 后每 2 秒重发一次，直到拿到编号为止：
        PUBLISH strategyserver_online
        {"id":-1,"unique_string":"ST-761-2cea7fd9d5c0test",
         "ip":"192.168.1.136","mac":"2cea7fd9d5c0test","usecount":1}
    * 频道名就是 strategyserver_online，【没有】 `_<REDISSELECT>` 后缀。
      实测把 DataHub.ini 的 REDISSELECT 从 0 改成 1，频道名不变。
      （但 136 上确实存在 strategyserver_online_1 这种频道，
        是另一套/另一个版本的进程在用，属于旁路，不用管。）
    * 同时插件 SUBSCRIBE 自己的 unique_string：SUBSCRIBE ST-761-2cea7fd9d5c0test

【2】数据中台分配编号（数据中台 → 策略平台）
    往 unique_string 频道发：
        PUBLISH ST-761-2cea7fd9d5c0test {"id":7,"dataHubString":"test"}
    * 插件收到后回调 id=1，data1='7'（编号），data2=''。
    * 紧接着插件会再 PUBLISH 一次 strategyserver_online，这次 id=7
      —— 相当于"上线确认"，数据中台据此把该策略平台登记为在线。

【3】建流（策略平台自己做）
    插件随即执行：
        XGROUP CREATE ST-7      user_group 0 MKSTREAM
        XGROUP CREATE ST-7-reply user_group 0 MKSTREAM

【4】数据中台下发业务报文（数据中台 → 策略平台）
        XADD ST-7 * request_id <rid> task <json>
    * 流名 = ST-<分配到的编号>。注意：不是 unique_string，是纯数字编号。

【5】策略平台收报文
        XREADGROUP GROUP user_group ST-7 BLOCK 100 COUNT 1 STREAMS ST-7 ST-7-reply > >
    * consumer 名字 = 流名本身（ST-7）。
    * 【同一个 group 同时读两条流】：ST-7（收）和 ST-7-reply（发？）。
      实测 ST-7-reply 一直为空，插件往它上面没有 XADD 任何东西。
      回包走的是 DataHub_reply_stream，不是 ST-7-reply。

【6】策略平台回包（策略平台 → 数据中台）
        XADD DataHub_reply_stream * request_id <rid> task {"status":"OK"}
        XACK ST-7 user_group <刚消费掉的条目ID>
    * 回包的 request_id 原样带回请求的 request_id。
    * 回包内容和插件回调里 SendMQ 传的 data 一致（测试客户端传的是 {"status":"OK"}）。

【7】心跳（策略平台 → 数据中台），每 5 秒
        PUBLISH strategyserver_beat
        {"id":7,"unique_string":"ST-761-2cea7fd9d5c0test",
         "ip":"...","mac":"...","usecount":1}

【8】数据中台 → 策略平台 的心跳
        PUBLISH <unique_string> {"id":1,"dataHubString":"test1"}
    实测确实有这个包在跑（每 10 秒一次），收在 unique_string 频道上。

【9】下线（策略平台 → 数据中台）
        PUBLISH strategyserver_offline {"id":7}

==================== 与文档的差异（重要） ====================
文档写的是：
    PUBLISH strategyserver_online '{"unique_string":"ST-789-AABBCCDDEEJJ",...}'
    PUBLISH ST-789-AABBCCDDEEJJ '{"id":2,"dataHubString":"test"}'
    流名 ST-<index>
"编号 1 → ST-1" 这句，实测的含义是：
    unique_string 里的 "ST-<随机数>" 是【上线会话名】，不是编号；
    真正决定流名的是【数据中台分配的编号 id】。
    即 XADD 的目标流是 ST-<id>，和 unique_string 无关。
数据中台分配编号时若想指定流名，就分配哪个 id，插件就读 ST-<id>。
本工具据此把"分配编号"做成了显式参数（--assign-id），
这样想模拟"编号 1 → ST-1"只要 --assign-id 1 即可。

==================== MsgType 一览 ====================
实测从真 DataHub 落在 ST-0 上的报文里出现过的（136 db0，37567 条全量扫描）：
    顶层键形状只有两种：
      ("MsgType","create")  37426 条   <- 注意 task 里 create 在前、MsgType 在后
      ("Account","MsgType")   141 条   <- MsgType=18
    MsgType:
        4  = create   插入条件单（95%+ 是 CondType=2/5 的批量拆单）
        18 = Account  用户信息（实测 Account.Pwd 是 base64 密文）
    文档另列（这批数据里没出现，但协议有）：
        8  = remove    删除条件单
        11 = modify    修改条件单
        17 = pwdUpdate 用户密码变更
    * 真 DataHub 发出来的 task 是 {"create":{...},"MsgType":4}，
      文档写的是 {"MsgType":4,"create":{...}}——键顺序不同，JSON 语义一样。
      本工具按"两种都发得出来"处理（见 cases.py 的 MSGTYPE_FIRST）。

==================== 回包到底走哪个流（重要，实测） ====================
用 MONITOR 抓真插件的行为，结论：
    SendMQ(...)  ->  XADD DataHub_reply_stream * request_id <rid> task <data>
                    XACK ST-<id> user_group <条目ID>
    即：【策略平台主动回给数据中台的包，写的是 DataHub_reply_stream】。
    插件还会 XGROUP CREATE ST-<id>-reply，但全程没有往它 XADD 过。

但历史上 ST-0-reply 里确实有 4553 条策略平台形态的回包：
    request_id = ST_192.168.1.136_<epoch>_<n>
    task       = {"Err":0,"Msg":"insert trade success"}
               / {"Err":0,"Msg":"update order success","Results":[{"OrderNo":"20184"}]}
且它的消费组是 user_group、消费者名是 ST-0（= 策略平台在读它）。
=> ST-<id>-reply 更像是【数据中台写给策略平台】的应答流（策略平台读它），
   而不是策略平台写给数据中台的。

因此本工具把回包目标做成可切换（--reply-stream）：
    auto   ：默认，按实测走 DataHub_reply_stream（= 真插件行为）
    reply  ：写 ST-<id>-reply（若确认是策略平台写的，用这个）
    both   ：两边都写（联调期最保险）
真数据中台在跑时优先用 auto，行为和真插件一致，不会污染。

==================== 与「数据中台to策略平台.txt」逐条对照 ====================
（结论：文档与实测【一致】。下表列出对照结果与几处细节补充。）

文档条目                        | 实测结果
--------------------------------|--------------------------------------------
1 tradeserver_online            | 一致。实测报文 {"id":-1,"unique_string":"WT-...",...}
  {"unique_string":"WT-789-..."}| （文档示例写 id:1，实测插件首发是 id=-1，
  中台回 PUBLISH WT-.. {id:2}   |   分配后才变正数；语义相同）
2 tradeserver_beat              | 一致（未单独深挖，属委托服务器方向）
3 tradeserver_offline {"id":1}  | 一致（同上；本方向未复现）
4 strategyserver_online         | ★ 一致。实测：
  {"unique_string":"ST-789-..", | PUBLISH strategyserver_online
   "id":1,...,"usecount":3}      | {"id":-1,"unique_string":"ST-761-<mac><name>",
                                |  "ip":"..","mac":"..","usecount":1}
  中台回 PUBLISH ST-789-..      | ★ 一致：PUBLISH <unique_string>
  {id:2,dataHubString:test}     |   {"id":41,"dataHubString":"test"}
                                |   插件回调 id=1 data1='41'（拿到编号）
5 strategyserver_beat           | ★ 一致。实测拿到编号后每 5s 发：
  {"unique_string":..,"id":1,   | PUBLISH strategyserver_beat
   ...,"usecount":3}            | {"id":41,"unique_string":"ST-761-..",
                                |  "ip":..,"mac":..,"usecount":1}
                                | 文档说的"心跳包会发到这个 CHANNEL"确认无误
6 strategyserver_offline        | 文档有，但实测【抓不到】：
  {"id":1}                      | * DestroyMQ 不返回（线程挂住），期间无 offline
                                | * SIGTERM 杀进程也不发
                                | * 策略 .so 里确实有 "publish offline_data error," 这个
                                |   错误串 → 说明代码里有 offline 分支，只是没被触发
                                |   （对照：委托 .so 的字符串表里能看到
                                |    _ZN10cplug_impl7offlineE / createOfflineJsonStr，
                                |    策略 .so 的字符串表里没有这两个）
                                | 结论：策略插件这一版【offline 逻辑存在但未验证能发出】，
                                |   文档描述的是设计意图。本工具仍会照发，
                                |   以便将来中台实现了能对上。
7 datahub_online                | 现场存在该频道（136 本地 Redis 有订阅者）
8 datahub_beat                  | 一致：PUBLISH datahub_beat_1 {"level":2,"role":1}
9 中台→在线服务器 心跳          | ★ 一致：PUBLISH <unique_string>
  {"id":1,"dataHubString":"test1"} {"id":1,"dataHubString":"test1"}
                                | 实测约每 10s 一次

★ 同事的两点补充，实测均确认：
  1) "监听 strategyserver_online，取里面的 unique_string 作为回编号的 CHANNEL"
     —— 完全正确，这就是本工具 mock_datahub 的核心逻辑。
  2) "之后心跳包会发到 strategyserver_beat" —— 正确，见第 5 条。

【唯一需要留意的一处】频道名有两套并存：
  不带后缀 strategyserver_online    ← 插件(.so)发的是这套
  带 _1    strategyserver_online_1  ← 现场真数据中台订阅的是这套
  实测 PUBSUB NUMSUB（136 本地 Redis）：
      strategyserver_online     = 0 个订阅者
      strategyserver_online_1   = 1 个订阅者   ← 真中台在这边（CLIENT LIST 显示
                                                192.168.1.137 连 db1、sub=11，
                                                正是这一整套 _1 频道）
      tradeserver_online/_1、beat、offline、datahub_* 全部同规律

  补充实测：该后缀【与 DataHub.ini 的 REDISSELECT 无关】——
  REDISSELECT 依次设成 0/1/2，插件都仍连 db0、都仍发不带后缀的名字（MONITOR 确认）。

  另外：我分别往 strategyserver_online 和 strategyserver_online_1 发过上线报文
  （格式与真插件完全一致），【两边都没收到数据中台的分配编号回复】。
  所以目前"没应答"不能只归因于后缀；更可能就是策略平台方向尚未开放。
  真中台在 137 上仍在正常发 datahub_beat_1，说明它本身是活的。

  用 --channel-suffix _1 可切到带后缀的一套。
"""

# ===================== 频道 =====================
CH_STRATEGY_ONLINE = "strategyserver_online"    # 策略平台上线（策略→中台）
CH_STRATEGY_BEAT = "strategyserver_beat"        # 策略平台心跳（策略→中台）
CH_STRATEGY_OFFLINE = "strategyserver_offline"  # 策略平台下线（策略→中台）

CH_TRADE_ONLINE = "tradeserver_online"          # 委托服务器上线（委托→中台）
CH_TRADE_BEAT = "tradeserver_beat"
CH_TRADE_OFFLINE = "tradeserver_offline"

CH_DATAHUB_ONLINE = "datahub_online"            # 数据中台上线
CH_DATAHUB_BEAT = "datahub_beat"
CH_DATAHUB_OFFLINE = "datahub_offline"

CH_SUFFIX_1 = "_1"     # 现场另一套环境用的后缀（见下方说明）


def chan(base, suffix=""):
    """频道名 + 可选后缀。"""
    return base + (suffix or "")


# --- 兼容旧名（= 带 _1 后缀的那一套）---
CH_STRATEGY_ONLINE_1 = CH_STRATEGY_ONLINE + CH_SUFFIX_1
CH_STRATEGY_BEAT_1 = CH_STRATEGY_BEAT + CH_SUFFIX_1
CH_STRATEGY_OFFLINE_1 = CH_STRATEGY_OFFLINE + CH_SUFFIX_1


# ===================== 流 =====================
STREAM_REQ = "DataHub_req_stream"        # 委托服务器 → 数据中台 的请求流
STREAM_REPLY = "DataHub_reply_stream"    # 策略平台 → 数据中台 的回包流（MONITOR 实测）
GROUP = "user_group"                     # 消费组名（实测固定为 user_group）

# 回包目标模式
REPLY_AUTO = "auto"    # DataHub_reply_stream（真插件实测行为，默认）
REPLY_ST = "reply"     # ST-<id>-reply
REPLY_BOTH = "both"    # 两边都写

UNIQUE_PREFIX = "ST-"                    # 策略平台 unique_string 前缀


def stream_for(server_id):
    """数据中台 → 策略平台：下发流名 = ST-<分配到的编号>（实测确认）"""
    return "ST-%s" % server_id


def reply_stream_for(server_id):
    """策略平台建的第二个流，实测插件会 XREADGROUP 它但从不写。"""
    return "ST-%s-reply" % server_id


def req_stream_for(server_id):
    """委托服务器侧的流（对照用）。"""
    return "WT-%s" % server_id


def reply_req_stream_for(server_id):
    return "WT-%s-reply" % server_id


def unique_string(rand, mac, name=""):
    """构造 unique_string：ST-<随机数>-<mac><名字>（实测格式）。

    插件自己生成时形如 ST-761-2cea7fd9d5c0test
    （`test` 是 CreateMQ 的 unique_string 参数拼在 mac 后面）。
    """
    return "%s%s-%s%s" % (UNIQUE_PREFIX, rand, mac, name)


# ===================== MsgType =====================
MSG_CREATE = 4      # 插入条件单
MSG_REMOVE = 8      # 删除条件单
MSG_MODIFY = 11     # 修改条件单
MSG_PWD_UPDATE = 17 # 用户密码更换
MSG_ACCOUNT = 18    # 用户信息

MSG_TYPE_NAMES = {
    MSG_CREATE: "create(插入条件单)",
    MSG_REMOVE: "remove(删除条件单)",
    MSG_MODIFY: "modify(修改条件单)",
    MSG_PWD_UPDATE: "pwdUpdate(密码更换)",
    MSG_ACCOUNT: "Account(用户信息)",
}

# MsgType -> 报文里的子对象键（实测：create/modify 用小写子对象，remove 是小写 remove）
MSG_PAYLOAD_KEY = {
    MSG_CREATE: "create",
    MSG_MODIFY: "modify",
    MSG_REMOVE: "remove",
    MSG_PWD_UPDATE: "pwdUpdate",
    MSG_ACCOUNT: "Account",
}

REPLY_OK = '{"status":"OK"}'

# 历史上策略平台真回过的两种形态（见上"回包到底走哪个流"）
REPLY_TRADE = '{"Err":0,"Msg":"insert trade success"}'
REPLY_ORDER = '{"Err":0,"Msg":"update order success","Results":[{"OrderNo":"20184"}]}'
