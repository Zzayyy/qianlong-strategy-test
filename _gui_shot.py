# -*- coding: utf-8 -*-
"""
_gui_shot.py —— 把 GUI 各状态截成 PNG，用于目视检查布局
=====================================================
offscreen 平台也能抓 QWidget.grab()，所以不需要真人看屏幕。
产出 out/_shot_*.png。
"""
import os
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts.warning=false")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PySide6.QtWidgets import QApplication
import gui_test as G

OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)


def pump(app, s=0.4):
    end = time.time() + s
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def shot(w, app, name):
    pump(app, 0.5)
    w.resize(1500, 950)
    pump(app, 0.5)
    p = os.path.join(OUT, "_shot_%s.png" % name)
    w.grab().save(p)
    print("  已保存 %s (%d bytes)" % (p, os.path.getsize(p)))
    return p


def main():
    app = QApplication(sys.argv)
    w = G.MainWindow()
    w.resize(1500, 950)
    w.show()
    pump(app, 1.0)

    print("截图 1：初始状态")
    shot(w, app, "1_default")

    print("截图 2：勾选 destroy + 填用例编号")
    w.combo_type.setCurrentText("destroy")
    w.edit_cases.setText("C201,C203-C210")
    w.chk_ifaces["create"].setChecked(True)
    w.chk_ifaces["modify"].setChecked(True)
    pump(app, 0.3)
    shot(w, app, "2_destroy")

    print("截图 3：切到统计汇总页（先刷新）")
    w._refresh_summary()
    for _ in range(60):
        pump(app, 0.1)
        if w._rows:
            break
    w.tabs.setCurrentIndex(1)
    shot(w, app, "3_summary")

    print("截图 4：运行日志页（模拟一段日志）")
    w.tabs.setCurrentIndex(0)
    for line in [
        "$ .../mock_strategy.py --host 192.168.1.137 --db 0 --assign-id 1",
        "[策略平台] 已连接 Redis 192.168.1.137:6379 db0",
        "[策略平台]   XGROUP CREATE ST-1 user_group -> OK",
        "[策略平台] 自应答模式：直接使用分配编号 id=1 -> 下发流 ST-1",
        "[策略平台] 开始消费 ST-1 / ST-1-reply（consumer=ST-1-w0）",
        "[策略平台] ← [ST-1] #1790219901492-0 request_id=STTEST_x_1  MsgType=4 create(插入条件单)",
        "$ .../send_test.py --assign-id 1 --interface create --type normal --workers 8",
        "  发送条数          : 2000  (1238.5 条/秒, 失败 0)",
        "  回包条数          : 2000  (1238.5 条/秒, 超时未回 0, 在途 0)",
        "  同步RTT探测: 平均 1.135 ms",
        "[完成] 退出码 0",
    ]:
        w.append_log(line)
    shot(w, app, "4_log")

    print("截图 5：左栏折叠起来的样子")
    w.tabs.setCurrentIndex(0)
    for b in (w.left_scroll.widget().findChildren(G.CollapsibleBox)):
        pass
    shot(w, app, "5_final")

    print("截图 6：发送参数面板展开，显示「允许打真平台」开关")
    # 展开「4. 发送参数」这一栏（标题在 btn 上）
    for b in w.left_scroll.widget().findChildren(G.CollapsibleBox):
        if "发送参数" in b.btn.text():
            b.setExpanded(True)
            break
    w.chk_force_live.setChecked(True)
    pump(app, 0.3)
    shot(w, app, "6_forcelive")

    print()
    print("提示：这些 PNG 只是 offscreen 渲染，字体可能与真机略有差异。")
    w.close()


if __name__ == "__main__":
    main()
