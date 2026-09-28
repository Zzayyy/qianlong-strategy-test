# -*- coding: utf-8 -*-
"""验证 GUI 新增的「允许打真平台」开关：
   1. 默认是关的（不持久化）
   2. 勾上后 argv 里会出现 --force-live
   3. 不勾时 argv 里没有
   4. _live_foreign() 能识别出真平台消费者（137 ST-50 上有 ST-50）
   5. 没有真平台时也不误报（干净流）
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

# 【必须】offscreen 下 QMessageBox 是模态的，没人点按钮就会永久阻塞。
# 换掉它，任何弹窗都变成"记录 + 自动回答"，测试才不会卡死。
_gui_headless.install(answer="no")

app = QApplication.instance() or QApplication(sys.argv)
ok_n = bad_n = 0


def check(cond, label, extra=""):
    global ok_n, bad_n
    if cond:
        ok_n += 1
        print("  [OK]   %s%s" % (label, ("  " + extra) if extra else ""))
    else:
        bad_n += 1
        print("  [FAIL] %s%s" % (label, ("  " + extra) if extra else ""))


w = G.MainWindow()
print("=" * 70)
print("1) 默认状态")
print("=" * 70)
check(hasattr(w, "chk_force_live"), "存在「允许打真平台」复选框")
check(w.chk_force_live.isChecked() is False, "默认【未勾选】（不持久化）")
check("允许打真平台" in w.chk_force_live.text(), "文案含「允许打真平台」",
      w.chk_force_live.text())

print()
print("=" * 70)
print("2) argv 组装")
print("=" * 70)
w.chk_force_live.setChecked(False)
a_off = w._send_argv("create", "normal")
check("--force-live" not in a_off, "未勾选 -> argv 无 --force-live")
w.chk_force_live.setChecked(True)
a_on = w._send_argv("create", "normal")
check("--force-live" in a_on, "勾选后 -> argv 含 --force-live")
# 预览分支不走 force-live（预览不发数据，没必要）
a_prev = w._send_argv("create", "normal", preview=True)
check("--force-live" not in a_prev, "预览模式不加 --force-live（预览不发数据）")

print()
print("=" * 70)
print("3) 真平台探测 _live_foreign()")
print("=" * 70)
# 指向 137 真平台占用的 ST-50
w.edit_host.setText("192.168.1.137")
w.spin_db.setValue(0)
w.spin_assign.setValue(50)
foreign = w._live_foreign()
if foreign is None:
    print("  [SKIP] 连不上 137，跳过探测断言")
else:
    names = [f["name"] for f in foreign]
    check(len(foreign) > 0, "ST-50 上探测到外来消费者（真平台）", "names=%s" % names)
    check("ST-50" in names, "消费者名是 ST-50（真平台命名）")

# 干净流：用高位编号，应该探测不到
w.spin_assign.setValue(199)
clean = w._live_foreign()
if clean is not None:
    check(len(clean) == 0, "ST-199（干净流）未误报外来消费者",
          "found=%s" % [f["name"] for f in clean])

print()
print("=" * 70)
print("4) _confirm_force_live 的行为")
print("=" * 70)
# 干净但【没人读】的流：现在会被「这条流没人读」拦下（这是刻意的）
w.spin_assign.setValue(199)
w.chk_force_live.setChecked(False)
_gui_headless.clear()
r = w._confirm_force_live()
check(r is False, "空流（无消费者）+ 未勾选 -> 拦下")
check("这条流没人读" in " ".join(_gui_headless.titles()),
      "拦下的理由是「没人读」", str(_gui_headless.titles()))

# 勾了「只发不收」时不该拦（纯发压测，本来就不等回包）
_gui_headless.clear()
w.chk_no_reply.setChecked(True)
r = w._confirm_force_live()
check(r is True, "勾了「只发不收」-> 空流也放行（不等回包）")
w.chk_no_reply.setChecked(False)

# 有真平台的流（ST-50）+ 未勾选 -> 问一次；自动回答"否"
w.spin_assign.setValue(50)
w.chk_force_live.setChecked(False)
_gui_headless.clear()
r = w._confirm_force_live()
check(r is False, "真平台流 + 未勾选 -> 弹窗问，答否 -> 不发")
check("会被安全闸拦下" in " ".join(_gui_headless.titles()),
      "弹的是「会被安全闸拦下」", str(_gui_headless.titles()))
check(w.chk_force_live.isChecked() is False, "答否之后开关没有被打开")

w.close()
print()
print("=" * 70)
print("结果: %s   通过 %d / 失败 %d" % ("全部通过" if bad_n == 0 else "有失败", ok_n, bad_n))
print("=" * 70)
sys.exit(1 if bad_n else 0)
