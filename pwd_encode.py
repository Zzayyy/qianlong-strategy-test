# -*- coding: utf-8 -*-
"""
pwd_encode.py —— Pwd 字段加密（对齐 pwdEncode.cpp）
==================================================
MsgType=18（用户信息）里的 Pwd 不是明文密码，而是【两层 AES-256-CBC + Base64】：

    内层 = encrypt_string(明文密码,   账号)          # 注意：账号不带 _7_6 后缀
    外层 = encrypt_string(内层结果,   当前日期字符串)  # 形如 "20260901"

    Pwd  = 外层

为什么这样：策略平台要拿这个密码去柜台登录。它先用日期解开外层，
再用账号解开内层，才拿到真正的登录密码。

C++ 侧的算法细节（pwdEncode.cpp，逐行对齐）：
  * encrypt_string(data, key) = base64( IV(16字节随机) || AES-256-CBC(pkcs7(data)) )
  * key 拷进 32 字节数组，不足补 0x00（超长截断）—— 即"零填充"
  * IV 随机生成后【前置】在密文最前面
  * pkcs7 填充；解密时校验并去掉

★ 随机 IV ⇒ 同一个密码每次加密结果都不同，这是正常的，不是 bug。
  解密方靠 IV 还原，所以不需要保证可复现。

实测验证（用 136 真实流里抓到的一条 MsgType=18）：
    样本 Pwd = caQ0awvz...LdrKQ==
    日期解外层 -> "s2l8keD5C/xdyejE0ishGy/paDYZe9+BDX8Czi64H88="
    账号解内层 -> "123123"    ← 明文密码，与签署用的一致

用法：
    from pwd_encode import encode_pwd
    pwd = encode_pwd("123123", "010100011300")           # 日期默认取今天
    pwd = encode_pwd("123123", "010100011300", "20260901")  # 指定日期

自测：
    python pwd_encode.py            # 跑内置用例（含对真实样本的解密验证）
    python pwd_encode.py --selftest # 同上
"""
import base64
import datetime
import os
import sys

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _HAVE_CRYPTO = True
except ImportError:
    _HAVE_CRYPTO = False

BLOCK = 16
KEY_LEN = 32


# ==================== 纯 Python AES 兜底（无 cryptography 时用）====================
# 目标机（Linux python3.9）常常没装 cryptography，而本工具强调"零依赖"，
# 所以内置一份纯 Python AES-256-CBC。速度慢，但发报文这点量足够。
_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_RCON = [0x01000000, 0x02000000, 0x04000000, 0x08000000, 0x10000000,
         0x20000000, 0x40000000, 0x80000000, 0x1B000000, 0x36000000,
         0x6C000000, 0xD8000000, 0xAB000000, 0x4D000000, 0x9A000000]


def _gmul(a, b):
    res = 0
    for _ in range(8):
        if b & 1:
            res ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return res


