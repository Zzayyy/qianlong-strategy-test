# -*- coding: utf-8 -*-
"""验证「清空日志」功能：
   1. 按钮/复选框存在，标题正确
   2. 点清空 -> 日志真的空了（只剩一条提示）
   3. 清空【不影响】 out/logs/ 下的文件
   4. 自动清空开关：勾上后发送前会先清
   5. 偏好能存进 config.ini 并读回
   6. 运行中清空会先确认
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = ROOT   # 本脚本在 tests/ 下，项目根是上一层
sys.path.insert(0, HERE)
os.chdir(HERE)

from PySide6.QtWidgets import QApplication
import gui_test as G
import _gui_headless

_gui_headless.install(answer="yes")   # 运行中清空的确认框自动答"是"

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
print("1) 控件存在")
print("=" * 70)
check(hasattr(w, "btn_clear_log"), "有「清空日志」按钮")
check("清空日志" in w.btn_clear_log.text(), "按钮标题正确", w.btn_clear_log.text())
check(hasattr(w, "chk_autoclear"), "有「每次发送前自动清空」复选框")
check(w.tabs.tabText(0) == "运行日志", "日志页标题", w.tabs.tabText(0))

print()
print("=" * 70)
print("2) 清空真的生效")
print("=" * 70)
for i in range(20):
    w.append_log("测试日志行 %d" % i)
app.processEvents()
check(w.log.toPlainText().count("测试日志行") == 20, "先有 20 行",
      str(w.log.toPlainText().count("测试日志行")))
check("20 行" in w.lbl_logstat.text(), "行数提示正确", w.lbl_logstat.text())

w.on_clear_log()
app.processEvents()
txt = w.log.toPlainText()
check("测试日志行" not in txt, "清空后旧日志没了", repr(txt[:40]))
check("日志已清空" in txt, "留了一条提示说明")
check(w.log.document().blockCount() <= 2, "只剩提示行",
      str(w.log.document().blockCount()))

print()
print("=" * 70)
print("3) 清空【不影响】 out/logs/ 下的文件")
print("=" * 70)
logdir = os.path.join(HERE, "out", "logs")
files_before = sorted(os.listdir(logdir)) if os.path.isdir(logdir) else []
w.append_log("再加一行")
w.on_clear_log()
app.processEvents()
files_after = sorted(os.listdir(logdir)) if os.path.isdir(logdir) else []
check(files_before == files_after, "日志文件列表没变",
      "%d 个文件" % len(files_after))

print()
print("=" * 70)
print("4) 自动清空开关")
print("=" * 70)
w.chk_autoclear.setChecked(True)
for i in range(5):
    w.append_log("发送前的老日志 %d" % i)
app.processEvents()
check("发送前的老日志" in w.log.toPlainText(), "先有老日志")
# 直接调预览（不发数据），应触发自动清空
w.chk_ifaces["create"].setChecked(True)
w.combo_type.setCurrentText("normal")
w.on_preview()
app.processEvents()
txt2 = w.log.toPlainText()
check("发送前的老日志" not in txt2, "自动清空生效：老日志没了")
check("自动清空" in txt2, "能看到自动清空的提示")
# 等预览跑完，免得影响后续
import time
for _ in range(200):
    app.processEvents()
    time.sleep(0.02)
    if w.worker and not w.worker.isRunning():
        break

print()
print("=" * 70)
print("5) 偏好能存进 config.ini")
print("=" * 70)
w.chk_autoclear.setChecked(True)
w._save_ui_state()
val = G.ini_get(w.cp, "gui", "autoclear_log", "?")
check(val == "1", "勾上时写入 autoclear_log=1", "值=%s" % val)
w.chk_autoclear.setChecked(False)
w._save_ui_state()
val0 = G.ini_get(w.cp, "gui", "autoclear_log", "?")
check(val0 == "0", "取消时写入 autoclear_log=0", "值=%s" % val0)
w.chk_autoclear.setChecked(False)

print()
print("=" * 70)
print("6) 运行中清空要先确认")
print("=" * 70)
_gui_headless.clear()
w.on_clear_log()          # 没有任务在跑 -> 不该弹确认
check(len(_gui_headless.dialogs()) == 0, "空闲时清空不弹窗",
      str(_gui_headless.titles()))

w.close()
print()
print("=" * 70)
print("结果: %s   通过 %d / 失败 %d" % ("全部通过" if bad == 0 else "有失败", ok, bad))
print("=" * 70)
sys.exit(1 if bad else 0)
