# -*- coding: utf-8 -*-
"""
_gui_paths.py —— 验证 GUI 的「预览」和「破坏测试」两条按钮路径真的能跑通
=====================================================================
_gui_integration.py 验的是「开始发送」（normal）。这里补上另外两个主按钮：
  - 预览报文（--no-send，不写 Redis）
  - 破坏测试（--type destroy，真写 Redis）
并确认预览确实不往 Redis 写数据。
"""
import os
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts.warning=false")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = ROOT   # 本脚本在 tests/ 下，项目根是上一层
sys.path.insert(0, HERE)

from PySide6.QtWidgets import QApplication
import gui_test as G
import _gui_headless

# 【必须】offscreen 下 QMessageBox 是模态的，没人点按钮就永久阻塞（踩过）。
# 本用例要真的发出去，所以自动回答"是"。
_gui_headless.install(answer="yes")
from resp_min import RespClient
import protocol as P

FAIL = []
ASSIGN = 95      # 高位号段，避开真平台


def check(name, cond, extra=""):
    print("  %s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                         ("  " + str(extra)) if extra else ""))
    if not cond:
        FAIL.append(name)


def pump(app, s):
    end = time.time() + s
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def wait_worker(app, w, limit=120):
    for _ in range(limit):
        pump(app, 0.25)
        if w.worker and not w.worker.isRunning():
            return True
    return False


def main():
    app = QApplication(sys.argv)
    kw = dict(host="192.168.1.137", port=6379, password="QianLong@2026&", db=0)
    c = RespClient(**kw).connect()
    stream = P.stream_for(ASSIGN)
    c.delete(stream, P.reply_stream_for(ASSIGN))

    w = G.MainWindow()
    w.show()
    w.edit_host.setText("192.168.1.137")
    w.edit_pwd.setText("QianLong@2026&")
    w.spin_assign.setValue(ASSIGN)
    w.spin_svc_workers.setValue(2)
    w.chk_ifaces["create"].setChecked(True)
    pump(app, 0.3)

    print()
    print("=" * 72)
    print("1) 预览报文（--no-send）应【不】写 Redis")
    print("=" * 72)
    w.combo_type.setCurrentText("normal")
    exists_before = bool(c.cmd("EXISTS", stream))
    w.on_preview()
    ok = wait_worker(app, w)
    check("预览任务跑完", ok)
    txt = w.log.toPlainText()
    check("日志含 'XADD'（预览输出）", "XADD" in txt)
    check("日志含 '预览模式'", "预览模式" in txt)
    exists_after = bool(c.cmd("EXISTS", stream))
    check("预览没有创建 ST-%d" % ASSIGN,
          not exists_after or c.xlen(stream) == 0,
          "exists=%s XLEN=%s" % (exists_after,
                                 c.xlen(stream) if exists_after else "-"))

    print()
    print("=" * 72)
    print("2) 破坏测试（--type destroy）真写 Redis")
    print("=" * 72)
    # 先起 Mock 策略平台，否则没人消费
    w._start_strategy()
    pump(app, 1.0)
    check("Mock 策略平台已启动", w.svc_strategy.running)
    got = False
    for _ in range(80):
        pump(app, 0.25)
        if c.cmd("EXISTS", stream):
            got = True
            break
    check("ST-%d 已建" % ASSIGN, got)

    before = c.xlen(P.STREAM_REPLY) if c.cmd("EXISTS", P.STREAM_REPLY) else 0
    w.combo_type.setCurrentText("destroy")
    w.spin_max.setValue(50)
    w.spin_workers.setValue(2)
    w.spin_wait.setValue(5)
    w.on_send("destroy")
    ok = wait_worker(app, w, 160)
    check("破坏测试跑完", ok)

    xlen = c.xlen(stream)
    check("ST-%d 收到破坏报文" % ASSIGN, xlen > 0, "XLEN=%s" % xlen)
    after = c.xlen(P.STREAM_REPLY)
    check("坏报文也被消费并回包", after > before, "%s -> %s" % (before, after))
    grp = c.xinfo_groups(stream)
    if grp:
        check("pending=0", grp[0].get("pending") in (0, "0"), grp[0].get("pending"))

    # 确认畸形 task 确实进流了（抽一条看看）
    bad = 0
    for eid, f in c.xrevrange(stream, 60):
        t = str(f.get("task", ""))
        if not t.startswith("{") or '"MsgType"' not in t:
            bad += 1
    check("流里确实有畸形报文", bad > 0, "%d 条不像正常 JSON" % bad)

    print()
    print("=" * 72)
    print("3) 汇总表刷新")
    print("=" * 72)
    for _ in range(80):
        pump(app, 0.25)
        if w._rows:
            break
    check("汇总表有数据", len(w._rows) > 0, "%d 行" % len(w._rows))

    print()
    print("=" * 72)
    print("4) 收尾")
    print("=" * 72)
    w.svc_strategy.stop()
    pump(app, 1.5)
    check("Mock 已停止", not w.svc_strategy.running)
    c.delete(stream, P.reply_stream_for(ASSIGN))
    c.close()
    print("  已清理 ST-%d" % ASSIGN)
    try:
        w.close()
    except Exception:
        pass

    print()
    print("=" * 72)
    if FAIL:
        print("结果: %d 项失败" % len(FAIL))
        for f in FAIL:
            print("   - %s" % f)
    else:
        print("结果: 全部通过")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
