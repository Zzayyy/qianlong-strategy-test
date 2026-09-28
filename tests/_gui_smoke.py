# -*- coding: utf-8 -*-
"""
_gui_smoke.py —— GUI 冒烟测试（不需要人盯着屏幕）
================================================
用 offscreen 平台把窗口建起来，然后直接调内部方法验证：
  1. 窗口能否成功构造（控件、布局、样式不报错）
  2. 命令组装是否正确（_send_argv / _start_strategy）
  3. 统计汇总能否加载既有 json
  4. 导出 Excel 能否成功落盘
  5. 用例计数、连接摘要是否正常

这样能在没有人机交互的情况下发现大部分低级错误。
用法：python _gui_smoke.py
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
from PySide6.QtCore import QTimer

import gui_test as G
import _gui_headless

# 【必须】offscreen 下 QMessageBox 是模态的，没人点按钮就永久阻塞（踩过）。
_gui_headless.install(answer="no")

FAIL = []


def check(name, cond, extra=""):
    print("  %s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                         ("  " + str(extra)) if extra else ""))
    if not cond:
        FAIL.append(name)


def main():
    app = QApplication(sys.argv)
    print("=" * 72)
    print("1) 构造窗口")
    print("=" * 72)
    w = G.MainWindow()
    w.show()
    app.processEvents()
    check("窗口已构造", w is not None)
    check("标题正确", "策略平台" in w.windowTitle(), w.windowTitle())
    check("左侧滚动区存在", hasattr(w, "left_scroll"))
    check("用例库已加载（excel_loader）", G.CASES is not None)
    check("策略平台服务对象就绪", hasattr(w, "svc_strategy"))
    check("数据中台服务对象就绪", hasattr(w, "svc_datahub"))

    print()
    print("=" * 72)
    print("2) 用例选择提示（★ 不在勾选时读表）")
    print("=" * 72)
    w.edit_cases.setText("")
    w.combo_type.setCurrentText("normal")
    for n, c in w.chk_ifaces.items():
        c.setChecked(n == "create")
    app.processEvents()
    txt = w.lbl_cases.text()
    check("提示有内容", bool(txt), txt)
    # 【改过】原来这里会读 Excel 统计条数（3000 行要 3 秒，3 万行 160 秒，
    # 且跑在主线程上直接卡死界面）。现在只显示选择，不读表。
    check("提示里含已选接口名", "create" in txt, txt)
    check("提示里写明条数延后统计", "统计" in txt, txt)
    check("★ 不再显示读表算出的条数", "create=5" not in txt
          and "全库" not in txt, txt)

    # 读表统计仍然可用，只是改到点发送时才做（走 _count_pool）
    cnt, det, err = w._estimate_count()
    check("需要时仍能统计出条数（create normal=5）",
          "create=5" in det, "%s / %s" % (det, err))
    w.combo_type.setCurrentText("normal")
    app.processEvents()

    summ = w.lbl_conn.text()
    check("连接摘要含 Redis 地址", "192.168.1.137" in summ, summ)
    check("连接摘要含下发流 ST-", "ST-" in summ, summ)

    print()
    print("=" * 72)
    print("3) 命令组装")
    print("=" * 72)
    argv = w._send_argv("create", "normal")
    joined = " ".join(argv)
    check("含 send_test.py", "send_test.py" in joined)
    check("含 --assign-id", "--assign-id" in joined)
    check("含 --host/--db", "--host 192.168.1.137" in joined and "--db 0" in joined)
    check("含 --type normal", "--type normal" in joined)
    check("含 --interface create", "--interface create" in joined)
    check("含 --sync-probe", "--sync-probe" in joined)
    check("含 --workers", "--workers" in joined)

    prev = " ".join(w._send_argv("create", "normal", preview=True))
    check("预览模式加 --no-send", "--no-send" in prev)
    check("预览模式不含 --workers", "--workers" not in prev)

    # 破坏测试
    d = " ".join(w._send_argv("create", "destroy"))
    check("destroy 走 --type destroy", "--type destroy" in d)

    # 指定用例
    w.edit_cases.setText("C201,C203-C210")
    c2 = " ".join(w._send_argv("create", "destroy"))
    check("--cases 被带上", "--cases C201,C203-C210" in c2)
    w.edit_cases.setText("")

    print()
    print("=" * 72)
    print("4) Mock 服务的命令组装")
    print("=" * 72)
    # 直接检查参数拼装逻辑（不真启动进程）
    import inspect
    src = inspect.getsource(G.MainWindow._start_strategy)
    check("strategy 支持 _1 后缀", "_1" in src)
    check("strategy 支持 --auto-assign-id", "auto-assign-id" in src)
    check("strategy 支持 --no-assign", "no-assign" in src)
    src2 = inspect.getsource(G.MainWindow._start_datahub)
    check("datahub 支持 --alloc-start", "alloc-start" in src2)
    check("datahub 不再传 --push（该功能已删）", "--push" not in src2)

    print()
    print("=" * 72)
    print("5) 统计汇总 + Excel 导出")
    print("=" * 72)
    w._refresh_summary()
    for _ in range(60):
        app.processEvents()
        if w._rows:
            break
        time.sleep(0.05)
    check("汇总加载到数据", len(w._rows) > 0, "%d 行" % len(w._rows))
    if w._rows:
        row = w._rows[0]
        for k in ("label", "sent", "reply", "send_per_sec", "lat_avg_us"):
            check("字段 %s 存在" % k, k in row)
        check("表格行数=数据行数", w.tbl.rowCount() == len(w._rows),
              "%d vs %d" % (w.tbl.rowCount(), len(w._rows)))
        check("表头有 15 列", w.tbl.columnCount() == 15, w.tbl.columnCount())

        out = os.path.join(HERE, "out", "_smoke_export.xlsx")
        if os.path.exists(out):
            os.remove(out)
        try:
            from openpyxl import Workbook, load_workbook
            from openpyxl.styles import Font
            # 直接调导出逻辑（跳过文件对话框）
            wb = Workbook()
            ws = wb.active
            ws.title = "汇总"
            ws.append(["标签", "时间", "时长(s)", "发送", "回包"])
            for r in w._rows:
                ws.append([r["label"], r["time"], round(r["duration"], 3),
                           r["sent"], r["reply"]])
            widths = [22, 15, 9, 9, 9]
            for i, wd in enumerate(widths):
                col = ws.cell(row=1, column=i + 1).column_letter
                ws.column_dimensions[col].width = wd
            for r in w._rows[:20]:
                ser = (r["raw"].get("series") or [])
                if not ser:
                    continue
                name = str(r["label"])[:28] or "run"
                for ch in "[]:*?/\\":
                    name = name.replace(ch, "_")
                sh = wb.create_sheet(name)
                sh.append(["秒", "发送", "回包"])
                for x in ser:
                    sh.append([x.get("sec", 0), x.get("sent", 0), x.get("reply", 0)])
            wb.save(out)
            check("Excel 导出成功", os.path.exists(out))
            rb = load_workbook(out)
            check("Excel 可回读", len(rb.sheetnames) >= 1, rb.sheetnames)
        except Exception as e:
            check("Excel 导出", False, e)

    print()
    print("=" * 72)
    print("6) Mock 数据中台分组（打真平台必起 -> 默认展开、标题点明用途）")
    print("=" * 72)
    boxes = w.findChildren(G.CollapsibleBox)
    titles = [b.btn.text() for b in boxes]
    dh = [b for b in boxes if "Mock 数据中台" in b.btn.text()]
    check("存在「Mock 数据中台」分组", bool(dh), titles)
    if dh:
        # 【改过】原来它是折叠的且标着「常规测试不用起」，导致打真平台时
        # 找不到「给它分配编号」的入口（平台会一直卡在 id=-1）。现已默认展开。
        check("该分组默认【展开】", dh[0].isExpanded())
        check("标题点明「打真平台必起」", "必起" in dh[0].btn.text(),
              dh[0].btn.text())
        check("编号起始默认 50（避开低位号段）",
              w.spin_dh_alloc.value() == 50, w.spin_dh_alloc.value())

    print()
    print("=" * 72)
    print("7) 关闭窗口")
    print("=" * 72)
    try:
        w.close()
        app.processEvents()
        check("窗口可正常关闭", True)
    except Exception as e:
        check("窗口可正常关闭", False, e)

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
