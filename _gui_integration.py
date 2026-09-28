# -*- coding: utf-8 -*-
"""
_gui_integration.py —— GUI 集成测试：真的通过界面起 Mock、发报文、收统计
=====================================================================
冒烟测试只验证了控件和命令拼装；这里验证最关键、最可能出问题的一环：
  ServiceProc（QProcess）能不能真的把 mock_strategy 拉起来、
  收到输出、并且 ST-1 被真建出来、报文能被消费回包。

流程（全程 offscreen，不需要人操作）：
  1. 构造窗口
  2. 调 w._start_strategy() 真启动 Mock 策略平台（QProcess）
  3. 等 ST-1 出现
  4. 用 w._run([...]) 真跑一次 send_test（少量条数）
  5. 校验 ST-1 有数据、回包落到 DataHub_reply_stream、汇总表有行
  6. 停止服务、清场
"""
import json
import os
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts.warning=false")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PySide6.QtWidgets import QApplication
import gui_test as G
import _gui_headless

# 【必须】offscreen 下 QMessageBox 是模态的，没人点按钮就永久阻塞（踩过）。
_gui_headless.install(answer="yes")
from resp_min import RespClient
import protocol as P

FAIL = []
ASSIGN = 94      # 高位号段，避开真平台


def check(name, cond, extra=""):
    print("  %s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                         ("  " + str(extra)) if extra else ""))
    if not cond:
        FAIL.append(name)


def pump(app, seconds):
    """跑 Qt 事件循环 N 秒（让 QProcess 的 readyRead 有机会触发）。"""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def main():
    app = QApplication(sys.argv)
    kw = dict(host="192.168.1.137", port=6379, password="QianLong@2026&", db=0)

    # 清场
    c = RespClient(**kw).connect()
    stream = P.stream_for(ASSIGN)
    c.delete(stream, P.reply_stream_for(ASSIGN))
    before_reply = c.xlen(P.STREAM_REPLY)
    print("清场完成；ST-%d 已删；回包流基线 XLEN=%s" % (ASSIGN, before_reply))

    print()
    print("=" * 72)
    print("1) 构造窗口并设置参数")
    print("=" * 72)
    w = G.MainWindow()
    w.show()
    app.processEvents()
    w.edit_host.setText("192.168.1.137")
    w.edit_pwd.setText("QianLong@2026&")
    w.spin_port.setValue(6379)
    w.spin_db.setValue(0)
    w.spin_assign.setValue(ASSIGN)
    w.spin_svc_workers.setValue(2)
    w.chk_svc_autoid.setChecked(True)
    w.chk_svc_suffix.setChecked(False)
    w.chk_ifaces["create"].setChecked(True)
    w.combo_type.setCurrentText("normal")
    w.spin_max.setValue(30)
    w.spin_workers.setValue(2)
    w.spin_wait.setValue(5)
    w.spin_sync.setValue(0)
    app.processEvents()
    check("参数已设置", w.spin_assign.value() == ASSIGN, w._stream_name())

    print()
    print("=" * 72)
    print("2) 通过界面启动 Mock 策略平台（走 ServiceProc / QProcess）")
    print("=" * 72)
    w._start_strategy()
    pump(app, 1.0)
    check("服务状态=运行中", w.svc_strategy.running,
          "proc state=%s" % w.svc_strategy.proc.state())
    check("按钮状态正确：启动已禁用", not w.btn_st_svc.isEnabled())
    check("按钮状态正确：停止已启用", w.btn_sp_svc.isEnabled())
    check("状态标签显示运行中", "运行中" in w.lbl_svc_st.text(), w.lbl_svc_st.text())

    # 等 ST-31 建出来
    got = False
    for _ in range(80):
        pump(app, 0.25)
        if c.cmd("EXISTS", stream):
            got = True
            break
    check("ST-%d 已被 Mock 创建" % ASSIGN, got)

    log_txt = w.log.toPlainText()
    check("日志里能看到启动命令", "mock_strategy.py" in log_txt)
    check("日志里能看到 mock 的输出", "XGROUP CREATE" in log_txt
          or "自应答" in log_txt or "开始消费" in log_txt)

    print()
    print("=" * 72)
    print("3) 通过界面发报文（走 Worker）")
    print("=" * 72)
    w._batch_start = time.time()
    cmds = [w._send_argv("create", "normal")]
    print("  命令: %s" % " ".join(cmds[0]))
    w._run(cmds)
    for _ in range(120):
        pump(app, 0.25)
        if w.worker and not w.worker.isRunning():
            break
    check("Worker 已结束", not (w.worker and w.worker.isRunning()))

    print()
    print("=" * 72)
    print("4) 校验结果")
    print("=" * 72)
    xlen = c.xlen(stream)
    after_reply = c.xlen(P.STREAM_REPLY)
    check("ST-%d 收到报文" % ASSIGN, xlen > 0, "XLEN=%s" % xlen)
    check("回包流有新增", after_reply > before_reply,
          "%s -> %s" % (before_reply, after_reply))
    grp = c.cmd("EXISTS", stream) and c.xinfo_groups(stream)
    grp = grp or []
    check("消费组存在", bool(grp), grp)
    if grp:
        check("pending=0（都 ack 了）", grp[0].get("pending") in (0, "0"),
              grp[0].get("pending"))
    # 核对 request_id 对得上
    req_ids = set()
    for eid, f in c.xrevrange(stream, 40):
        req_ids.add(f.get("request_id"))
    rep_ids = set()
    for eid, f in c.xrevrange(P.STREAM_REPLY, 40):
        rid = f.get("request_id", "")
        if rid in req_ids:
            rep_ids.add(rid)
    check("request_id 能配上回包", len(rep_ids) > 0,
          "%d 条匹配" % len(rep_ids))

    print()
    print("=" * 72)
    print("5) 统计汇总（GUI 自动刷新）")
    print("=" * 72)
    for _ in range(80):
        pump(app, 0.25)
        if w._rows:
            break
    check("汇总表有数据", len(w._rows) > 0, "%d 行" % len(w._rows))
    if w._rows:
        r = w._rows[0]
        check("汇总里发送数>0", r["sent"] > 0, r["sent"])
        check("汇总里回包数>0", r["reply"] > 0, r["reply"])

    print()
    print("=" * 72)
    print("6) 停止服务并清场")
    print("=" * 72)
    w.svc_strategy.stop()
    pump(app, 2.0)
    check("服务已停止", not w.svc_strategy.running)
    check("按钮状态已复位：启动可用", w.btn_st_svc.isEnabled())
    check("状态标签已复位", "未启动" in w.lbl_svc_st.text(), w.lbl_svc_st.text())

    c.delete(stream, P.reply_stream_for(ASSIGN))
    print("  已清理 ST-%d" % ASSIGN)
    c.close()
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
