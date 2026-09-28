# -*- coding: utf-8 -*-
"""
_crosscheck_cpp.py —— 用 136 上的真 pwdEncode.cpp 交叉验证 Python 实现
===================================================================
目的：不满足于"能解开一个历史样本"，而是让 C++ 现场生成密文，
      再由 Python 解密；并用 Python 生成密文，让 C++ 解密。
      双向都对，才算真正对齐。

在 136 的 /tmp 下编译（不碰 /home/yangsh/so_test 里同事的东西）。
"""
import os
import sys

import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOST, PORT, USER, PWD = "192.168.1.136", 22, "yangsh", "qianlong@135246"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = ROOT   # 本脚本在 tests/ 下，项目根是上一层
TMP = "/tmp/pwdcheck_yangsh"

MAIN = r'''
#include "pwdEncode.h"
#include <cstdio>
#include <cstring>
#include <cstdlib>

// 用法:
//   ./pwdcheck gen  <pwd> <acct> <date>   -> 打印两层加密结果
//   ./pwdcheck dec  <b64> <key>           -> 解密并打印（用于验证 Python 生成的密文）
int main(int argc, char** argv) {
    pwdEncode pe;
    if (argc >= 5 && strcmp(argv[1], "gen") == 0) {
        char* inner = pe.encrypt_string(argv[2], argv[3]);
        char* outer = pe.encrypt_string(inner, argv[4]);
        printf("%s\n%s\n", inner, outer);
        free(inner); free(outer);
        return 0;
    }
    if (argc >= 4 && strcmp(argv[1], "dec") == 0) {
        char* p = pe.decrypt_string(argv[2], argv[3]);
        if (!p) { printf("<NULL>\n"); return 1; }
        printf("%s\n", p);
        free(p);
        return 0;
    }
    fprintf(stderr, "usage: pwdcheck gen|dec ...\n");
    return 2;
}
'''


def sh(cli, cmd, t=120):
    _, o, e = cli.exec_command(cmd, timeout=t)
    return (o.read().decode("utf-8", "replace"),
            e.read().decode("utf-8", "replace"))


def main():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, port=PORT, username=USER, password=PWD, timeout=20)
    print("[OK] 已连上 %s" % HOST)

    o, _ = sh(cli, "which g++ gcc 2>&1; g++ --version 2>&1 | head -1")
    print("[编译器] %s" % o.strip())
    if "g++" not in o and "gcc" not in o:
        print("!! 没有编译器，跳过 C++ 交叉验证")
        cli.close()
        return 1

    sftp = cli.open_sftp()
    sh(cli, "mkdir -p %s" % TMP)
    for f in ("pwdEncode.cpp", "pwdEncode.h"):
        sftp.put(os.path.join(HERE, f), "%s/%s" % (TMP, f))
    with sftp.open(TMP + "/main.cpp", "w") as fh:
        fh.write(MAIN)
    sftp.close()
    print("[OK] 已上传 pwdEncode.cpp/.h + main.cpp 到 %s（未碰 so_test 目录）" % TMP)

    o, e = sh(cli, "cd %s && g++ -O2 -o pwdcheck main.cpp pwdEncode.cpp 2>&1 && echo BUILD_OK" % TMP)
    print("[编译] %s" % (o.strip() or e.strip()))
    if "BUILD_OK" not in o:
        cli.close()
        return 1

    # 让 Python 侧也加载一份（用真 AES 那个实现）
    sys.path.insert(0, HERE)
    import pwd_encode as PE

    DATE = "20260901"
    cases = [("123123", "010100011300"),
             ("abcdef", "999999"),
             ("", "010100011300"),
             ("P@ssw0rd!", "010100011301")]

    print()
    print("=" * 74)
    print("方向 A：C++ 加密 → Python 解密（两层都验）")
    print("=" * 74)
    fail = 0
    for pwd, acct in cases:
        o, e = sh(cli, "cd %s && ./pwdcheck gen '%s' '%s' '%s'" % (TMP, pwd, acct, DATE))
        lines = [x for x in o.strip().splitlines() if x]
        if len(lines) < 2:
            print("  %-12r ✗ C++ 未输出 (%s)" % (pwd, (e or o)[:80]))
            fail += 1
            continue
        inner_cpp, outer_cpp = lines[0], lines[1]
        # Python 解外层拿内层
        try:
            inner_py = PE.decrypt_string(outer_cpp, DATE)
            plain_py = PE.decrypt_string(inner_py, acct)
        except Exception as ex:
            print("  %-12r ✗ Python 解密失败: %s" % (pwd, ex))
            fail += 1
            continue
        ok = (inner_py == inner_cpp) and (plain_py == pwd)
        if not ok:
            fail += 1
        print("  密码=%-12r 账号=%-14s 内层一致=%s 明文还原=%r %s"
              % (pwd, acct, inner_py == inner_cpp, plain_py, "✓" if ok else "✗"))

    print()
    print("=" * 74)
    print("方向 B：Python 加密 → C++ 解密")
    print("=" * 74)
    for pwd, acct in cases:
        enc = PE.encode_pwd(pwd, acct, DATE)
        # C++ 解外层
        o, e = sh(cli, "cd %s && ./pwdcheck dec '%s' '%s'" % (TMP, enc, DATE))
        inner_cpp = o.strip().splitlines()[0] if o.strip() else "<空>"
        if inner_cpp in ("<NULL>", "<空>"):
            print("  %-12r ✗ C++ 解外层失败" % pwd)
            fail += 1
            continue
        # C++ 解内层（注意：空明文时 C++ 打印空行，不能当成失败）
        o2, _ = sh(cli, "cd %s && ./pwdcheck dec '%s' '%s'" % (TMP, inner_cpp, acct))
        plain_cpp = o2.split("\n")[0].rstrip("\r") if o2 else ""
        if plain_cpp == "<NULL>":
            print("  %-12r ✗ C++ 解内层失败" % pwd)
            fail += 1
            continue
        ok = (plain_cpp == pwd)
        if not ok:
            fail += 1
        print("  密码=%-12r 账号=%-14s C++ 解出=%r %s"
              % (pwd, acct, plain_cpp, "✓" if ok else "✗"))

    print()
    print("=" * 74)
    print("清理 %s" % TMP)
    sh(cli, "rm -rf %s" % TMP)
    print("结果: %s" % ("全部一致 ✓" if fail == 0 else "%d 项不一致 ✗" % fail))
    cli.close()
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
