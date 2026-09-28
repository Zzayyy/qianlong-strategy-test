# -*- coding: utf-8 -*-
"""
safety.py —— 共享安全闸：避免误伤真实策略平台
============================================
背景（2026-09-24 实测）：
    真实策略平台连的是 192.168.1.136:6379 db0，同事在测别的；
    计划之后【切换到 192.168.1.137】。而本工具的默认目标恰好就是 137 ——
    也就是说，切换之后，"直接跑默认命令"会从"安全"变成"打到真平台"。

两类伤害，都很隐蔽：
  1) 往 ST-<id> 里 XADD  → 给真实策略平台下发假条件单（可能触发真实交易）
  2) 加入 ST-<id> 的 user_group 用 XREADGROUP '>' 读
     → Redis Streams 同组是【负载均衡不是广播】，会把本该真平台处理的消息抢走！
     表现不是报错，而是真平台的单子"莫名少了一半"，极难排查。

判定"谁是外来消费者"：
    本工具的 mock 用 consumer 名 = ST-<id>-w0 / -w1 / ...（带 -wN 后缀）
    真策略平台 / 真插件用 consumer 名 = ST-<id>（就是流名本身）
    实测确认：136 db0 的 ST-0 上，consumer 名就是 "ST-0"。

用法：
    from safety import guard_stream
    ok = guard_stream(conn, "ST-0", force_live=args.force_live, tool="send_test")
    if not ok:
        return 2
"""
import re
import sys

GROUP = "user_group"


def stream_for(server_id):
    return "ST-%s" % server_id


def _base_stream(stream):
    """ST-0-reply -> ST-0（-reply 流上，mock 的 consumer 名绑定的是 req 流名）"""
    return stream[:-len("-reply")] if stream.endswith("-reply") else stream


def _mock_consumer_pattern(stream):
    """本工具 mock 的 consumer 命名：<req 流名>-w<数字>

    【坑】mock_strategy 用 consumer = ST-<id>-wN，同时 XREADGROUP 读
    ST-<id> 和 ST-<id>-reply 两条流。所以在 ST-<id>-reply 上，
    消费者名字也是 ST-<id>-wN，而不是 ST-<id>-reply-wN。
    若按当前流名拼正则，在 -reply 流上会把自家 mock 误判成"外来消费者"。
    """
    return re.compile(r"^%s-w\d+$" % re.escape(_base_stream(stream)))


def foreign_consumers(conn, stream):
    """返回该流上「不属于本工具」的消费者描述列表。

    只读操作（XINFO GROUPS / XINFO CONSUMERS）。
    """
    out = []
    if not conn.cmd("EXISTS", stream):
        return out
    try:
        groups = conn.xinfo_groups(stream)
    except Exception:
        return out
    pat = _mock_consumer_pattern(stream)
    for g in groups:
        gname = g.get("name") or GROUP
        try:
            for cc in conn.xinfo_consumers(stream, gname):
                nm = str(cc.get("name", ""))
                if not pat.match(nm):
                    out.append({"group": gname, "name": nm,
                                "idle": cc.get("idle"),
                                "pending": cc.get("pending")})
        except Exception:
            pass
    return out


def describe(foreign):
    return "\n".join("    %s/%s (idle=%sms pending=%s)"
                     % (f["group"], f["name"], f["idle"], f["pending"])
                     for f in foreign)


def guard_stream(conn, stream, force_live=False, tool="", what="发送"):
    """检查流是否安全。安全返回 True；发现外来消费者返回 False 并打印警告。

    tool   : 工具名，用于提示里说清是谁拦的
    what   : "发送" 或 "消费"，决定措辞（消费更危险，会抢消息）
    """
    foreign = foreign_consumers(conn, stream)
    if not foreign:
        return True
    if force_live:
        sys.stderr.write(
            "\n[%s] ⚠ 已用 --force-live 忽略安全闸：%s 上有外来消费者 %s\n"
            % (tool, stream, [f["name"] for f in foreign]))
        return True

    if what == "消费":
        harm = ("加入它的消费组 = 把本该真实策略平台处理的消息【抢走一部分】。\n"
                "Redis Streams 同组是负载均衡不是广播 —— 真平台的单子会莫名少掉，\n"
                "而且不会报任何错，极难排查！")
        tip = "如果你只是想测，请换一个没被占用的编号（如 --assign-id 50）。"
    else:
        harm = "往它写数据 = 给真实策略平台下发假条件单，可能触发真实交易！"
        tip = "如果只是想测，请换一个没被占用的编号（如 --assign-id 50）。"

    sys.stderr.write(
        "\n" + "!" * 74 + "\n"
        "【安全闸 / %s】目标流 %s 上已存在不是本工具创建的消费者：\n"
        "%s\n\n"
        "%s\n\n"
        "真平台与 mock 的区分：\n"
        "    真平台/真插件 consumer 名 = %s（就是流名）\n"
        "    本工具 mock  consumer 名 = %s-w0 / -w1 / ...（带 -wN）\n\n"
        "先跑只读体检确认：  python check_env.py%s\n"
        "确认无害后要强行继续，加：  --force-live\n"
        % (tool, stream, describe(foreign), harm,
           stream, stream,
           ("\n" + tip) if tip else "") + "!" * 74 + "\n")
    return False


def guard_many(conn, streams, force_live=False, tool="", what="消费"):
    """对多条流逐一检查；任一不安全就返回 False（用于 mock 同时读 ST-N/ST-N-reply）。"""
    for s in streams:
        if not guard_stream(conn, s, force_live=force_live, tool=tool, what=what):
            return False
    return True
