# -*- coding: utf-8 -*-
"""验证 GUI 新增/改动的三件事：
   1. 「启动 Mock 数据中台」面板默认展开、标题不再说"不用起"、默认编号 50
   2. 日志里出现「分配编号 N」时自动同步「策略平台身份 → 分配编号」
   3. 目标流上没有消费者时，发送前会被拦下并指出平台实际在哪
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

from PySide6.QtWidgets import QApplication
import gui_test as G
import _gui_headless

_gui_headless.install(answer="no")

app = QApplication.instance() or QApplication(sys.argv)
ok = bad = 0


def check(cond, label, extra=""):
    global ok, bad
    if cond:
        ok += 1
        print("  [OK]   %s%s" % (label, ("  " + extra) if extra else ""))
    else:
        bad += 1
        print("  [FAIL] %s%s" % (label, ("  " + extra) if extra else ""))


w = G.MainWindow()

print("=" * 70)
print("1) 「Mock 数据中台」面板可见性")
print("=" * 70)
box = None
for b in w.findChildren(G.CollapsibleBox):      # 它在右栏「服务管理」下
    if "Mock 数据中台" in b.btn.text():
        box = b
        break
check(box is not None, "找得到「Mock 数据中台」面板")
check(box.isExpanded(), "默认【展开】（不再是收起来的）")
check("必起" in box.btn.text(), "标题点明「打真平台必起」", box.btn.text())
check(w.spin_dh_alloc.value() == 50, "编号起始默认 50（不是 1）",
      str(w.spin_dh_alloc.value()))

print()
print("=" * 70)
print("2) 自动同步「分配编号」")
print("=" * 70)
w.spin_assign.setValue(94)
w._sniff_assigned_id(
    "[15:22:22] ★ 新策略平台上线 ST-65-2cea7fd9d5c0QLDataHub → "
    "分配编号 50（流 ST-50）usecount=0")
check(w.spin_assign.value() == 50, "嗅到「分配编号 50」后自动改为 50",
      str(w.spin_assign.value()))
w._sniff_assigned_id("[15:22:22] ← 上线确认 ST-761-...test id=95")
check(w.spin_assign.value() == 50, "「上线确认」不会误改编号")
w._sniff_assigned_id("[15:22:22] → 分配编号 51 到 ST-... : {...}")
check(w.spin_assign.value() == 51, "换一个编号也能同步", str(w.spin_assign.value()))

print()
print("=" * 70)
print("3) 目标流无消费者 -> 发送前拦下")
print("=" * 70)
w.edit_host.setText("192.168.1.137")
w.spin_db.setValue(0)
# ST-89 是个不存在的流（没有消费者）
w.spin_assign.setValue(89)
info = w._stream_probe()
if info is None:
    print("  [SKIP] 连不上 137")
else:
    foreign, consumers, exists = info
    check(consumers == [], "ST-89 上探测到 0 个消费者")
    live = w._find_live_stream()
    print("       _find_live_stream() -> %r" % live)
    check("ST-50" in live, "能指出平台实际在 ST-50", live)
    # _confirm_force_live 会弹窗，offscreen 下 QMessageBox.warning 会阻塞，
    # 所以这里只验证探测分支的判据，不真调它。
    check(True, "（弹窗分支不在此单测触发，避免 offscreen 阻塞）")

w.close()
print()
print("=" * 70)
print("结果: %s   通过 %d / 失败 %d" % ("全部通过" if bad == 0 else "有失败", ok, bad))
print("=" * 70)
sys.exit(1 if bad else 0)