def _key_expand(key):
    w = [0] * 60
    for i in range(8):
        w[i] = ((key[i * 4] << 24) | (key[i * 4 + 1] << 16)
                | (key[i * 4 + 2] << 8) | key[i * 4 + 3])
    for i in range(8, 60):
        t = w[i - 1]
        if i % 8 == 0:
            t = ((t << 8) | ((t >> 24) & 0xFF)) & 0xFFFFFFFF
            t = ((_SBOX[(t >> 24) & 0xFF] << 24) | (_SBOX[(t >> 16) & 0xFF] << 16)
                 | (_SBOX[(t >> 8) & 0xFF] << 8) | _SBOX[t & 0xFF])
            t ^= _RCON[i // 8 - 1]
        elif i % 8 == 4:
            t = ((_SBOX[(t >> 24) & 0xFF] << 24) | (_SBOX[(t >> 16) & 0xFF] << 16)
                 | (_SBOX[(t >> 8) & 0xFF] << 8) | _SBOX[t & 0xFF])
        w[i] = w[i - 8] ^ t
    return w


def _encrypt_block(blk, w):
    """blk: 16 字节 bytearray（就地修改）"""
    for i in range(4):
        rk = w[i]
        blk[i * 4 + 0] ^= (rk >> 24) & 0xFF
        blk[i * 4 + 1] ^= (rk >> 16) & 0xFF
        blk[i * 4 + 2] ^= (rk >> 8) & 0xFF
        blk[i * 4 + 3] ^= rk & 0xFF
    for r in range(1, 14):
        for i in range(16):
            blk[i] = _SBOX[blk[i]]
        # shift rows
        t = blk[1]; blk[1] = blk[5]; blk[5] = blk[9]; blk[9] = blk[13]; blk[13] = t
        t = blk[2]; blk[2] = blk[10]; blk[10] = t
        t = blk[6]; blk[6] = blk[14]; blk[14] = t
        t = blk[15]; blk[15] = blk[11]; blk[11] = blk[7]; blk[7] = blk[3]; blk[3] = t
        # mix columns
        for c in range(4):
            o = c * 4
            a0, a1, a2, a3 = blk[o], blk[o + 1], blk[o + 2], blk[o + 3]
            blk[o] = _gmul(2, a0) ^ _gmul(3, a1) ^ a2 ^ a3
            blk[o + 1] = a0 ^ _gmul(2, a1) ^ _gmul(3, a2) ^ a3
            blk[o + 2] = a0 ^ a1 ^ _gmul(2, a2) ^ _gmul(3, a3)
            blk[o + 3] = _gmul(3, a0) ^ a1 ^ a2 ^ _gmul(2, a3)
        for i in range(4):
            rk = w[r * 4 + i]
            blk[i * 4 + 0] ^= (rk >> 24) & 0xFF
            blk[i * 4 + 1] ^= (rk >> 16) & 0xFF
            blk[i * 4 + 2] ^= (rk >> 8) & 0xFF
            blk[i * 4 + 3] ^= rk & 0xFF
    for i in range(16):
        blk[i] = _SBOX[blk[i]]
    t = blk[1]; blk[1] = blk[5]; blk[5] = blk[9]; blk[9] = blk[13]; blk[13] = t
    t = blk[2]; blk[2] = blk[10]; blk[10] = t
    t = blk[6]; blk[6] = blk[14]; blk[14] = t
    t = blk[15]; blk[15] = blk[11]; blk[11] = blk[7]; blk[7] = blk[3]; blk[3] = t
    for i in range(4):
        rk = w[14 * 4 + i]
        blk[i * 4 + 0] ^= (rk >> 24) & 0xFF
        blk[i * 4 + 1] ^= (rk >> 16) & 0xFF
        blk[i * 4 + 2] ^= (rk >> 8) & 0xFF
        blk[i * 4 + 3] ^= rk & 0xFF
    return blk


def _decrypt_block(blk, w):
    """严格对齐 C++ pwdEncode::aes_decrypt_block 的轮顺序：

        add_round_key(14)
        inv_shift_rows; inv_sub_bytes
        for r in 13..1:  add_round_key(r); inv_mix_columns; inv_shift_rows; inv_sub_bytes
        add_round_key(0)

    ★ 顺序不能改：C++ 里 add_round_key 在 inv_mix_columns 之前，
      早期版本我写成 inv_shift_rows 开头，导致解密出乱码（加密不受影响）。
    """
    def _ark(r):
        for i in range(4):
            rk = w[r * 4 + i]
            blk[i * 4 + 0] ^= (rk >> 24) & 0xFF
            blk[i * 4 + 1] ^= (rk >> 16) & 0xFF
            blk[i * 4 + 2] ^= (rk >> 8) & 0xFF
            blk[i * 4 + 3] ^= rk & 0xFF

    def _inv_shift():
        t = blk[13]; blk[13] = blk[9]; blk[9] = blk[5]; blk[5] = blk[1]; blk[1] = t
        t = blk[2]; blk[2] = blk[10]; blk[10] = t
        t = blk[6]; blk[6] = blk[14]; blk[14] = t
        t = blk[3]; blk[3] = blk[7]; blk[7] = blk[11]; blk[11] = blk[15]; blk[15] = t

    def _inv_sub():
        for i in range(16):
            blk[i] = _INV_SBOX[blk[i]]

    def _inv_mix():
        for c in range(4):
            o = c * 4
            a0, a1, a2, a3 = blk[o], blk[o + 1], blk[o + 2], blk[o + 3]
            blk[o] = _gmul(14, a0) ^ _gmul(11, a1) ^ _gmul(13, a2) ^ _gmul(9, a3)
            blk[o + 1] = _gmul(9, a0) ^ _gmul(14, a1) ^ _gmul(11, a2) ^ _gmul(13, a3)
            blk[o + 2] = _gmul(13, a0) ^ _gmul(9, a1) ^ _gmul(14, a2) ^ _gmul(11, a3)
            blk[o + 3] = _gmul(11, a0) ^ _gmul(13, a1) ^ _gmul(9, a2) ^ _gmul(14, a3)

    _ark(14)
    _inv_shift()
    _inv_sub()
    for r in range(13, 0, -1):
        _ark(r)
        _inv_mix()
        _inv_shift()
        _inv_sub()
    _ark(0)
    return blk


def _key32(key):
    b = key.encode("utf-8") if isinstance(key, str) else key
    return (b + b"\x00" * KEY_LEN)[:KEY_LEN]


def _pkcs7(data):
    pad = BLOCK - (len(data) % BLOCK)
    return data + bytes([pad]) * pad


def _unpad(data):
    if not data:
        return data
    pad = data[-1]
    if pad == 0 or pad > BLOCK or data[-pad:] != bytes([pad]) * pad:
        return data
    return data[:-pad]


def _aes_cbc_encrypt(plain: bytes, key: str, iv: bytes) -> bytes:
    w = _key_expand(list(_key32(key)))
    out = bytearray()
    prev = iv
    for off in range(0, len(plain), BLOCK):
        blk = bytearray(a ^ b for a, b in zip(plain[off:off + BLOCK], prev))
        _encrypt_block(blk, w)
        out += blk
        prev = bytes(blk)
    return bytes(out)


def _aes_cbc_decrypt(ct: bytes, key: str, iv: bytes) -> bytes:
    w = _key_expand(list(_key32(key)))
    out = bytearray()
    prev = iv
    for off in range(0, len(ct), BLOCK):
        blk = bytearray(ct[off:off + BLOCK])
        _decrypt_block(blk, w)
        out += bytes(a ^ b for a, b in zip(blk, prev))
        prev = ct[off:off + BLOCK]
    return bytes(out)


# ==================== 对外接口 ====================
def encrypt_string(data: str, key: str, iv: bytes = None) -> str:
    """对齐 C++ pwdEncode::encrypt_string：base64(IV || AES-256-CBC(pkcs7(data)))"""
    raw = data.encode("utf-8")
    if iv is None:
        iv = os.urandom(BLOCK)
    if _HAVE_CRYPTO:
        c = Cipher(algorithms.AES(_key32(key)), modes.CBC(iv))
        enc = c.encryptor()
        ct = enc.update(_pkcs7(raw)) + enc.finalize()
    else:
        ct = _aes_cbc_encrypt(_pkcs7(raw), key, iv)
    return base64.b64encode(iv + ct).decode("ascii")


def decrypt_string(b64: str, key: str) -> str:
    """解密（自测/排查用）。失败抛 ValueError。"""
    raw = base64.b64decode(b64)
    if len(raw) <= BLOCK or len(raw) % BLOCK != 0:
        raise ValueError("密文长度异常: %d" % len(raw))
    iv, ct = raw[:BLOCK], raw[BLOCK:]
    if _HAVE_CRYPTO:
        c = Cipher(algorithms.AES(_key32(key)), modes.CBC(iv))
        dec = c.decryptor()
        pt = dec.update(ct) + dec.finalize()
    else:
        pt = _aes_cbc_decrypt(ct, key, iv)
    return _unpad(pt).decode("utf-8")


def today_str(fmt="%Y%m%d"):
    return datetime.datetime.now().strftime(fmt)


def encode_pwd(password: str, account: str, date_str: str = None) -> str:
    """生成 MsgType=18 的 Pwd 字段。

    password : 明文登录密码
    account  : 云单账号，**不带 _类型_渠道 后缀**（如 "010100011300"）
    date_str : 外层密钥日期字符串，默认取当天 YYYYMMDD

    注意：随机 IV ⇒ 每次结果不同，属正常。
    """
    if not _HAVE_CRYPTO:
        pass  # 走纯 Python 兜底，功能一致
    if account is None or str(account).strip() == "":
        raise ValueError("account 为空：内层密钥必须是账号本身（不带 _7_6）")
    acct = str(account).strip()
    if "_" in acct:
        # 常见误用：传了 UniqueAccount（010100011300_7_6）。
        # 内层密钥必须是纯账号，这里自动纠正并提示，避免加密出一串没用的东西。
        fixed = acct.split("_")[0]
        sys.stderr.write("[WARN] account=%r 含下划线，内层密钥应为纯账号，"
                         "已自动改用 %r\n" % (acct, fixed))
        acct = fixed
    d = (date_str or today_str()).strip()
    inner = encrypt_string(password, acct)
    return encrypt_string(inner, d)


def decode_pwd(pwd: str, account: str, date_str: str = None) -> str:
    """反向解出明文（排查用）。"""
    d = (date_str or today_str()).strip()
    inner = decrypt_string(pwd, d)
    return decrypt_string(inner, str(account).split("_")[0])


# ==================== 自测 ====================
def _selftest():
    ok = True
    print("依赖: %s" % ("cryptography" if _HAVE_CRYPTO else "纯 Python AES（兜底）"))
    print("=" * 74)

    # 1) 对真实样本解密（这是最强证据：真实流里抓到的密文）
    sample = ("caQ0awvz26RmuxLzhAWJVkkfyqrX3tQWs2rGT6xSmxnYi+0DttbRAjU1UO0W2DBl"
              "WMKMo8Cc8fJs6dAHgLdrKQ==")
    print("1) 用真实样本验证算法")
    print("   密文 = %s..." % sample[:40])
    try:
        inner = decrypt_string(sample, "20260910")
        print("   日期(20260910) 解外层 -> %r" % inner)
        plain = decrypt_string(inner, "010100011300")
        print("   账号(010100011300) 解内层 -> %r" % plain)
        good = (plain == "123123")
        print("   结果: %s" % ("✓ 与真实签署密码一致" if good else "✗ 不符"))
        ok = ok and good
    except Exception as e:
        print("   ✗ 解密失败: %s" % e)
        ok = False

    # 2) 往返：encode -> decode 应还原
    print("\n2) 往返自测（encode → decode）")
    for pwd, acct in (("123123", "010100011300"),
                      ("123456", "010100011301"),
                      ("aB!@#$%^&*()", "999999"),
                      ("", "010100011300")):
        enc = encode_pwd(pwd, acct, "20260901")
        dec = decode_pwd(enc, acct, "20260901")
        good = (dec == pwd)
        ok = ok and good
        print("   密码=%-12r 账号=%-14s -> %s  %s"
              % (pwd, acct, enc[:40] + "...", "✓" if good else "✗ got %r" % dec))

    # 3) 随机 IV ⇒ 两次结果应不同，但都能解开
    print("\n3) 随机 IV 检查（两次加密不同、都能解回）")
    e1 = encode_pwd("123123", "010100011300", "20260901")
    e2 = encode_pwd("123123", "010100011300", "20260901")
    good = (e1 != e2) and decode_pwd(e1, "010100011300", "20260901") == "123123" \
        and decode_pwd(e2, "010100011300", "20260901") == "123123"
    ok = ok and good
    print("   e1 != e2: %s   两者都能解开: %s  %s"
          % (e1 != e2, decode_pwd(e1, "010100011300", "20260901") == "123123",
             "✓" if good else "✗"))

    # 4) 误传 UniqueAccount 时自动纠正
    print("\n4) 误传 UniqueAccount（带 _7_6）应自动纠正并仍可解")
    enc = encode_pwd("123123", "010100011300_7_6", "20260901")
    good = decode_pwd(enc, "010100011300", "20260901") == "123123"
    ok = ok and good
    print("   %s" % ("✓" if good else "✗"))

    print("\n" + "=" * 74)
    print("自测结果: %s" % ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest())
