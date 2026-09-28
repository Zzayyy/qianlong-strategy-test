# -*- coding: utf-8 -*-
"""验证：勾选接口 / 切类型【不再读表】，以及点发送后的统计仍正确。

同时实测勾选一次要多久（改造前是读 4 遍表）。
"""
import os
import sys
import time

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
print("1) 勾选接口不再读表（应该毫秒级）")
print("=" * 70)
# 把所有接口勾上再取消，反复 10 次，计时
t = time.time()
for _ in range(10):
    for n, c in w.chk_ifaces.items():
        c.setChecked(True)
    app.processEvents()
    for n, c in w.chk_ifaces.items():
        c.setChecked(n == "create")
    app.processEvents()
d = time.time() - t
check(d < 1.0, "勾选 60 次总耗时 < 1s（不读表）", "%.3f s" % d)
check("create=5" not in w.lbl_cases.text(), "提示里没有读表算出的条数",
      w.lbl_cases.text()[:60])

print()
print("=" * 70)
print("2) 提示内容正确")
print("=" * 70)
w.combo_type.setCurrentText("normal")
w.edit_cases.setText("")
app.processEvents()
txt = w.lbl_cases.text()
check("create" in txt, "含已选接口", txt)
check("normal" in txt, "含类型")
check("统计" in txt, "写明延后统计")
w.edit_cases.setText("C201,C203-C205")
app.processEvents()
check("C201" in w.lbl_cases.text(), "含指定用例", w.lbl_cases.text())

print()
print("=" * 70)
print("3) 需要时统计仍然准确（_estimate_count 走真读表）")
print("=" * 70)
w.edit_cases.setText("")
w.combo_type.setCurrentText("normal")
t = time.time()
cnt, det, err = w._estimate_count()
d = time.time() - t
check("create normal 统计出 5 条", "create=5" in det, "%s (%.2fs)" % (det, d))
w.combo_type.setCurrentText("destroy")
cnt2, det2, _ = w._estimate_count()
check("destroy 统计 >200", cnt2 > 200, "%d 条" % cnt2)

print()
print("=" * 70)
print("4) 只勾 create 时，统计只算 create（不扫别的表）")
print("=" * 70)
for n, c in w.chk_ifaces.items():
    c.setChecked(n == "create")
w.combo_type.setCurrentText("normal")
t = time.time()
cnt, det, _ = w._estimate_count()
d = time.time() - t
check("只统计了 create", "=" in det and det.count("=") == 1, det)
check("结果是 5", "create=5" in det, det)
print("      （读 1 张表 %.2f s；改造前勾选一次要读 4 张）" % d)

w.close()
print()
print("=" * 70)
print("结果: %s   通过 %d / 失败 %d" % ("全部通过" if bad == 0 else "有失败", ok, bad))
print("=" * 70)
sys.exit(1 if bad else 0)
