# -*- coding: utf-8 -*-
"""
resp_min.py —— 极简 Redis RESP 客户端（纯 socket，不依赖 redis-py）
=================================================================
为什么不用 redis-py：
  目标 Linux 机（如 192.168.1.136 的 python3.9）通常没装 redis 模块，
  而 datahub_test/mock_datahub.py 已经把"纯 socket 够用"验证过了。
  这里把那套实现抽出来，补齐 XADD / XREADGROUP / XINFO / XACK 等命令，
  并加一个"管道批量 XADD"，压测时用来把吞吐拉满。

线程安全：不保证。一个线程用一个 RespClient 实例。

用法：
    c = RespClient("192.168.1.137", 6379, "QianLong@2026&", db=0).connect()
    c.publish("strategyserver_online", '{"id":-1}')
    c.xadd("ST-1", {"request_id": "r1", "task": "{...}"})
    c.close()
"""
import socket
import threading
import time

__all__ = ["RespError", "RespClient", "flat_fields", "now_ms"]


class RespError(Exception):
    """Redis 返回 -ERR 时抛出"""


def now_ms():
    return int(time.time() * 1000)


def flat_fields(fields):
    """把 Redis 返回的扁平数组 [k1,v1,k2,v2,...] 转成 dict。

    XRANGE / XREVRANGE / XREADGROUP 返回的每条记录都是这种扁平数组。
    """
    d = {}
    if isinstance(fields, list):
        for i in range(0, len(fields) - 1, 2):
            d[fields[i]] = fields[i + 1]
    return d


