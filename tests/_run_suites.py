# -*- coding: utf-8 -*-
"""带硬超时地跑一组 GUI 自测，避免某个用例卡住把整轮拖死。

用法: python _run_suites.py _gui_smoke.py _gui_noread.py ...
每个用例单独子进程 + 超时 kill，最后汇总。
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = ROOT   # 本脚本在 tests/ 下，项目根是上一层
TESTS = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
TIMEOUT = 200


def main(suites):
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    env["QT_LOGGING_RULES"] = "qt.qpa.fonts.warning=false"
    results = []
    for s in suites:
        t0 = time.time()
        # 脚本在 tests/ 下；cwd 用项目根（GUI 测试要相对项目根找 data/、out/）
        path = s if os.path.isabs(s) else os.path.join(TESTS, os.path.basename(s))
        try:
            p = subprocess.run([PY, "-u", path], cwd=HERE, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=TIMEOUT)
            out = p.stdout or ""
            rc = p.returncode
            status = "超时" if False else ("通过" if rc == 0 else "失败(rc=%d)" % rc)
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or "")
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            rc = -1
            status = "!!! 超时 %ds，已强杀" % TIMEOUT
        d = time.time() - t0
        # 抓结果行与失败项
        lines = [l for l in out.splitlines()
                 if l.startswith("结果:") or l.strip().startswith("- ")]
        print("%-24s %-18s %6.1fs  %s"
              % (s, status, d, " | ".join(lines[:6])))
        results.append((s, status, rc))
    print()
    bad = [r for r in results if r[2] != 0]
    print("合计 %d 套，失败 %d 套" % (len(results), len(bad)))
    for s, st, _ in bad:
        print("   ✗ %s  %s" % (s, st))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or [
        "_gui_smoke.py", "_gui_noread.py", "_gui_clearlog.py",
        "_gui_forcelive.py", "_gui_dhflow.py", "_gui_paths.py",
        "_gui_integration.py"]))
