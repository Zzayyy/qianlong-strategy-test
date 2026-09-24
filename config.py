# -*- coding: utf-8 -*-
"""
config.py —— 共享配置（config.ini 读写）
=======================================
所有工具都从这里取 Redis 地址 / 策略平台身份参数，命令行参数优先级更高。
"""
import configparser
import os

HERE = os.path.dirname(os.path.abspath(__file__))
INI_PATH = os.path.join(HERE, "config.ini")

DEFAULTS = {
    "redis": {
        "host": "192.168.1.137",
        "port": "6379",
        "pwd": "QianLong@2026&",
        "db": "0",
    },
    "strategy": {
        # 数据中台分配给策略平台的编号；决定下发流名 ST-<id>
        "assign_id": "1",
        # unique_string = ST-<rand>-<mac><name>，插件自己也是这么拼的
        "unique_rand": "761",
        "unique_name": "test",
        "mac": "2cea7fd9d5c0",
        "ip": "192.168.1.136",
        "usecount": "1",
        # 收到业务报文后回给数据中台的内容
        "reply_data": '{"status":"OK"}',
        # 上线重发间隔（秒）—— 插件实测是 2 秒一次，直到拿到编号
        "online_interval": "2.0",
        # 心跳间隔（秒）—— 插件实测约 5 秒
        "beat_interval": "5.0",
    },
    "test": {
        "workers": "4",
        "max": "0",
        "rate": "0",
        "wait": "5.0",
        "interface": "create",
        "type": "normal",
    },
}


def load(path=INI_PATH):
    """读配置；缺的段落/键用 DEFAULTS 补上（不写盘）。"""
    cp = configparser.ConfigParser()
    if os.path.exists(path):
        cp.read(path, encoding="utf-8")
    for sec, kv in DEFAULTS.items():
        if not cp.has_section(sec):
            cp.add_section(sec)
        for k, v in kv.items():
            if not cp.has_option(sec, k):
                cp.set(sec, k, v)
    return cp


def save(cp, path=INI_PATH):
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f)


def get(cp, section, key, cast=None):
    v = cp.get(section, key, fallback=DEFAULTS.get(section, {}).get(key, ""))
    if cast is None or v is None:
        return v
    try:
        return cast(v)
    except (ValueError, TypeError):
        return v


def redis_kwargs(cp, args=None):
    """组装 RespClient 参数；命令行 args（若给了对应属性）优先。"""
    def pick(attr, sec, key):
        if args is not None:
            v = getattr(args, attr, None)
            if v is not None and v != "":
                return v
        return get(cp, sec, key)

    return {
        "host": pick("host", "redis", "host"),
        "port": int(pick("port", "redis", "port")),
        "password": pick("pwd", "redis", "pwd"),
        "db": int(pick("db", "redis", "db")),
    }


def describe(kw):
    return "%s:%s db%s" % (kw["host"], kw["port"], kw["db"])