class RespClient(object):
    """一个连接 = 一个 socket。订阅连接上不能跑普通命令，请另开实例。"""

    def __init__(self, host, port=6379, password=None, db=0, timeout=15.0):
        self.host = host
        self.port = int(port)
        self.password = password
        self.db = int(db or 0)
        self.timeout = timeout
        self.sock = None
        self._buf = b""          # 增量读缓冲（listen 用）
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 连接
    def connect(self, timeout=None):
        self.sock = socket.create_connection(
            (self.host, self.port), timeout=timeout or self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.password:
            self._write("AUTH", self.password)
            self._read()
        if self.db:
            self._write("SELECT", str(self.db))
            self._read()
        return self

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def __enter__(self):
        return self.connect() if self.sock is None else self

    def __exit__(self, *exc):
        self.close()

    @property
    def connected(self):
        return self.sock is not None

    def select(self, db):
        self.db = int(db)
        self._write("SELECT", str(self.db))
        return self._read()

    # ------------------------------------------------------------ 编解码
    def _write(self, *args):
        buf = bytearray()
        buf.extend(("*%d\r\n" % len(args)).encode())
        for a in args:
            if isinstance(a, bytes):
                b = a
            elif isinstance(a, str):
                b = a.encode("utf-8")
            else:
                b = str(a).encode("utf-8")
            buf.extend(("$%d\r\n" % len(b)).encode())
            buf.extend(b)
            buf.extend(b"\r\n")
        self.sock.sendall(bytes(buf))

    def _readline(self):
        d = self._buf
        while b"\r\n" not in d:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed by redis")
            d += chunk
        line, self._buf = d.split(b"\r\n", 1)
        return line

    def _read(self):
        line = self._readline()
        t = line[:1]
        if t == b"+":
            return line[1:].decode("utf-8", "replace")
        if t == b"-":
            raise RespError(line[1:].decode("utf-8", "replace"))
        if t == b":":
            return int(line[1:])
        if t == b"$":
            n = int(line[1:])
            if n == -1:
                return None
            d = self._buf
            while len(d) < n + 2:
                chunk = self.sock.recv(max(65536, n + 2 - len(d)))
                if not chunk:
                    raise ConnectionError("connection closed by redis")
                d += chunk
            data, self._buf = d[:n], d[n + 2:]
            return data.decode("utf-8", "replace")
        if t == b"*":
            n = int(line[1:])
            if n == -1:
                return None
            return [self._read() for _ in range(n)]
        if t == b"_":                      # RESP3 null
            return None
        if t == b"#":                      # RESP3 bool
            return line[1:2] == b"t"
        if t == b",":                      # RESP3 double
            return float(line[1:])
        if t in (b"%", b"~", b">"):        # RESP3 map/set/push
            n = int(line[1:])
            if t == b"%":
                out = {}
                for _ in range(n):
                    k = self._read()
                    out[k] = self._read()
                return out
            return [self._read() for _ in range(n)]
        return line.decode("utf-8", "replace")

    def cmd(self, *args):
        """执行任意命令并返回解析后的结果。"""
        self._write(*args)
        return self._read()

    def cmd_ok(self, *args):
        """执行命令，-ERR 也不抛，返回字符串（用于 XGROUP 等幂等命令）。"""
        try:
            return self.cmd(*args)
        except RespError as e:
            return "ERR " + str(e)

    # ------------------------------------------------------------ 管道
    def pipeline(self, arg_list):
        """一次写入多条命令再一次收齐所有回复（压测吞吐关键）。

        arg_list: [(cmd, arg1, arg2, ...), ...]
        返回与 arg_list 等长的结果列表；单条 -ERR 以 RespError 实例占位。
        """
        for a in arg_list:
            self._write(*a)
        out = []
        for _ in arg_list:
            try:
                out.append(self._read())
            except RespError as e:
                out.append(e)
        return out

    # ------------------------------------------------------------ Pub/Sub
    def publish(self, channel, msg):
        return self.cmd("PUBLISH", channel, msg)

    def subscribe(self, channels):
        if isinstance(channels, str):
            channels = [channels]
        self._write("SUBSCRIBE", *channels)
        got = []
        while len(got) < len(channels):
            r = self._read()
            if isinstance(r, list) and r and r[0] == "subscribe":
                got.append(r[1])
        return got

    def psubscribe(self, patterns):
        if isinstance(patterns, str):
            patterns = [patterns]
        self._write("PSUBSCRIBE", *patterns)
        got = []
        while len(got) < len(patterns):
            r = self._read()
            if isinstance(r, list) and r and r[0] == "psubscribe":
                got.append(r[1])
        return got

    def read_message(self, timeout=1.0):
        """读一条订阅消息。返回 (kind, channel, payload) 或 None（超时）。

        kind: "message" / "pmessage" / "subscribe" / "psubscribe" / "pong"
        """
        self.sock.settimeout(timeout)
        try:
            m = self._read()
        except socket.timeout:
            return None
        except (ConnectionError, OSError):
            raise
        if not isinstance(m, list) or not m:
            return None
        kind = m[0]
        if kind == "message" and len(m) >= 3:
            return ("message", m[1], m[2])
        if kind == "pmessage" and len(m) >= 4:
            return ("pmessage", m[2], m[3])
        if kind in ("subscribe", "psubscribe", "unsubscribe", "punsubscribe"):
            return (kind, m[1] if len(m) > 1 else "", None)
        return (kind, "", None)

    def pubsub_channels(self):
        return sorted(self.cmd("PUBSUB", "CHANNELS") or [])

    def pubsub_numpat(self):
        return self.cmd("PUBSUB", "NUMPAT")

    # ------------------------------------------------------------ Stream
    def xadd(self, stream, fields, entry_id="*", maxlen=None, approximate=True):
        """XADD stream [MAXLEN ~ n] * f1 v1 ...  返回条目 ID。"""
        args = ["XADD", stream]
        if maxlen:
            if approximate:
                args += ["MAXLEN", "~", str(int(maxlen))]
            else:
                args += ["MAXLEN", str(int(maxlen))]
        args.append(entry_id)
        for k, v in fields.items():
            args.append(k)
            args.append(v if isinstance(v, (str, bytes)) else str(v))
        return self.cmd(*args)

    def xadd_pipeline(self, items, maxlen=None):
        """批量 XADD（单次写、单次收）。items: [(stream, fields_dict), ...]"""
        arg_list = []
        for stream, fields in items:
            args = ["XADD", stream]
            if maxlen:
                args += ["MAXLEN", "~", str(int(maxlen))]
            args.append("*")
            for k, v in fields.items():
                args.append(k)
                args.append(v if isinstance(v, (str, bytes)) else str(v))
            arg_list.append(tuple(args))
        return self.pipeline(arg_list)

    def xlen(self, stream):
        return self.cmd("XLEN", stream)

    def xtype(self, key):
        return self.cmd("TYPE", key)

    def xrange(self, stream, start="-", end="+", count=None):
        args = ["XRANGE", stream, start, end]
        if count:
            args += ["COUNT", str(int(count))]
        return self._entries(self.cmd(*args))

    def xrevrange(self, stream, count=10):
        return self._entries(self.cmd("XREVRANGE", stream, "+", "-", "COUNT", str(int(count))))

    @staticmethod
    def _entries(raw):
        """[[id,[f,v,...]], ...] -> [(id, {f:v}), ...]"""
        out = []
        for item in (raw or []):
            if isinstance(item, list) and len(item) >= 2:
                out.append((item[0], flat_fields(item[1])))
        return out

    def xdel(self, stream, *ids):
        return self.cmd("XDEL", stream, *ids)

    def delete(self, *keys):
        return self.cmd("UNLINK", *keys)

    def xgroup_create(self, stream, group="user_group", start="0", mkstream=True):
        """建消费者组；已存在（BUSYGROUP）不报错。"""
        args = ["XGROUP", "CREATE", stream, group, start]
        if mkstream:
            args.append("MKSTREAM")
        return self.cmd_ok(*args)

    def xgroup_destroy(self, stream, group="user_group"):
        return self.cmd_ok("XGROUP", "DESTROY", stream, group)

    def xack(self, stream, group, *ids):
        return self.cmd("XACK", stream, group, *ids)

    def xinfo_groups(self, stream):
        """返回 [{name, consumers, pending, last-delivered-id, ...}, ...]"""
        raw = self.cmd_ok("XINFO", "GROUPS", stream)
        if not isinstance(raw, list):
            return []
        out = []
        for g in raw:
            d = flat_fields(g)
            if d:
                out.append(d)
        return out

    def xinfo_consumers(self, stream, group):
        raw = self.cmd_ok("XINFO", "CONSUMERS", stream, group)
        if not isinstance(raw, list):
            return []
        return [flat_fields(c) for c in raw if isinstance(c, list)]

    def reconnect(self):
        """硬重连（重连前丢弃可能残留的半截响应缓冲）。"""
        self.close()
        self._buf = b""
        return self.connect()

    def xreadgroup(self, group, consumer, streams, count=1, block=100, noack=False):
        """XREADGROUP GROUP g c [COUNT n] [BLOCK ms] [NOACK] STREAMS s1 s2 ... > > ...

        返回 [(stream, [(id, {field:value}), ...]), ...]；无数据返回 []。
        streams 可传 str 或 list。

        超时安全：BLOCK 由服务端在 ms 后回 nil，正常不会触发 socket 超时。
        万一触发了（网络抖动），说明缓冲里可能有半截响应，直接重连，
        避免后续解析错位——压测长跑时这点很关键。
        """
        if isinstance(streams, str):
            streams = [streams]
        args = ["XREADGROUP", "GROUP", group, consumer]
        if count:
            args += ["COUNT", str(int(count))]
        if block:
            args += ["BLOCK", str(int(block))]
        if noack:
            args.append("NOACK")
        args.append("STREAMS")
        args += list(streams)
        args += [">"] * len(streams)

        # socket 超时必须比服务端 BLOCK 宽裕，否则会误判超时
        old = self.sock.gettimeout()
        want = (block / 1000.0 + 10.0) if block else None
        if want:
            self.sock.settimeout(want)
        try:
            self._write(*args)
            raw = self._read()
        except socket.timeout:
            self.reconnect()
            return []
        finally:
            if self.sock and old is not None:
                try:
                    self.sock.settimeout(old)
                except Exception:
                    pass

        if not raw:
            return []
        out = []
        for item in raw:
            if not (isinstance(item, list) and len(item) >= 2):
                continue
            entries = []
            for e in (item[1] or []):
                if isinstance(e, list) and len(e) >= 2:
                    entries.append((e[0], flat_fields(e[1])))
            out.append((item[0], entries))
        return out

    # ------------------------------------------------------------ 其它
    def ping(self):
        return self.cmd("PING")

    def dbsize(self):
        return self.cmd("DBSIZE")

    def keys(self, pattern="*"):
        return sorted(self.cmd("KEYS", pattern) or [])

    def get(self, key):
        return self.cmd("GET", key)

    def set(self, key, value):
        return self.cmd("SET", key, value)

    def json_get(self, key, path=None):
        args = ["JSON.GET", key]
        if path:
            args.append(path)
        return self.cmd_ok(*args)

    def info(self, section=None):
        return self.cmd("INFO", section) if section else self.cmd("INFO")
