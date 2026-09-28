# -*- coding: utf-8 -*-
"""验证 GUI 的「生成压测数据」：
   1. 控件存在、默认值正确
   2. 命令拼装正确（--bulk-normal / --bulk-start / --ref-map 只给 modify/remove）
   3. 校验逻辑：填了 ref_map 但 bulk=0 要拦下
   4. 真跑一次小批量，确认 data/*.xlsx 被重写且 ref 被回填

全程 offscreen，不发送任何数据。
"""
import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from PySide6.QtWidgets import QApplication
import gui_test as G
import _gui_headless

_gui_headless.install(answer="yes")   # 确认框一律答"是"

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


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def wait_done(limit=200):
    for _ in range(limit):
        pump(0.25)
        if w.worker and not w.worker.isRunning():
            return True
    return False


w = G.MainWindow()

print("=" * 70)
print("1) 控件存在与默认值")
print("=" * 70)
check(hasattr(w, "btn_gen"), "有「生成压测数据」按钮", w.btn_gen.text())
check(hasattr(w, "spin_bulk_accounts"), "有「批量账号数」")
check(hasattr(w, "spin_bulk_start"), "有「账号起始」")
check(hasattr(w, "edit_ref_map"), "有「Ref回填」输入框")
check(hasattr(w, "btn_open_data"), "有「打开 data 目录」")
# 这两个值会从 config.ini 读回来（跑过测试后可能非 0），所以只断言"能读到"，
# 不断言具体数值——否则测试自己污染自己（踩过：第一轮写进去 10000，第二轮就挂）。
check(isinstance(w.spin_bulk_accounts.value(), int), "「批量账号数」可读",
      str(w.spin_bulk_accounts.value()))
check(w.spin_bulk_start.value() == 0, "账号起始默认 0")
# 先把状态复位成"干净默认"，后面的用例才有确定前提
w.spin_bulk_accounts.setValue(0)
w.edit_ref_map.setText("")

print()
print("=" * 70)
print("2) 命令拼装")
print("=" * 70)
# 直接检查 on_generate 会拼出什么：临时记录 _run 的入参
captured = {}
orig_run = w._run


def fake_run(cmds, on_done=None):
    captured["cmds"] = cmds
    captured["on_done"] = on_done


w._run = fake_run

# 勾 create + remove
for n, c in w.chk_ifaces.items():
    c.setChecked(n in ("create", "remove"))
w.spin_bulk_accounts.setValue(100)
w.spin_bulk_start.setValue(0)
w.edit_ref_map.setText("")
w.on_generate()
cmds = captured.get("cmds") or []
check(len(cmds) == 2, "勾了 2 个接口 -> 2 条命令", str(len(cmds)))
joined = [" ".join(c) for c in cmds]
check(any("--interface create" in j and "--bulk-normal 100" in j for j in joined),
      "create 带 --bulk-normal 100")
check(any("--interface remove" in j for j in joined), "remove 有命令")
check(all("--ref-map" not in j for j in joined), "没选 Ref文件时不加 --ref-map")

# 造一个假 refs.json 再试
os.makedirs(os.path.join(ROOT, "out", "performance"), exist_ok=True)
fake = os.path.join(ROOT, "out", "performance", "_gui_test_refs.json")
json.dump([{"account": "010100%06d" % (11301 + i), "case_no": "CB%05d" % (i + 1),
            "ref": "20260928%06d" % (70000 + i)} for i in range(100)],
          open(fake, "w", encoding="utf-8"), ensure_ascii=False)
w.edit_ref_map.setText(fake)
captured.clear()
w.on_generate()
cmds = captured.get("cmds") or []
joined = [" ".join(c) for c in cmds]
check(any("--interface remove" in j and "--ref-map" in j for j in joined),
      "★ remove 带上 --ref-map")
check(any("--interface create" in j and "--ref-map" in j for j in joined) is False,
      "create 不该带 --ref-map（它不引用单号）")

print()
print("=" * 70)
print("3) 校验：填了 Ref文件但批量账号=0 要拦下")
print("=" * 70)
w.spin_bulk_accounts.setValue(0)
_gui_headless.clear()
captured.clear()
w.on_generate()
check("cmds" not in captured, "被拦下，没有执行生成",
      str(list(captured.keys())))
check("参数不匹配" in " ".join(_gui_headless.titles()),
      "弹出了「参数不匹配」", str(_gui_headless.titles()))

print()
print("=" * 70)
print("4) 真跑一次小批量（10 条），确认文件被重写 + ref 被回填")
print("=" * 70)
w._run = orig_run          # 恢复真执行
w.spin_bulk_accounts.setValue(10)
w.edit_ref_map.setText(fake)
for n, c in w.chk_ifaces.items():
    c.setChecked(n in ("remove",))
w.on_generate()
check("生成任务跑完", wait_done(), "worker 已结束")

import excel_loader as XL
p = XL.default_excel("remove")
pool = XL.load_cases(p, want_types={"normal"}, quiet=True)
check(len(pool) == 10, "remove normal 变成 10 条", str(len(pool)))
sample = json.loads(pool[0][3])["remove"]
# 假 refs.json 里第 1 条 ref = 20260928070000
check(str(sample.get("Ref", "")) == "20260928070000",
      "★ Ref 被回填成 refs.json 里的真实单号", str(sample.get("Ref")))
check("__REF" not in str(sample.get("Ref")),
      "★ 不再是 __REF token（说明回填真生效了）")

print()
print("=" * 70)
print("5) 收尾：恢复 remove 的 1 万条 + 清理假文件")
print("=" * 70)
try:
    os.remove(fake)
except Exception:
    pass
# 测试把 remove 改成了 10 条，恢复成 1 万条（避免污染后续使用）
import subprocess
subprocess.run([sys.executable, "make_excel.py", "--interface", "remove",
                "--bulk-normal", "10000"], cwd=ROOT,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
pool2 = XL.load_cases(XL.default_excel("remove"), want_types={"normal"}, quiet=True)
check(len(pool2) == 10000, "remove 已恢复成 1 万条", str(len(pool2)))
# 顺手把 GUI 偏好复位，别把测试值留在 config.ini 里
w.spin_bulk_accounts.setValue(0)
w.edit_ref_map.setText("")
w._save_ui_state()
w.close()

print()
print("=" * 70)
print("结果: %s   通过 %d / 失败 %d" % ("全部通过" if bad == 0 else "有失败", ok, bad))
print("=" * 70)
sys.exit(1 if bad else 0)
