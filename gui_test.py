# -*- coding: utf-8 -*-
"""
gui_test.py —— 策略平台测试 GUI（PySide6）
=========================================
把策略方向的那几个命令行工具包成一个界面，免去敲参数：

    send_test.py      手动 XADD 发送器（normal / destroy）
    mock_strategy.py  模拟策略平台（收 ST-N、回 DataHub_reply_stream、上线+心跳）
    mock_datahub.py   模拟数据中台（策略方向：应答上线、分配编号、发心跳）
    make_excel.py     生成/扩充用例 Excel

界面结构（参照 datahub_test/gui_test.py 的布局习惯）：
    顶栏        标题 + 当前连接摘要
    左栏(可滚动) 1 测试数据（造/选数据）
                 2 发送参数（发送范围 / 规模与速率 / 回复处理 / 稳定性测试）
    右栏(可滚动) 服务管理（起/停 Mock 策略平台/数据中台）
                 + 连接设置 / 策略平台身份 / 输出
    下栏标签页   运行日志 | 统计汇总（各带一条工具条）
    底部按钮    预览报文 | 运行稳定性测试 | 开始发送 | 停止

依赖：venv 已装 PySide6 + openpyxl。
运行：venv/Scripts/python.exe gui_test.py
"""
import configparser
import json
import os
import re
import subprocess
import sys
import time

# 屏蔽 Qt 在 Windows 上枚举旧系统字体失败的无害警告（Fixedsys/MS Sans Serif 等）
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts.warning=false")

from PySide6.QtCore import (Qt, QThread, Signal, QObject, QProcess,
                            QProcessEnvironment, QEvent)
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QComboBox, QPushButton, QSpinBox,
    QDoubleSpinBox, QCheckBox, QLineEdit, QTextEdit, QGroupBox,
    QGridLayout, QVBoxLayout, QHBoxLayout, QMessageBox,
    QAbstractSpinBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QSplitter, QFileDialog, QTabWidget, QToolButton, QScrollArea,
    QFrame, QSizePolicy,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")
PERF_DIR = os.path.join(OUT_DIR, "performance")
LOG_DIR = os.path.join(OUT_DIR, "logs")
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
PYTHON = sys.executable

# 日志区最多保留行数（超出自动丢最旧的，防止界面卡死）
MAX_LOG_LINES = 4000

sys.path.insert(0, BASE_DIR)


def _try_import_cases():
    """导入接口清单（拿接口列表和用例数）。失败不影响 GUI 启动。

    用例数据现在都在 data/*.xlsx（由 make_excel.py 从 interfaces/ 生成），
    所以这里只探测"有哪些接口"，计数走 excel_loader。
    """
    try:
        import excel_loader
        return excel_loader
    except Exception as e:
        print("[WARN] 导入 excel_loader 失败: %s" % e)
        return None


CASES = _try_import_cases()


# ==================== 滚轮防误改 ====================
class WheelGuard(QObject):
    """让 SpinBox/ComboBox 不再"滚轮悬停即改值"，改为滚动外层滚动区。

    背景：Qt 的 QAbstractSpinBox / QComboBox 默认把滚轮当作"改值"操作。
    在「发送参数」这种内容超高、需要滚动的面板里，用户只想滚页面，
    鼠标划过输入框却把参数改了 —— 而且常常没察觉（如把 10000 改成 9999）。

    【不能用 hasFocus() 判断】—— 这是踩过的坑：
      这些控件的 focusPolicy 默认是 Qt.WheelFocus，含义包含
      "鼠标滚轮也能让它获得焦点"。真实流程是：
        滚轮事件到达 -> Qt 先把焦点给该控件 -> 再投递给它
      所以过滤器里读到的 hasFocus() 恒为 True，永远走"放行"分支，
      防护形同虚设。
      （早期用 sendEvent 直接投给控件测试时看不到这个问题，因为那条路径
        不经过 Qt 的焦点处理，导致"测试通过、真机失效"。）

    改用【显式交互】判断：只有用户真正点进来（Tab/点击）才允许滚轮微调。
    做法：把焦点策略从 WheelFocus 降为 StrongFocus（去掉"滚轮可获焦"），
    这样滚轮不再抢焦点，hasFocus() 就恢复成"用户主动聚焦"的可靠信号。

    本实现与 datahub_test/gui_test.py 的 WheelGuard 保持一致，改之前先看那边。
    """

    def _has_user_focus(self, obj):
        """obj 或其内部子控件（如 SpinBox 的 QLineEdit）当前是否持有焦点。

        SpinBox 被点击时，真实焦点在其内部 QLineEdit 上；只判断 obj.hasFocus()
        会把"用户已点进来"误判为未聚焦，导致滚轮微调失效。
        """
        if obj.hasFocus():
            return True
        for child in obj.findChildren(QWidget):
            if child.hasFocus():
                return True
        return False

    def eventFilter(self, obj, ev):
        if ev.type() != QEvent.Type.Wheel:
            return False
        if self._has_user_focus(obj):
            return False          # 用户已聚焦：保留原生滚轮微调
        # 转交给最近的祖先滚动区，让页面正常滚动
        w = obj.parentWidget()
        while w is not None:
            if isinstance(w, QScrollArea):
                QApplication.sendEvent(w.viewport(), ev)
                break
            w = w.parentWidget()
        return True               # 吃掉，阻止改值

    def install(self, *widgets):
        """给一批控件装上。

        关键：必须同时把焦点策略从 WheelFocus 降为 StrongFocus，
        否则滚轮会先抢走焦点、让 hasFocus() 判据失效（见类注释）。
        """
        for w in widgets:
            if w is None:
                continue
            fp = w.focusPolicy()
            if fp == Qt.FocusPolicy.WheelFocus:
                w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            w.installEventFilter(self)
            # SpinBox 内部有 QLineEdit，鼠标真实落点是它；一并装上更保险
            for child in w.findChildren(QWidget):
                if isinstance(child, (QLineEdit, QAbstractSpinBox, QComboBox)):
                    cfp = child.focusPolicy()
                    if cfp == Qt.FocusPolicy.WheelFocus:
                        child.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                    child.installEventFilter(self)


# ==================== 配置读写 ====================
def load_ini():
    cp = configparser.ConfigParser()
    if os.path.exists(CONFIG_PATH):
        cp.read(CONFIG_PATH, encoding="utf-8")
    for sec in ("redis", "strategy", "test", "gui"):
        if not cp.has_section(sec):
            cp.add_section(sec)
    return cp


def save_ini(cp):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            cp.write(f)
    except Exception as e:
        print("[WARN] 保存 config.ini 失败: %s" % e)


def ini_get(cp, sec, key, default=""):
    try:
        return cp.get(sec, key)
    except Exception:
        return default


# ==================== 子进程：一次性命令 ====================
class SshSoakWorker(QThread):
    """在远程 Linux 上跑稳定性测试（连接 → 上传 → 执行 → 下载），实时回传输出。

    为什么稳定性测试要跑在 Linux 上：长时间不受 Windows 休眠/断网影响，
    且与现场环境一致（datahub_test 那边的 .so 也必须在 Linux 上跑）。
    """

    line = Signal(str)
    finished_rc = Signal(int)

    def __init__(self, host, port, user, pwd, remote_dir, jobs,
                 uploads=None, downloads=None, on_line=None, parent=None):
        """jobs = [(label, shell_cmd), ...] 顺序执行；downloads = [cfg, ...] 与之一一对应。"""
        super().__init__(parent)
        self.host = host
        self.port = port
        self.user = user
        self.pwd = pwd
        self.remote_dir = remote_dir
        self.jobs = list(jobs)
        self.uploads = list(uploads or [])
        self.downloads = list(downloads or [])
        self._stop = False
        self._session = None

    def run(self):
        import ssh_runner as SR
        rc_last = 0
        try:
            self._session = SR.SshSession(
                self.host, self.port, self.user, self.pwd, self.remote_dir,
                on_line=self.line.emit)
            self._session.connect()
            if self.uploads:
                self.line.emit("[SSH] 同步脚本与数据（只传比远程新的）...")
                self._session.upload(self.uploads)
            for i, (label, cmd) in enumerate(self.jobs):
                if self._stop:
                    break
                if len(self.jobs) > 1:
                    self.line.emit("─" * 60)
                    self.line.emit("[SSH] (%d/%d) %s" % (i + 1, len(self.jobs), label))
                dl = self.downloads[i] if i < len(self.downloads) else None
                rc_last = self._session.run(cmd, download=dl)
                if rc_last != 0 or self._stop:
                    break
        except Exception as e:
            self.line.emit("[SSH][ERROR] %s" % e)
            rc_last = -1
        finally:
            if self._session:
                self._session.close()
        self.finished_rc.emit(rc_last)

    def stop(self):
        self._stop = True


class Worker(QThread):
    """跑一次性命令（预览/发送/自测），实时回传输出。支持多条命令顺序执行。"""

    line = Signal(str)
    finished_rc = Signal(int)

    def __init__(self, cmds, cwd, extra_env=None, parent=None):
        super().__init__(parent)
        self.cmds = cmds if (cmds and isinstance(cmds[0], list)) else [cmds]
        self.cwd = cwd
        self.extra_env = extra_env or {}
        self._proc = None
        self._stopped = False

    def run(self):
        rc_last = 0
        try:
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env.update(self.extra_env)
            for cmd in self.cmds:
                if self._stopped:
                    break
                self.line.emit("$ " + " ".join(cmd))
                self._proc = subprocess.Popen(
                    cmd, cwd=self.cwd, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace", bufsize=1, env=env)
                for out in self._proc.stdout:
                    self.line.emit(out.rstrip("\n"))
                    if self._stopped:
                        break
                self._proc.wait()
                rc_last = self._proc.returncode
                if rc_last != 0 or self._stopped:
                    break
        except Exception as e:
            self.line.emit("[ERROR] %s" % e)
            rc_last = -1
        self.finished_rc.emit(rc_last)

    def stop(self):
        self._stopped = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass


# ==================== 子进程：常驻服务 ====================
class ServiceProc(QObject):
    """常驻服务（Mock 策略平台 / Mock 数据中台）：可启可停，输出实时回传。"""

    line = Signal(str, str)      # (tag, text)
    state = Signal(str, bool)    # (tag, running)

    def __init__(self, tag, parent=None):
        super().__init__(parent)
        self.tag = tag
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_out)
        self.proc.finished.connect(self._on_finished)
        self._buf = ""
        self.running = False

    def start(self, argv, cwd):
        if self.running:
            return False, "已在运行"
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        self.proc.setProcessEnvironment(env)
        self.proc.setWorkingDirectory(cwd)
        self.line.emit(self.tag, "$ " + " ".join(argv))
        self.proc.start(argv[0], argv[1:])
        if not self.proc.waitForStarted(8000):
            self.line.emit(self.tag, "[ERROR] 启动失败: %s" % self.proc.errorString())
            return False, self.proc.errorString()
        self.running = True
        self.state.emit(self.tag, True)
        return True, ""

    def _on_out(self):
        raw = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        self._buf += raw
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.line.emit(self.tag, line.rstrip("\r"))

    def _on_finished(self, code, status):
        if self._buf.strip():
            self.line.emit(self.tag, self._buf.rstrip())
            self._buf = ""
        self.running = False
        self.line.emit(self.tag, "[退出] code=%s" % code)
        self.state.emit(self.tag, False)

    def stop(self):
        if not self.running:
            return
        self.line.emit(self.tag, "[停止] 正在终止 ...")
        self.proc.terminate()
        if not self.proc.waitForFinished(3000):
            self.proc.kill()
            self.proc.waitForFinished(2000)


# ==================== 统计汇总加载 ====================
class SummaryLoader(QThread):
    """扫描 out/performance/*_stats.json，汇总成表格行。"""

    loaded = Signal(list, int, str)

    def __init__(self, perf_dir, since=0.0, parent=None):
        super().__init__(parent)
        self.perf_dir = perf_dir
        self.since = since

    def run(self):
        rows, err = [], ""
        try:
            if not os.path.isdir(self.perf_dir):
                self.loaded.emit([], 0, "目录不存在: %s" % self.perf_dir)
                return
            files = [f for f in os.listdir(self.perf_dir) if f.endswith("_stats.json")]
            files.sort(key=lambda f: os.path.getmtime(os.path.join(self.perf_dir, f)),
                       reverse=True)
            n = 0
            for f in files:
                p = os.path.join(self.perf_dir, f)
                if self.since and os.path.getmtime(p) < self.since:
                    continue
                try:
                    with open(p, encoding="utf-8") as fh:
                        d = json.load(fh)
                except Exception:
                    continue
                cpu = d.get("cpu") or {}
                rows.append({
                    "file": f,
                    "label": d.get("label") or f[:-11],
                    "time": time.strftime("%m-%d %H:%M:%S",
                                          time.localtime(d.get("end") or 0)),
                    "duration": d.get("duration", 0) or 0,
                    "sent": d.get("sent", 0),
                    "reply": d.get("reply", 0),
                    "send_fail": d.get("send_fail", 0),
                    "timeout": d.get("timeout_reply", 0),
                    "send_per_sec": d.get("send_per_sec", 0) or 0,
                    "reply_per_sec": d.get("reply_per_sec", 0) or 0,
                    "sent_mb": (d.get("sent_bytes", 0) or 0) / 1048576.0,
                    "reply_mb": (d.get("reply_bytes", 0) or 0) / 1048576.0,
                    "lat_avg_us": d.get("lat_avg_us", 0) or 0,
                    "lat_p95_ms": d.get("lat_p95_ms", 0) or 0,
                    "lat_p99_ms": d.get("lat_p99_ms", 0) or 0,
                    "lat_max_ms": d.get("lat_max_ms", 0) or 0,
                    "cpu": cpu.get("proc_percent", 0) or 0,
                    "n_err": len(d.get("errors") or {}),
                    "raw": d,
                })
                n += 1
            self.loaded.emit(rows, n, err)
        except Exception as e:
            self.loaded.emit([], 0, "%s" % e)


# ==================== 可折叠分组 ====================
class CollapsibleBox(QWidget):
    """标题条 + 内容，点击标题可收起（样式沿用 datahub_test 的 QToolBox 风格）"""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.btn = QToolButton()
        self.btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn.setArrowType(Qt.ArrowType.DownArrow)
        self.btn.setText(title)
        self.btn.setCheckable(True)
        self.btn.setChecked(True)
        self.btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        f = QFont("Microsoft YaHei", 9)
        f.setBold(True)
        self.btn.setFont(f)
        self.btn.setStyleSheet(
            "QToolButton { background: #eef3fb; border: 1px solid #d0d8e4;"
            " border-radius: 3px; padding: 5px 10px; font-weight: bold;"
            " text-align: left; }"
            "QToolButton:hover { background: #cfe0f8; }")
        self.content = QWidget()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(2)
        lay.addWidget(self.btn)
        lay.addWidget(self.content)
        self.btn.clicked.connect(self._on_toggled)

    def _on_toggled(self, checked):
        self.btn.setArrowType(Qt.ArrowType.DownArrow if checked
                              else Qt.ArrowType.RightArrow)
        self.content.setVisible(checked)

    def setExpanded(self, expanded):
        self.btn.setChecked(expanded)
        self._on_toggled(expanded)

    def isExpanded(self):
        return self.btn.isChecked()


# ==================== 主窗口 ====================
class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("策略平台测试工具 (数据中台 → 策略平台)")
        self.resize(*self._initial_size())
        self.cp = load_ini()
        self.worker = None
        self._soak_running = False      # soak 是否在跑（决定「运行稳定性测试」可点性）
        self.summary_loader = None
        self._rows = []
        self._batch_start = 0.0
        self.guard = WheelGuard(self)
        self.svc_strategy = ServiceProc("策略平台", self)
        self.svc_datahub = ServiceProc("数据中台", self)

        self._build_ui()
        self._wire_services()
        self._restore_ui_state()
        self._update_conn_summary()

    # ---------------- 尺寸 / 状态记忆 ----------------
    def _initial_size(self):
        try:
            scr = QApplication.primaryScreen().availableGeometry()
            return (min(1500, int(scr.width() * 0.92)),
                    min(950, int(scr.height() * 0.92)))
        except Exception:
            return (1480, 920)

    def _restore_ui_state(self):
        h = ini_get(self.cp, "gui", "hsplit", "")
        v = ini_get(self.cp, "gui", "vsplit", "")
        for key, sp, default in (("hsplit", self.hsplit, (900, 520)),
                                 ("vsplit", self.vsplit, (620, 300))):
            raw = h if key == "hsplit" else v
            try:
                parts = [int(x) for x in raw.split(",")] if raw else []
                if len(parts) == 2 and sum(parts) > 0:
                    sp.setSizes(parts)
                else:
                    sp.setSizes(list(default))
            except Exception:
                sp.setSizes(list(default))

    def _save_conn_to_ini(self):
        """把当前界面上的连接/身份/参数写回 config.ini（下次打开还在）。"""
        try:
            self.cp.set("redis", "host", self.edit_host.text().strip())
            self.cp.set("redis", "port", str(self.spin_port.value()))
            self.cp.set("redis", "pwd", self.edit_pwd.text())
            self.cp.set("redis", "db", str(self.spin_db.value()))
            self.cp.set("strategy", "assign_id", str(self.spin_assign.value()))
            self.cp.set("strategy", "unique_rand", self.edit_uniq_rand.text().strip())
            self.cp.set("strategy", "unique_name", self.edit_uniq_name.text().strip())
            self.cp.set("strategy", "mac", self.edit_mac.text().strip())
            self.cp.set("strategy", "usecount", str(self.spin_usecount.value()))
            self.cp.set("test", "workers", str(self.spin_workers.value()))
            self.cp.set("test", "max", str(self.spin_max.value()))
            self.cp.set("test", "rate", str(self.spin_rate.value()))
            self.cp.set("test", "wait", str(self.spin_wait.value()))
            self.cp.set("test", "interface",
                        ",".join(self.selected_interfaces()) or "create")
            self.cp.set("test", "type", self.combo_type.currentText())
            if not self.cp.has_section("gui"):
                self.cp.add_section("gui")
            self.cp.set("gui", "stats_out", self.edit_stats_out.text().strip())
            # Mock 服务选项（纯偏好，无风险）
            if hasattr(self, "chk_svc_refecho"):
                self.cp.set("gui", "svc_refecho",
                            "1" if self.chk_svc_refecho.isChecked() else "0")
            # 远程 Linux（稳定性测试跑在远端）
            if hasattr(self, "chk_remote"):
                self.cp.set("gui", "remote",
                            "1" if self.chk_remote.isChecked() else "0")
                self.cp.set("gui", "ssh_host", self.edit_ssh_host.text().strip())
                self.cp.set("gui", "ssh_port", str(self.spin_ssh_port.value()))
                self.cp.set("gui", "ssh_user", self.edit_ssh_user.text().strip())
                self.cp.set("gui", "ssh_pass", self.edit_ssh_pass.text())
                self.cp.set("gui", "ssh_dir", self.edit_ssh_dir.text().strip())
                self.cp.set("gui", "ssh_nohup",
                            "1" if self.chk_ssh_nohup.isChecked() else "0")
            # 「每次发送前自动清空日志」记住用户的选择（纯偏好，无风险）
            self.cp.set("gui", "autoclear_log",
                        "1" if self.chk_autoclear.isChecked() else "0")
            # 生成压测数据的参数（只是默认值，误填也不会自动发送）
            self.cp.set("gui", "bulk_accounts", str(self.spin_bulk_accounts.value()))
            self.cp.set("gui", "bulk_start", str(self.spin_bulk_start.value()))
            self.cp.set("gui", "ref_map", self.edit_ref_map.text().strip())
            # 行情代码（每天都在换，记住上次填的，省得天天重填）
            self.cp.set("gui", "contract_code",
                        self.edit_contract_code.text().strip())
            self.cp.set("gui", "target_stock_code",
                        self.edit_target_code.text().strip())
            # 稳定性测试参数（纯偏好，不点「运行」不会发任何数据）
            if hasattr(self, "chk_soak"):
                self.cp.set("gui", "soak",
                            "1" if self.chk_soak.isChecked() else "0")
                self.cp.set("gui", "soak_flow",
                            "1" if self.chk_soak_flow.isChecked() else "0")
                self.cp.set("gui", "soak_mode",
                            self.combo_soak_mode.currentData() or "rounds")
                self.cp.set("gui", "soak_rounds", str(self.spin_soak_rounds.value()))
                self.cp.set("gui", "soak_hours", str(self.spin_soak_hours.value()))
                self.cp.set("gui", "soak_batch", str(self.spin_soak_batch.value()))
                self.cp.set("gui", "soak_gap", str(self.spin_soak_gap.value()))
                self.cp.set("gui", "soak_midgap_on",
                            "1" if self.chk_soak_midgap.isChecked() else "0")
                self.cp.set("gui", "soak_midgap_sec",
                            "%g" % self.spin_soak_midgap.value())
                self.cp.set("gui", "soak_clean",
                            self.combo_soak_clean.currentData() or "monitor")
                self.cp.set("gui", "soak_rotate",
                            "1" if self.chk_soak_rotate.isChecked() else "0")
                self.cp.set("gui", "soak_keep",
                            "1" if self.chk_soak_keep.isChecked() else "0")
                self.cp.set("gui", "soak_maxlag", str(self.spin_soak_maxlag.value()))
                self.cp.set("gui", "soak_maxpend", str(self.spin_soak_maxpend.value()))
                self.cp.set("gui", "soak_maxtout", str(self.spin_soak_maxtout.value()))
                self.cp.set("gui", "soak_maxout", str(self.spin_soak_maxout.value()))
                self.cp.set("gui", "soak_maxbiz", str(self.spin_soak_maxbiz.value()))
                self.cp.set("gui", "soak_minreply",
                            "%g" % self.spin_soak_minreply.value())
        except Exception:
            pass

    def _save_ui_state(self):
        try:
            for key, sp in (("hsplit", self.hsplit), ("vsplit", self.vsplit)):
                self.cp.set("gui", key, ",".join(str(x) for x in sp.sizes()))
            self._save_conn_to_ini()
            save_ini(self.cp)
        except Exception:
            pass

    # ---------------- 界面 ----------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        title = QLabel("策略平台测试工具 · 数据中台 → 策略平台")
        tf = QFont("Microsoft YaHei", 13)
        tf.setBold(True)
        title.setFont(tf)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(title)

        self.lbl_conn = QLabel("")
        self.lbl_conn.setStyleSheet("color: #666;")
        self.lbl_conn.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_conn.setToolTip("当前生效的 Redis 连接，可在右栏「连接设置」修改")
        root.addWidget(self.lbl_conn)

        self.vsplit = QSplitter(Qt.Orientation.Vertical)
        self.vsplit.setChildrenCollapsible(False)
        self.hsplit = QSplitter(Qt.Orientation.Horizontal)
        self.hsplit.setChildrenCollapsible(False)
        self.vsplit.addWidget(self.hsplit)

        self.hsplit.addWidget(self._build_left())
        self.hsplit.addWidget(self._build_right())

        self.vsplit.addWidget(self._build_bottom())
        root.addWidget(self.vsplit, 1)

        root.addLayout(self._build_actions())

    # ---- 左栏：只管「发什么」----
    def _build_left(self):
        """左栏 = 1 测试数据（造/选数据）→ 2 发送参数（怎么发，含 4 个分区）。

        布局参考 datahub_test/gui_test.py：连接/身份/输出/服务都搬到右栏，
        让左栏专注「要发什么」，从上到下一条主流程走完。

        ★ 「用例类型」/「指定用例」放在「2. 发送参数 → 发送范围」而【不是】
          「1. 测试数据」：它们是 send_test 的 --type/--cases，属于"这一批
          怎么筛"，不改任何数据。datahub_test 也是这么摆的（g2 里）。
        """
        outer = QWidget()
        ol = QVBoxLayout(outer)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(4)

        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(6)

        lay.addWidget(self._box_data())
        lay.addWidget(self._box_send())
        lay.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)
        ol.addWidget(scroll, 1)
        self.left_scroll = scroll
        return outer

    def _box_conn(self):
        box = CollapsibleBox("连接设置 (Redis)")
        g = QGridLayout(box.content)
        self.edit_host = QLineEdit(ini_get(self.cp, "redis", "host", "192.168.1.137"))
        self.spin_port = QSpinBox()
        self.spin_port.setRange(1, 65535)
        self.spin_port.setValue(int(ini_get(self.cp, "redis", "port", "6379") or 6379))
        self.spin_port.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.edit_pwd = QLineEdit(ini_get(self.cp, "redis", "pwd", "QianLong@2026&"))
        self.edit_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.spin_db = QSpinBox()
        self.spin_db.setRange(0, 15)
        self.spin_db.setValue(int(ini_get(self.cp, "redis", "db", "0") or 0))
        self.spin_db.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

        g.addWidget(QLabel("主机"), 0, 0)
        g.addWidget(self.edit_host, 0, 1, 1, 3)
        g.addWidget(QLabel("端口"), 1, 0)
        g.addWidget(self.spin_port, 1, 1)
        g.addWidget(QLabel("库号 db"), 1, 2)
        g.addWidget(self.spin_db, 1, 3)
        g.addWidget(QLabel("密码"), 2, 0)
        g.addWidget(self.edit_pwd, 2, 1, 1, 3)

        note = QLabel("⚠ 默认打 137（DataHub.ini 指向的那台）。136 有同事在测别的，"
                      "别把 host 改成 136。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#a15c00; background:#fff8e6;"
                           " border:1px solid #f0d9a0; border-radius:3px; padding:4px;")
        g.addWidget(note, 3, 0, 1, 4)

        for w in (self.spin_port, self.spin_db):
            self.guard.install(w)
        for w in (self.edit_host, self.edit_pwd, self.spin_port, self.spin_db):
            try:
                w.editingFinished.connect(self._update_conn_summary)
            except Exception:
                pass
        return box

    def _box_identity(self):
        box = CollapsibleBox("策略平台身份 (决定下发流 ST-<编号>)")
        g = QGridLayout(box.content)
        self.spin_assign = QSpinBox()
        self.spin_assign.setRange(0, 100000)
        self.spin_assign.setValue(int(ini_get(self.cp, "strategy", "assign_id", "1") or 1))
        self.spin_assign.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_assign.setToolTip(
            "数据中台分配给策略平台的编号，决定下发流名 ST-<编号>。\n"
            "发送器和 Mock 用的是同一个编号，保证「发的」和「收的」是同一条流。")

        self.edit_uniq_rand = QLineEdit(ini_get(self.cp, "strategy", "unique_rand", "761"))
        self.edit_uniq_name = QLineEdit(ini_get(self.cp, "strategy", "unique_name", "test"))
        self.edit_uniq_rand.setMaximumWidth(90)
        self.edit_uniq_name.setMaximumWidth(90)
        self.edit_mac = QLineEdit(ini_get(self.cp, "strategy", "mac", "2cea7fd9d5c0"))
        self.spin_usecount = QSpinBox()
        self.spin_usecount.setRange(0, 99999)
        self.spin_usecount.setValue(int(ini_get(self.cp, "strategy", "usecount", "1") or 1))
        self.spin_usecount.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_usecount.setMaximumWidth(90)

        self.combo_suffix = QComboBox()
        self.combo_suffix.addItems(["（不带后缀，插件用的）", "_1（现场真中台用的）"])
        self.combo_suffix.setToolTip(
            "现场存在两套频道名：\n"
            "  不带后缀 strategyserver_online   ← 插件(.so) 发这套\n"
            "  带 _1    strategyserver_online_1 ← 现场真中台订这套\n"
            "用 PUBSUB NUMSUB 实测：不带后缀 0 个订阅者，_1 有 1 个。")

        g.addWidget(QLabel("分配编号"), 0, 0)
        g.addWidget(self.spin_assign, 0, 1)
        hint = QLabel("→ 发送与接收都用这条流")
        hint.setStyleSheet("color:#0a5;")
        g.addWidget(hint, 0, 2, 1, 2)
        g.addWidget(QLabel("unique 随机段"), 1, 0)
        g.addWidget(self.edit_uniq_rand, 1, 1)
        g.addWidget(QLabel("名字段"), 1, 2)
        g.addWidget(self.edit_uniq_name, 1, 3)
        g.addWidget(QLabel("mac"), 2, 0)
        g.addWidget(self.edit_mac, 2, 1)
        g.addWidget(QLabel("usecount"), 2, 2)
        g.addWidget(self.spin_usecount, 2, 3)
        g.addWidget(QLabel("频道后缀"), 3, 0)
        g.addWidget(self.combo_suffix, 3, 1, 1, 3)

        self.lbl_stream = QLabel("")
        self.lbl_stream.setStyleSheet("color:#0a5; font-weight:bold;")
        g.addWidget(self.lbl_stream, 4, 0, 1, 4)

        self.guard.install(self.spin_assign, self.spin_usecount, self.combo_suffix)
        self.spin_assign.valueChanged.connect(self._update_conn_summary)
        return box

    def _box_data(self):
        box = CollapsibleBox("1. 测试数据 (Excel: data/*.xlsx)")
        box.setExpanded(True)
        g = QGridLayout(box.content)
        names = CASES.list_interfaces() if CASES else \
            ["create", "modify", "remove", "pwdUpdate", "account"]
        self.chk_ifaces = {}
        for i, n in enumerate(names):
            chk = QCheckBox(n)
            chk.setChecked(n == "create")
            # 【不做读表统计】勾选只更新一行文字提示，不碰 Excel（见 _update_case_count）
            chk.toggled.connect(self._update_case_count)
            self.chk_ifaces[n] = chk
            g.addWidget(chk, i // 3, i % 3)

        # 业务流模式下接口勾选不参与（接口固定），给一行说明。
        # 行号放在所有接口勾选框【之下】：勾选框占 0..(len-1)//3 行。
        self.lbl_ifaces_note = QLabel("")
        self.lbl_ifaces_note.setWordWrap(True)
        self.lbl_ifaces_note.setStyleSheet(
            "color:#a15c00; background:#fff8e6;"
            " border:1px solid #f0d9a0; border-radius:3px; padding:3px;")
        self.lbl_ifaces_note.setVisible(False)
        note_row = (len(names) - 1) // 3 + 1
        g.addWidget(self.lbl_ifaces_note, note_row, 0, 1, 3)

        # ---------------- 生成压测数据（--bulk-normal / --ref-map）----------------
        # 默认每个接口 normal 只有 2~5 条，压测会循环复用同一批报文；
        # 这里按 datahub_test 的做法：批量账号 + 用 create 落盘的真实单号回填。
        #
        # ★ 「用例类型」/「指定用例」原在本面板，已移到「2. 发送参数」的
        #   「发送范围」分区 —— 它们是【发送时的筛选器】（send_test 的
        #   --type / --cases），不是数据本身；本面板只留"造/选数据"的东西。
        # ★ 行号要从「接口勾选 + 业务流说明」之下开始：
        #   勾选框占 0..note_row-1，说明行在 note_row。
        row_gen = note_row + 1
        self.spin_bulk_accounts = QSpinBox()
        self.spin_bulk_accounts.setRange(0, 1000000)
        self.spin_bulk_accounts.setValue(int(ini_get(self.cp, "gui", "bulk_accounts", "0") or 0))
        self.spin_bulk_accounts.setMaximumWidth(90)
        self.spin_bulk_accounts.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_bulk_accounts.setToolTip(
            "为勾选的接口生成 N 行正常数据、每行一个不同账号（压测不循环）。\n"
            "modify/remove 的第 i 行会引用单号，需配合下面的「Ref回填文件」才是真实单号。\n"
            "0 = 不启用（保持原来的小表）。")
        self.guard.install(self.spin_bulk_accounts)

        self.spin_bulk_start = QSpinBox()
        self.spin_bulk_start.setRange(0, 999999)
        self.spin_bulk_start.setValue(int(ini_get(self.cp, "gui", "bulk_start", "0") or 0))
        self.spin_bulk_start.setMaximumWidth(90)
        self.spin_bulk_start.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_bulk_start.setToolTip(
            "账号 6 位序号起点。0 = 接口默认（11301，紧邻真实账号 010100011300 之后）。\n"
            "★ 用 Ref 回填时必须保持 0：回填是拿「账号」去 refs.json 查单号的，\n"
            "  改了起点会和 create 的账号序列错位，导致全部查不到。")
        self.guard.install(self.spin_bulk_start)

        self.edit_ref_map = QLineEdit(ini_get(self.cp, "gui", "ref_map", ""))
        self.edit_ref_map.setPlaceholderText("Ref回填文件 *_refs.json（发 create 后自动落盘，可选）")
        self.edit_ref_map.setToolTip(
            "选 send_test 发 create 时自动落盘的 refs JSON（out/performance/*_refs.json）。\n"
            "生成 modify/remove 时按【账号】把 __REF token 换成平台真实返回的单号 ——\n"
            "比靠行序猜可靠得多，不依赖 create 是否按行序全部成功。\n\n"
            "【前置】必须先真的发过一次 create（勾选 create 点「开始发送」），\n"
            "        日志里会出现「★ 抓到 N 个条件单号，已写入: ...」，把那个文件选上。\n\n"
            "【注意】填了它，「批量账号数」必须等于 create 造单的数量，且「起始序号」保持 0；\n"
            "        否则按账号查不到，会打印 ERROR。")
        self.edit_ref_map.textChanged.connect(self._update_case_count)
        btn_pick_map = QPushButton("浏览…")
        btn_pick_map.setToolTip("选择 create 落盘的 *_refs.json")
        btn_pick_map.clicked.connect(self._pick_ref_map)

        self.btn_gen = QPushButton("生成压测数据")
        self.btn_gen.setToolTip(
            "对勾选的接口执行 make_excel.py：\n"
            "  批量账号数 > 0  -> 加 --bulk-normal N（替换 normal 段为 N 行不同账号）\n"
            "  选了 Ref文件    -> 对 modify/remove 加 --ref-map（回填真实单号）\n"
            "只重写 data/*.xlsx，不会发送任何数据。")
        self.btn_gen.clicked.connect(self.on_generate)
        self.btn_open_data = QPushButton("打开 data 目录")
        self.btn_open_data.clicked.connect(self._open_data_dir)

        g.addWidget(QLabel("批量账号数"), row_gen, 0)
        g.addWidget(self.spin_bulk_accounts, row_gen, 1)
        g.addWidget(self.btn_gen, row_gen, 2)
        g.addWidget(self.btn_open_data, row_gen, 3)
        g.addWidget(QLabel("账号起始"), row_gen + 1, 0)
        g.addWidget(self.spin_bulk_start, row_gen + 1, 1)
        g.addWidget(QLabel("Ref回填"), row_gen + 2, 0)
        g.addWidget(self.edit_ref_map, row_gen + 2, 1, 1, 2)
        g.addWidget(btn_pick_map, row_gen + 2, 3)

        # ---- 行情代码（每天都在换，做成可填，免得天天改源码）----
        self.edit_contract_code = QLineEdit(
            ini_get(self.cp, "gui", "contract_code", ""))
        self.edit_contract_code.setPlaceholderText("90008169")
        self.edit_contract_code.setMaximumWidth(140)
        self.edit_contract_code.setToolTip(
            "行情【合约代码】。留空 = 用 interfaces/_common.py 里的默认值。\n\n"
            "★ 改完必须点「生成压测数据」重新生成表才生效 ——\n"
            "  表里存的是字面量，不是运行时 token。\n"
            "  生成后会打印一行 [OK] 行情代码：合约=... 便于核对。\n\n"
            "写入的字段：\n"
            "  create/modify 的 Entrust.ContractCode、\n"
            "  CfgExceedPrice.StockCode、\n"
            "  CondPrice / CondPercent / CondTime .ContractCode\n\n"
            "⚠ modify 还会用它覆盖 CondLoss / CondProfit .ContractCode；\n"
            "  但 create 里这两组是【另一个代码 10011743】，不受本框影响。")
        self.guard.install(self.edit_contract_code)

        self.edit_target_code = QLineEdit(
            ini_get(self.cp, "gui", "target_stock_code", ""))
        self.edit_target_code.setPlaceholderText("159901")
        self.edit_target_code.setMaximumWidth(140)
        self.edit_target_code.setToolTip(
            "止盈止损【标的代码】，写进 CondTargetLoss / CondTargetProfit 的 StockCode。\n"
            "留空 = 用 _common.py 里的默认值。同样需要重新生成表才生效。\n\n"
            "⚠ 只对 modify 生效：create 里这两组是【510050】，不受本框影响。")
        self.guard.install(self.edit_target_code)

        g.addWidget(QLabel("合约代码"), row_gen + 3, 0)
        g.addWidget(self.edit_contract_code, row_gen + 3, 1)
        g.addWidget(QLabel("止盈止损标的"), row_gen + 3, 2)
        g.addWidget(self.edit_target_code, row_gen + 3, 3)

        self.lbl_gen = QLabel("")
        self.lbl_gen.setWordWrap(True)
        self.lbl_gen.setStyleSheet("color:#666;")
        g.addWidget(self.lbl_gen, row_gen + 4, 0, 1, 4)

        return box

    def _pick_ref_map(self):
        """浏览选择 create 落盘的 refs.json。"""
        cur = self.edit_ref_map.text().strip() or os.path.join(BASE_DIR, "out", "performance")
        d, _ = QFileDialog.getOpenFileName(
            self, "选择 create 回填 JSON (*_refs.json)", cur, "JSON (*.json)")
        if d:
            self.edit_ref_map.setText(d)

    def _open_data_dir(self):
        d = os.path.join(BASE_DIR, "data")
        try:
            os.makedirs(d, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(d)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", d])
            else:
                subprocess.Popen(["xdg-open", d])
        except Exception as e:
            QMessageBox.warning(self, "提示", "打不开目录：%s" % e)

    def on_generate(self):
        """按界面参数生成 data/*.xlsx（只写文件，不发送）。"""
        names = self.selected_interfaces()
        if not names:
            QMessageBox.warning(self, "提示", "请先勾选至少一个接口")
            return
        bulk_n = self.spin_bulk_accounts.value()
        ref_map = self.edit_ref_map.text().strip()
        ref_ifaces = [n for n in names if n in ("modify", "remove")]

        # ---- 生成前的一致性校验（照 datahub_test：宁可先问，也别生成无效数据）----
        if ref_map and not bulk_n:
            QMessageBox.warning(
                self, "参数不匹配",
                "填了「Ref回填」但「批量账号数」是 0。\n\n"
                "Ref 回填要按【账号】去 refs.json 查单号，必须先勾「批量账号数」"
                "生成多账号行（数量要与 create 造单数量一致）。")
            return
        if ref_map and not ref_ifaces:
            self.append_log("[提示] Ref回填只对 modify/remove 生效，"
                            "当前勾选的接口用不到，将忽略。")
        if ref_map and ref_ifaces and self.spin_bulk_start.value() != 0:
            if not self._ask("可能生成无效数据",
                             "「账号起始序号」是 %d（非 0）。\n\n"
                             "回填是按【账号】查单号的，改了起点会和 create 的账号序列"
                             "错位，导致一行都查不到、Ref 仍是 __REF token。\n\n"
                             "确定继续吗？" % self.spin_bulk_start.value()):
                return
        if not bulk_n and not (ref_map and ref_ifaces):
            if not self._ask("确认",
                             "「批量账号数」是 0，也没有可用的 Ref 回填。\n\n"
                             "这样生成出来还是原来的小表（每个接口 normal 只有 2~5 条），"
                             "压测时会被循环复用。\n\n仍要继续吗？"):
                return

        cmds = []
        codes = []
        if self.edit_contract_code.text().strip():
            codes += ["--contract-code", self.edit_contract_code.text().strip()]
        if self.edit_target_code.text().strip():
            codes += ["--target-stock-code", self.edit_target_code.text().strip()]
        for n in names:
            cmd = self._base_cmd("make_excel.py") + ["--interface", n]
            if bulk_n:
                cmd += ["--bulk-normal", str(bulk_n),
                        "--bulk-start", str(self.spin_bulk_start.value())]
                if ref_map and n in ("modify", "remove"):
                    cmd += ["--ref-map", ref_map]
            cmd += codes
            cmds.append(cmd)

        self.append_log("")
        self.append_log("#" * 60)
        self.append_log("# 生成压测数据（只写 data/*.xlsx，不发送）")
        self.append_log("#   接口=%s  批量账号=%s  Ref回填=%s"
                        % (",".join(names), bulk_n or "关",
                           os.path.basename(ref_map) if ref_map else "关"))
        if codes:
            self.append_log("#   行情代码：合约=%s  止盈止损标的=%s"
                            % (self.edit_contract_code.text().strip() or "（默认）",
                               self.edit_target_code.text().strip() or "（默认）"))
        self.append_log("#" * 60)
        self._run(cmds, on_done=lambda rc: self._after_generate(rc, names))

    def _after_generate(self, rc, names):
        """生成完刷新提示与用例计数。"""
        try:
            parts = []
            for n in names:
                p = CASES.default_excel(n) if CASES else ""
                if p and os.path.exists(p):
                    parts.append("%s %.1fMB" % (n, os.path.getsize(p) / 1e6))
            self.lbl_gen.setText("已生成：%s（点「开始发送」才会真的发）"
                                 % "，".join(parts) if parts else "生成结束")
        except Exception:
            self.lbl_gen.setText("生成结束（退出码 %s）" % rc)
        self._update_case_count()

    def _box_send(self):
        """2. 发送参数 —— 含「发送范围 / 规模与速率 / 回复处理 / 稳定性测试」四个分区。

        分区标题用下划线样式分隔（同 datahub_test），视觉上把参数分组，
        避免一长串控件看不出层次。
        """
        box = CollapsibleBox("2. 发送参数")
        box.setExpanded(True)

        def section(text):
            """分区标题：跨 4 列，带下划线分隔。"""
            lbl = QLabel(text)
            lbl.setStyleSheet("color:#3a5a8c; font-weight:bold; padding-top:8px;"
                              " border-bottom:1px solid #d8dee8;")
            return lbl

        def spin(lo, hi, val, w=100, tip="", dbl=False):
            s = QDoubleSpinBox() if dbl else QSpinBox()
            s.setRange(lo, hi)
            s.setValue(val)
            s.setMaximumWidth(w)
            s.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            if dbl:
                s.setDecimals(2)
            if tip:
                s.setToolTip(tip)
            self.guard.install(s)
            return s

        def flabel(zh, flag, tip=""):
            """中文名 + 灰色 CLI 参数名，方便界面和命令行互相对照。"""
            l = QLabel('%s <span style="color:#888;">%s</span>' % (zh, flag))
            l.setToolTip(tip or ("命令行对应参数：<b>%s</b>" % flag))
            return l

        cp = self.cp
        self.spin_workers = spin(1, 256, int(ini_get(cp, "test", "workers", "4") or 4), 90,
                                 "并发发送线程数")
        self.spin_max = spin(0, 100000000, int(ini_get(cp, "test", "max", "0") or 0), 110,
                             "最多处理多少条（在「用例类型」筛选之后计算）。\n"
                             "★ 0 = 全部：把当前筛选出的用例各发一次。\n"
                             "   例：类型选 destroy、account -> 发 96 条就结束。\n"
                             "小于用例数 = 只发前 N 条；\n"
                             "大于用例数 = 循环复用用例凑够 N 条（压测要量大时用）。")
        self.spin_seconds = spin(0, 86400, 0, 90,
                                 "按时间跑：跑够这么多秒就停（优先于总条数）。0=不用")
        self.spin_rate = spin(0, 1000000, int(ini_get(cp, "test", "rate", "0") or 0), 100,
                              "全局限速 条/秒。0=不限速")
        self.spin_wait = spin(0, 3600, int(float(ini_get(cp, "test", "wait", "5") or 5)), 90,
                              "发完等回包的秒数（收齐或稳定后提前结束）")
        # 默认 0（关闭）：它是「额外真实报文」，默认开着 = 默认多发给真平台 20 条；
        # 而且没回包时每条干等 5 秒（填 20 可能白等 100 秒），很容易被当成卡死。
        # 需要真实 RTT 的人会主动填。
        self.spin_sync = spin(0, 10000, 0, 90,
                              "压测后单发单收 N 次，测链路真实 RTT（对比批量口径）。\n"
                              "★ 这 N 条是【额外真实写入 ST-N 的报文】，计入该流 XLEN\n"
                              "  但【不计入】上面的发送/回包统计，流里条数 = 总条数 + N。\n"
                              "★ 收不到回包时每条要干等 5 秒（填 20 = 最多白等 100 秒）。\n"
                              "默认 0 = 关闭。要测真实 RTT 再填，建议先填 1~3。")
        self.edit_reply_stream = QLineEdit("")
        self.edit_reply_stream.setPlaceholderText("留空=DataHub_reply_stream")
        self.chk_no_reply = QCheckBox("只发不收（--no-reply）")
        self.chk_no_reply.setToolTip("勾上=不读回包，纯发压测。命令行对应：<b>--no-reply</b>")
        self.chk_quiet = QCheckBox("安静模式（--quiet 1）")
        self.chk_quiet.setToolTip("少打印。只影响日志器的常规输出，"
                                  "【不影响「预览报文」】，也不会让报文消失。\n"
                                  "命令行对应：<b>--quiet 1</b>（默认）/ 0=啰嗦")
        self.chk_quiet.setChecked(True)
        # 打真平台：默认关。目标流上存在外来消费者（真平台）时，send_test.py 会被
        # 安全闸拦下（退出码 2）；勾上这个才会加 --force-live 放行。
        self.chk_force_live = QCheckBox("允许打真平台（--force-live，跳过安全闸）")
        self.chk_force_live.setToolTip(
            "目标流上已有【真实策略平台】的消费者时，安全闸会拦住发送。\n"
            "勾上这个 = 明知是真平台仍要发（给真平台下发假条件单，可能触发真实交易）。\n"
            "仅在明确要测真平台接口时使用；发完记得取消勾选。")
        self.chk_force_live.setStyleSheet("color:#b00;")
        # 【故意不持久化】每次打开都从"关"开始。这个开关一旦记住，
        # 下次顺手点发送就可能直接打到真平台，风险太大。
        self.chk_force_live.setChecked(False)

        g = QGridLayout(box.content)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        g.setVerticalSpacing(6)

        # ==================== A. 发送范围 ====================
        # 「用例类型」(--type) 和「指定用例」(--cases) 是【发送时的筛选器】，
        # 不是数据本身 —— 所以放在这里，与 datahub_test 的 g2 一致。
        # 上面「1. 测试数据」只管"造/选哪些 Excel 数据"。
        r = 0
        g.addWidget(section("发送范围"), r, 0, 1, 4)
        r += 1

        self.combo_type = QComboBox()
        self.combo_type.addItems(["normal", "error", "destroy", "all"])
        self.combo_type.currentTextChanged.connect(self._update_case_count)
        self.combo_type.setToolTip(
            "按用例类型筛选（命令行 --type）。\n"
            "normal=合法报文（压测基线）；error=业务层非法；\n"
            "destroy=畸形/极端报文；all=不过滤。\n\n"
            "★ 它同时决定「稳定性测试」里回复率的默认档位：\n"
            "  normal 默认要求 ≥99%，error/destroy/all 默认不判。")

        self.edit_cases = QLineEdit("")
        self.edit_cases.setPlaceholderText("留空=全部；如 C201,C203-C210 或 5-20（行号）")
        self.edit_cases.setToolTip(
            "按用例编号筛选，支持区间。分隔符可用 , ; 和空格，可混用。\n"
            "  带字母 = 按「用例编号」列匹配（忽略大小写与前导零）：C201 / CB00001\n"
            "  纯数字 = 按 Excel 数据行号（1 起始）：5 / 5-20\n"
            "编号前缀=接口首字母：C=create M=modify R=remove P=pwdUpdate A=account\n\n"
            "★ 写错会直接报错并停止发送，不会退化成「发全表」。\n"
            "命令行下若没显式给 --type，写这里会自动把类型放宽为 all，\n"
            "否则 --cases C201（destroy 用例）会被 normal 过滤成 0 条。")
        # 改筛选条件也要刷新提示（只是拼字符串，不读表）
        self.edit_cases.textChanged.connect(self._update_case_count)

        g.addWidget(flabel("用例类型", "--type"), r, 0)
        g.addWidget(self.combo_type, r, 1)
        g.addWidget(flabel("指定用例", "--cases"), r, 2)
        g.addWidget(self.edit_cases, r, 3)
        self.guard.install(self.combo_type)     # 防"滚轮划过即改值"
        r += 1

        # 提示行跟着搬过来：它汇总的正是「接口 + 类型 + 指定用例」这个筛选结果
        self.lbl_cases = QLabel("")
        self.lbl_cases.setStyleSheet("color:#555;")
        self.lbl_cases.setWordWrap(True)
        g.addWidget(self.lbl_cases, r, 0, 1, 4)
        r += 1

        # ==================== B. 规模与速率 ====================
        g.addWidget(section("规模与速率"), r, 0, 1, 4)
        r += 1
        g.addWidget(flabel("并发线程", "--workers", "并发发送线程数。命令行：--workers N"),
                   r, 0)
        g.addWidget(self.spin_workers, r, 1)
        # 记住标签引用：按秒跑时要连标签一起置灰（和 datahub_test 一样）
        self.lbl_max = flabel(
            "总条数", "--max",
            "最多处理多少条（在「用例类型」筛选之后计算）。\n"
            "★ 0 = 全部：当前筛选出的用例各发一次。\n"
            "命令行：--max N\n"
            "【按秒跑 > 0 时本项失效】改由「按秒跑」的秒数决定何时停。")
        g.addWidget(self.lbl_max, r, 2)
        g.addWidget(self.spin_max, r, 3)
        r += 1
        self.lbl_seconds = flabel(
            "按秒跑", "--seconds",
            "跑够这么多秒就停。0=不用（默认按「总条数」跑）。\n"
            "命令行：--seconds S\n"
            "★ 填了它就忽略「总条数」，用例会循环续发到时间用完。")
        g.addWidget(self.lbl_seconds, r, 0)
        g.addWidget(self.spin_seconds, r, 1)
        g.addWidget(flabel("限速/秒", "--rate",
                           "全局限速 条/秒。0=不限速。命令行：--rate R"), r, 2)
        g.addWidget(self.spin_rate, r, 3)
        r += 1

        # ==================== C. 回复处理 ====================
        g.addWidget(section("回复处理"), r, 0, 1, 4)
        r += 1
        g.addWidget(flabel("等回包 s", "--wait",
                           "发完等回包的秒数。命令行：--wait S"), r, 0)
        g.addWidget(self.spin_wait, r, 1)
        g.addWidget(flabel("RTT 探测", "--sync-probe",
                           "压测后单发单收 N 次测真实 RTT。\n"
                           "★ 这 N 条是【额外真实报文】，不计入统计；\n"
                           "没回包时每条干等 5 秒（填 20 = 最多白等 100 秒）。\n"
                           "命令行：--sync-probe N"), r, 2)
        g.addWidget(self.spin_sync, r, 3)
        r += 1
        g.addWidget(flabel("回包流", "--reply-stream",
                           "留空=DataHub_reply_stream。命令行：--reply-stream NAME"),
                   r, 0)
        g.addWidget(self.edit_reply_stream, r, 1, 1, 3)
        r += 1
        g.addWidget(self.chk_no_reply, r, 0, 1, 2)
        g.addWidget(self.chk_quiet, r, 2, 1, 2)
        r += 1
        g.addWidget(self.chk_force_live, r, 0, 1, 4)
        r += 1

        # ==================== D. 稳定性测试(Soak) ====================
        r = self._build_soak_section(g, section, r)

        # 「按秒跑」> 0 时「总条数」失效（send_test.py 会把 max 强制设 0），
        # 所以联动置灰，避免填了个没用的值还以为生效。做法同 datahub_test。
        self.spin_seconds.valueChanged.connect(self._sync_end_mode)
        self._sync_end_mode()
        return box

    def _build_soak_section(self, g, section, r):
        """把「稳定性测试」作为发送参数里的一个分区铺进去。返回下一行行号。

        复用 send_test.py（按轮调用）而不是另写一套发送逻辑，
        这样「稳定性测试」和「开始发送」的报文完全一致，测的才是同一个东西。
        """
        g.addWidget(section("稳定性测试 (Soak)"), r, 0, 1, 4)
        r += 1

        self.chk_soak = QCheckBox("启用稳定性测试（长时间连续跑 + 趋势汇总）")
        self.chk_soak.setChecked(ini_get(self.cp, "gui", "soak", "0") == "1")
        self.chk_soak.setToolTip(
            "勾选后展开下方参数，再点底部「运行稳定性测试」：\n"
            "· 按轮反复调用 send_test.py（报文与「开始发送」完全一致）\n"
            "· 结束条件可选：按时长（默认 8h）或按轮数（短测用，跑够 N 轮收尾）\n"
            "· 只产出 trend.csv / summary.json / soak.log（避免几千个日志/表格）\n\n"
            "接口/类型/目标流沿用「1. 测试数据」的接口勾选、"
            "本面板的「发送范围」与右栏「策略平台身份」的选择。")
        # ---- 模式选择：单接口循环 vs 业务流循环 ----
        self.chk_soak_flow = QCheckBox(
            "业务流模式：一组 = create → modify → remove（推荐）")
        self.chk_soak_flow.setChecked(
            ini_get(self.cp, "gui", "soak_flow", "0") == "1")
        self.chk_soak_flow.setToolTip(
            "勾上 = 按【业务顺序】成组循环，最接近真实负载：\n"
            "  ① create 发 N 条（自动从回包抓真实单号）\n"
            "  ② 用这些单号现生成 modify 表 → 发 modify N 条\n"
            "  ③ 同样现生成 remove 表 → 发 remove N 条\n"
            "  然后循环下一组。\n\n"
            "★ Ref 是动态的（平台回包给的），所以 modify/remove 表【每组重新生成】，\n"
            "  不能预先写死 —— 否则第二天单号日期变了会全部 ref not exist。\n\n"
            "不勾 = 单接口循环（原来的行为：反复发「1. 测试数据」里勾选的那个接口）。\n\n"
            "⚠ 用自带 Mock 平台验证时必须勾 Mock 的「回包带回 Ref」，\n"
            "   否则抓不到单号、业务流跑不起来。")
        g.addWidget(self.chk_soak_flow, r, 0, 1, 4)
        r += 1

        g.addWidget(self.chk_soak, r, 0, 1, 4)
        r += 1

        # ---- 参数区：随勾选显示/隐藏 ----
        self.soak_params = QWidget()
        sp = QGridLayout(self.soak_params)
        sp.setContentsMargins(14, 0, 0, 0)
        sp.setColumnStretch(1, 1)
        sp.setColumnStretch(3, 1)
        sp.setVerticalSpacing(6)

        self.combo_soak_mode = QComboBox()
        self.combo_soak_mode.addItem("按时长", "hours")
        self.combo_soak_mode.addItem("按轮数", "rounds")
        _i = self.combo_soak_mode.findData(ini_get(self.cp, "gui", "soak_mode", "rounds"))
        self.combo_soak_mode.setCurrentIndex(_i if _i >= 0 else 1)
        self.combo_soak_mode.setToolTip(
            "按轮数：跑够 N 轮即正常收尾（便于短测，几十秒就能验证整套流程）\n"
            "按时长：连续跑到设定小时数（长稳用）")

        self.spin_soak_rounds = QSpinBox()
        self.spin_soak_rounds.setRange(1, 100000)
        self.spin_soak_rounds.setValue(
            int(float(ini_get(self.cp, "gui", "soak_rounds", "5") or 5)))
        self.spin_soak_rounds.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_soak_rounds.setToolTip("跑够这么多轮就正常结束（优先于时长）")

        self.spin_soak_hours = QDoubleSpinBox()
        self.spin_soak_hours.setRange(0.01, 240)
        self.spin_soak_hours.setDecimals(2)
        self.spin_soak_hours.setValue(
            float(ini_get(self.cp, "gui", "soak_hours", "8") or 8))
        self.spin_soak_hours.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_soak_hours.setToolTip("连续跑多少小时（长稳用）")

        self.spin_soak_batch = QSpinBox()
        self.spin_soak_batch.setRange(1, 1000000)
        self.spin_soak_batch.setValue(
            int(float(ini_get(self.cp, "gui", "soak_batch", "500") or 500)))
        self.spin_soak_batch.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_soak_batch.setToolTip(
            "每一轮发多少条（soak 用它覆盖 --max，不影响上面的「总条数」）。\n\n"
            "⚠ 这不是限速：真实速率 = 每轮条数 ÷ 每轮耗时，\n"
            "而每轮耗时含【固定开销】（起子进程 + 读 Excel + 写汇总，约 1~3 秒）。\n"
            "条数越小，被固定开销拉低的平均速率越明显。\n"
            "想要恒定速率请用上面的「限速/秒」。")

        sp.addWidget(QLabel("结束条件"), 0, 0)
        sp.addWidget(self.combo_soak_mode, 0, 1)
        sp.addWidget(QLabel("每轮条数"), 0, 2)
        sp.addWidget(self.spin_soak_batch, 0, 3)
        sp.addWidget(QLabel("轮数"), 1, 0)
        sp.addWidget(self.spin_soak_rounds, 1, 1)
        sp.addWidget(QLabel("时长(小时)"), 1, 2)
        sp.addWidget(self.spin_soak_hours, 1, 3)

        self.spin_soak_gap = QDoubleSpinBox()
        self.spin_soak_gap.setRange(0, 3600)
        self.spin_soak_gap.setDecimals(1)
        self.spin_soak_gap.setValue(
            float(ini_get(self.cp, "gui", "soak_gap", "0") or 0))
        self.spin_soak_gap.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_soak_gap.setToolTip("每轮之间的停顿秒数。停顿计入每轮耗时，会拉低平均速率")

        self.combo_soak_clean = QComboBox()
        self.combo_soak_clean.addItem("只监控不清理（推荐）", "monitor")
        self.combo_soak_clean.addItem("每轮清理回包流", "per-round")
        _c = self.combo_soak_clean.findData(ini_get(self.cp, "gui", "soak_clean", "monitor"))
        self.combo_soak_clean.setCurrentIndex(_c if _c >= 0 else 0)
        self.combo_soak_clean.setToolTip(
            "monitor（默认，最安全）：只记录流的 XLEN / lag / 未ACK 趋势，不动数据。\n\n"
            "per-round：每轮结束后清空【回包流 DataHub_reply_stream】。\n"
            "⚠ 该流是多条 ST-* 共用的全局流（不同于 datahub 那边每条 WT 自己的回复流），"
            "非独占环境清理会干扰别人。只有在确认这个 Redis 上只有你在用、"
            "且回包流涨得太快时才开。\n"
            "目标流 ST-<编号> 始终只监控、不清理（避免删掉平台还没读的报文）。")

        sp.addWidget(QLabel("轮间隔(秒)"), 2, 0)
        sp.addWidget(self.spin_soak_gap, 2, 1)
        sp.addWidget(QLabel("流处理"), 2, 2)
        sp.addWidget(self.combo_soak_clean, 2, 3)

        # ---- 业务流中场停顿：create→modify→停N秒→remove ----
        # 只在业务流模式下有意义（单接口模式没有 modify→remove 这个位置），
        # 所以由 _sync_soak_flow() 按模式启用/置灰。
        self.chk_soak_midgap = QCheckBox("业务流中场停顿：modify 之后停")
        self.chk_soak_midgap.setChecked(
            ini_get(self.cp, "gui", "soak_midgap_on", "0") == "1")
        self.chk_soak_midgap.setToolTip(
            "把一组业务流改成：create → modify →【停 N 秒】→ remove。\n\n"
            "用途：观察「改单后停留一段时间再删单」的时序场景 ——\n"
            "比如平台在 modify 之后需要一段时间才把改动落库，\n"
            "立刻 remove 和等一会儿 remove 的行为可能不同。\n\n"
            "· 停顿期间【完全不碰 Redis】，是一个干净的静默窗口\n"
            "· 停顿计入本组耗时（trend.csv 的耗时/速率会相应变慢）\n"
            "· 只对业务流模式生效；单接口模式不传这个参数")

        self.spin_soak_midgap = QDoubleSpinBox()
        self.spin_soak_midgap.setRange(0.1, 86400)
        self.spin_soak_midgap.setDecimals(1)
        self.spin_soak_midgap.setSuffix(" 秒")
        self.spin_soak_midgap.setValue(
            float(ini_get(self.cp, "gui", "soak_midgap_sec", "10") or 10))
        self.spin_soak_midgap.setMaximumWidth(110)
        self.spin_soak_midgap.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_soak_midgap.setToolTip("modify 发完后等多少秒再发 remove（默认 10 秒）")
        sp.addWidget(self.chk_soak_midgap, 3, 0, 1, 3)
        sp.addWidget(self.spin_soak_midgap, 3, 3)

        self.chk_soak_rotate = QCheckBox("轮换用例（每轮换一批，避免重复发同一批数据）")
        self.chk_soak_rotate.setChecked(ini_get(self.cp, "gui", "soak_rotate", "1") == "1")
        self.chk_soak_rotate.setToolTip(
            "每轮按【本类型真实存在的行号】取一段（末尾回绕），避免反复发同一批。\n\n"
            "为什么要按类型取：本表行序是 normal(10000) → error → destroy(96)，\n"
            "destroy 只在第 10008~10103 行。若按「总行数」切段，头几轮的区间\n"
            "全是 normal，配 --type destroy 会一条都选不到，整场 soak 全变「跳过」。\n\n"
            "· create 等有唯一性约束的接口【建议开启】\n"
            "· 不开也能跑，但每轮都发同一批，数据代表性差")
        sp.addWidget(self.chk_soak_rotate, 4, 0, 1, 4)

        self.chk_soak_keep = QCheckBox("保留每轮明细（默认只留异常轮）")
        self.chk_soak_keep.setChecked(
            ini_get(self.cp, "gui", "soak_keep", "0") == "1")
        self.chk_soak_keep.setToolTip(
            "默认只保留【异常轮】的 stats JSON/Excel，正常轮的明细会被清掉，\n"
            "避免跑一晚上堆出几千个文件。勾上则每轮都留（占空间，排查细粒度用）。")
        sp.addWidget(self.chk_soak_keep, 5, 0, 1, 4)

        # ---- 异常阈值 ----
        note = QLabel(
            "⚠ 判异常用三类信号（回复率按用例类型自动分档）：\n"
            "· normal 默认要求回复率 ≥99%（压测数据本该条条有回包）\n"
            "· destroy/error 默认不判回复率（畸形报文不回包是平台正常行为）\n"
            "· lag/未ACK 能区分「平台没读」和「读了卡住」，建议长稳时都设上")
        note.setWordWrap(True)
        note.setStyleSheet("color:#a15c00; background:#fff8e6;"
                           " border:1px solid #f0d9a0; border-radius:3px; padding:4px;")
        sp.addWidget(note, 6, 0, 1, 4)

        self.spin_soak_minreply = QDoubleSpinBox()
        self.spin_soak_minreply.setRange(-1, 100)
        self.spin_soak_minreply.setDecimals(1)
        self.spin_soak_minreply.setValue(
            float(ini_get(self.cp, "gui", "soak_minreply", "-1") or -1))
        self.spin_soak_minreply.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_soak_minreply.setToolTip(
            "单轮回复率低于它就算异常轮。-1 = 按用例类型自动（normal 99，"
            "destroy/error 不判）；0 = 显式不判。\n\n"
            "⚠ destroy 的回包行为不稳定：2026-09-29 同一天实测过 0% 和 100%，\n"
            "盯 destroy 的回包要谨慎，别把它当硬判据。\n\n"
            "分子只算「对得上本次 request_id」的回包，恒 ≤100%（共用回包流上\n"
            "别人的回复已被剔除）。")

        self.spin_soak_maxlag = QSpinBox()
        self.spin_soak_maxlag.setRange(0, 100000000)
        self.spin_soak_maxlag.setValue(
            int(float(ini_get(self.cp, "gui", "soak_maxlag", "0") or 0)))
        self.spin_soak_maxlag.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_soak_maxlag.setToolTip(
            "目标流 lag（还没被平台读走的条数）超过它就算异常轮。\n"
            "0 = 不判。长稳建议设成每轮条数的 1~3 倍。\n\n"
            "lag 持续上涨 = 平台不伸手读了（可能挂了/编号变了）。")

        self.spin_soak_maxpend = QSpinBox()
        self.spin_soak_maxpend.setRange(0, 100000000)
        self.spin_soak_maxpend.setValue(
            int(float(ini_get(self.cp, "gui", "soak_maxpend", "0") or 0)))
        self.spin_soak_maxpend.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_soak_maxpend.setToolTip(
            "目标流「未ACK」（平台读了但没确认的条数）超过它就算异常轮。\n"
            "0 = 不判。长稳建议设成每轮条数的 1~3 倍。\n\n"
            "未ACK 持续上涨 = 平台读了却处理不完（卡住/线程池耗尽），\n"
            "这是最可靠的「平台挂了」信号（每次卡死都留下这个特征）。")

        self.spin_soak_maxtout = QSpinBox()
        self.spin_soak_maxtout.setRange(0, 100000000)
        self.spin_soak_maxtout.setValue(
            int(float(ini_get(self.cp, "gui", "soak_maxtout", "0") or 0)))
        self.spin_soak_maxtout.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_soak_maxtout.setToolTip(
            "单轮「超时未回」超过它算异常轮。0 = 不判。\n"
            "建议设成每轮条数的 1~2 倍。跑 normal 时它基本等于「丢失数」。")

        self.spin_soak_maxout = QSpinBox()
        self.spin_soak_maxout.setRange(0, 100000000)
        self.spin_soak_maxout.setValue(
            int(float(ini_get(self.cp, "gui", "soak_maxout", "0") or 0)))
        self.spin_soak_maxout.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_soak_maxout.setToolTip(
            "单轮结束时「在途未回」超过它算异常轮。0 = 不判。\n"
            "在途不清零 = 平台收了却一直不回（配合未ACK 看能区分卡在哪）。")

        self.spin_soak_maxbiz = QSpinBox()
        self.spin_soak_maxbiz.setRange(-1, 100000000)
        self.spin_soak_maxbiz.setValue(
            int(float(ini_get(self.cp, "gui", "soak_maxbiz", "0") or 0)))
        self.spin_soak_maxbiz.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_soak_maxbiz.setToolTip(
            "单轮【业务失败】达到多少条算异常轮。\n"
            "  0  = 不判（默认）\n"
            "  -1 = 零容忍：失败 1 条就报（长稳推荐）\n"
            "  N  = 超过 N 条才报\n\n"
            "★ 为什么需要它：回包「到了」不等于「办成了」。\n"
            "  平台可能回 {\"ErrID\":-5,\"Errmsg\":\"ref already inserted\"}\n"
            "  —— 这算业务失败，但回复率仍是 100%。\n"
            "  实测过回复率 100% 而其中 30% 是业务失败的批次，\n"
            "  光看回复率会把它判成「全绿」。\n\n"
            "失败原因会归类显示在日志/trend.csv 的「异常」列里。")

        sp.addWidget(QLabel("lag 上限"), 7, 0)
        sp.addWidget(self.spin_soak_maxlag, 7, 1)
        sp.addWidget(QLabel("未ACK 上限"), 7, 2)
        sp.addWidget(self.spin_soak_maxpend, 7, 3)

        sp.addWidget(QLabel("回复率下限%"), 8, 0)
        sp.addWidget(self.spin_soak_minreply, 8, 1)
        sp.addWidget(QLabel("超时未回上限"), 8, 2)
        sp.addWidget(self.spin_soak_maxtout, 8, 3)

        sp.addWidget(QLabel("在途上限"), 9, 0)
        sp.addWidget(self.spin_soak_maxout, 9, 1)
        sp.addWidget(QLabel("业务失败上限"), 9, 2)
        sp.addWidget(self.spin_soak_maxbiz, 9, 3)

        self.lbl_soak_hint = QLabel("")
        self.lbl_soak_hint.setWordWrap(True)
        self.lbl_soak_hint.setStyleSheet("color:#666;")
        sp.addWidget(self.lbl_soak_hint, 10, 0, 1, 4)

        g.addWidget(self.soak_params, r, 0, 1, 4)
        r += 1

        # SpinBox + ComboBox 都要装：漏装的下拉框会被"鼠标滚轮划过"直接改值
        # （曾经漏了 combo_soak_mode / combo_soak_clean，用户只是滚页面
        #   却把「结束条件」「流处理」改了）。
        for w in (self.spin_soak_batch, self.spin_soak_rounds, self.spin_soak_hours,
                  self.spin_soak_gap, self.spin_soak_midgap,
                  self.spin_soak_maxlag, self.spin_soak_maxpend,
                  self.spin_soak_maxtout, self.spin_soak_maxout,
                  self.spin_soak_maxbiz, self.spin_soak_minreply,
                  self.combo_soak_mode, self.combo_soak_clean):
            self.guard.install(w)
        self.combo_soak_mode.currentIndexChanged.connect(self._sync_soak_mode)
        self.chk_soak.toggled.connect(self._sync_soak_visibility)
        self.chk_soak_flow.toggled.connect(self._sync_soak_flow)
        # 中场停顿：勾选启用值框；值变了刷新提示
        self.chk_soak_midgap.toggled.connect(self._sync_soak_midgap)
        self.spin_soak_midgap.valueChanged.connect(self._update_soak_hint)
        # 注意：chk_svc_refecho 属于【右栏服务管理】，而这里是左栏，
        # 构建顺序上它还不存在 —— 那个信号在 _build_right 里接。
        self.spin_soak_batch.valueChanged.connect(self._update_soak_hint)
        # 这些都会改变"实际生效的判据"，提示要跟着变
        for w in (self.spin_soak_maxlag, self.spin_soak_maxpend,
                  self.spin_soak_maxtout, self.spin_soak_maxout,
                  self.spin_soak_maxbiz, self.spin_soak_minreply):
            w.valueChanged.connect(self._update_soak_hint)
        # 用例类型决定回复率默认档位，切换时要刷新提示
        self.combo_type.currentIndexChanged.connect(self._update_soak_hint)
        self._sync_soak_visibility()
        self._sync_soak_mode()
        self._sync_soak_flow()
        self._update_soak_hint()
        return r

    def _sync_end_mode(self):
        """按「按秒跑」是否启用，置灰/启用「总条数」。

        send_test.py 里：
            if seconds > 0:
                sender.max_count = 0      # max 被丢弃
                Timer(seconds, stop)
        即两者互斥，同时填只有秒数算数。所以这里把用不上的那个灰掉。
        """
        by_time = self.spin_seconds.value() > 0
        ok = not by_time
        for w in (self.spin_max, getattr(self, "lbl_max", None)):
            if w is not None:
                w.setEnabled(ok)
        # 置灰时明确说清"为什么灰"，而不是让人猜
        if by_time:
            self.spin_max.setToolTip(
                "【当前被禁用】因为「按秒跑」填了 %s 秒 —— send_test.py 在按时间模式下\n"
                "会把总条数强制设为 0（不限），改由秒数决定何时停。\n"
                "想让总条数生效，请把「按秒跑」改成 0。"
                % self.spin_seconds.value())
        else:
            self.spin_max.setToolTip(
                "最多处理多少条（在「用例类型」筛选之后计算）。\n"
                "★ 0 = 全部：当前筛选出的用例各发一次。\n"
                "命令行：--max N\n"
                "【按秒跑 > 0 时本项失效】改由「按秒跑」的秒数决定何时停。")

    def _box_misc(self):
        box = CollapsibleBox("输出")
        g = QGridLayout(box.content)
        self.edit_label = QLineEdit("")
        self.edit_label.setPlaceholderText("留空=接口_类型_时间戳")
        self.edit_stats_out = QLineEdit(ini_get(self.cp, "gui", "stats_out", PERF_DIR))
        btn_pick = QPushButton("选择目录")
        btn_pick.clicked.connect(self._pick_stats_dir)
        btn_open = QPushButton("打开输出目录")
        btn_open.clicked.connect(self._open_out_dir)

        g.addWidget(QLabel("标签"), 0, 0)
        g.addWidget(self.edit_label, 0, 1, 1, 3)
        g.addWidget(QLabel("统计目录"), 1, 0)
        g.addWidget(self.edit_stats_out, 1, 1)
        g.addWidget(btn_pick, 1, 2)
        g.addWidget(btn_open, 1, 3)
        return box

    def _soak_by_rounds(self):
        return (self.combo_soak_mode.currentData() or "hours") == "rounds"

    def _box_remote(self):
        """远程 Linux：稳定性测试跑在远端（长时间不受 Windows 休眠/断网影响）。"""
        box = CollapsibleBox("远程 Linux（稳定性测试跑在远端）")
        box.setExpanded(ini_get(self.cp, "gui", "box_remote", "0") == "1")
        g = QGridLayout(box.content)

        self.chk_remote = QCheckBox("启用远程执行（稳定性测试在 Linux 上跑）")
        self.chk_remote.setChecked(
            ini_get(self.cp, "gui", "remote", "0") == "1")
        self.chk_remote.setToolTip(
            "勾上后，「运行稳定性测试」会通过 SSH 在远程 Linux 上执行：\n"
            "  · 自动上传脚本与数据表（只传比远程新的，不会每次全量）\n"
            "  · 实时把远端输出回传到下面日志框\n"
            "  · 跑完自动下载 trend.csv / summary.json / soak.log 到本地\n\n"
            "★ 为什么建议跑 Linux：长时间长稳不受 Windows 休眠/断网影响，\n"
            "  也和现场环境一致（datahub_test 的 .so 同样只能在 Linux 上跑）。\n\n"
            "不勾 = 在本地 Windows 上跑（也能用，但长稳容易被系统干扰）。")

        self.edit_ssh_host = QLineEdit(ini_get(self.cp, "gui", "ssh_host", "192.168.1.136"))
        self.spin_ssh_port = QSpinBox()
        self.spin_ssh_port.setRange(1, 65535)
        self.spin_ssh_port.setValue(
            int(float(ini_get(self.cp, "gui", "ssh_port", "22") or 22)))
        self.spin_ssh_port.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_ssh_port.setMaximumWidth(80)
        self.edit_ssh_user = QLineEdit(ini_get(self.cp, "gui", "ssh_user", "yangsh"))
        self.edit_ssh_pass = QLineEdit(ini_get(self.cp, "gui", "ssh_pass", ""))
        self.edit_ssh_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_ssh_dir = QLineEdit(
            ini_get(self.cp, "gui", "ssh_dir", "/home/yangsh/so_test/strategy_soak"))
        self.edit_ssh_dir.setToolTip(
            "远端存放脚本与数据的目录。\n"
            "★ 建议用【独立目录】，别直接写现场在用的目录 —— 上传会覆盖同名文件。\n"
            "第一次运行会自动创建。")
        self.chk_ssh_nohup = QCheckBox("后台运行(nohup)：启动后立即返回，断开也不停")
        self.chk_ssh_nohup.setChecked(
            ini_get(self.cp, "gui", "ssh_nohup", "0") == "1")
        self.chk_ssh_nohup.setToolTip(
            "把稳定性测试放到远端后台执行（setsid + nohup）后立即返回，不占住 SSH：\n"
            "  · 关掉 GUI / 断开网络，远端照样继续跑（长稳推荐）\n"
            "  · 远端日志：<目录>/out/soak/soak_<名>_<时间>_nohup.log\n"
            "  · 停止：ssh 上去 pkill -INT -f soak_test.py（别用 -9，会丢 summary.json）\n\n"
            "★ 后台模式【不会自动下载结果】（启动瞬间还没有结果）——\n"
            "  跑完后再点「下载远端结果」取回。")

        btn_test = QPushButton("测试连接")
        btn_test.clicked.connect(self.on_test_ssh)
        btn_dl = QPushButton("下载远端结果")
        btn_dl.setToolTip("把远端 out/soak 下的 trend.csv / summary.json / soak.log 取回本地")
        btn_dl.clicked.connect(self.on_download_remote)
        btn_ssh_stop = QPushButton("停止远端")
        btn_ssh_stop.setToolTip("在远端执行 pkill -INT -f soak_test.py（优雅停止，保留 summary.json）")
        btn_ssh_stop.clicked.connect(self.on_stop_remote)

        self.lbl_remote = QLabel("")
        self.lbl_remote.setWordWrap(True)
        self.lbl_remote.setStyleSheet("color:#666;")

        g.addWidget(self.chk_remote, 0, 0, 1, 4)
        g.addWidget(QLabel("主机"), 1, 0)
        g.addWidget(self.edit_ssh_host, 1, 1)
        g.addWidget(QLabel("端口"), 1, 2)
        g.addWidget(self.spin_ssh_port, 1, 3)
        g.addWidget(QLabel("用户"), 2, 0)
        g.addWidget(self.edit_ssh_user, 2, 1)
        g.addWidget(QLabel("密码"), 2, 2)
        g.addWidget(self.edit_ssh_pass, 2, 3)
        g.addWidget(QLabel("远端目录"), 3, 0)
        g.addWidget(self.edit_ssh_dir, 3, 1, 1, 3)
        g.addWidget(self.chk_ssh_nohup, 4, 0, 1, 4)
        g.addWidget(btn_test, 5, 0)
        g.addWidget(btn_dl, 5, 1)
        g.addWidget(btn_ssh_stop, 5, 2, 1, 2)
        g.addWidget(self.lbl_remote, 6, 0, 1, 4)

        self.guard.install(self.edit_ssh_host, self.spin_ssh_port,
                           self.edit_ssh_user, self.edit_ssh_pass,
                           self.edit_ssh_dir)
        for w in (self.edit_ssh_host, self.edit_ssh_user, self.edit_ssh_dir,
                  self.edit_ssh_pass):
            w.textChanged.connect(self._update_remote_hint)
        self.spin_ssh_port.valueChanged.connect(self._update_remote_hint)
        self.chk_remote.toggled.connect(self._update_remote_hint)
        self._update_remote_hint()
        return box

    def _remote_dir(self):
        return self.edit_ssh_dir.text().strip().rstrip("/")

    def _remote_on(self):
        return hasattr(self, "chk_remote") and self.chk_remote.isChecked()

    def _update_remote_hint(self):
        try:
            if self._remote_on():
                self.lbl_remote.setText(
                    "目标: %s@%s:%d  →  %s"
                    % (self.edit_ssh_user.text().strip(),
                       self.edit_ssh_host.text().strip(),
                       self.spin_ssh_port.value(), self._remote_dir()))
                self.lbl_remote.setStyleSheet("color:#0a5;")
            else:
                self.lbl_remote.setText("未启用：稳定性测试在本地 Windows 上跑")
                self.lbl_remote.setStyleSheet("color:#888;")
        except Exception:
            pass

    def _remote_session_kw(self):
        return dict(host=self.edit_ssh_host.text().strip(),
                    port=self.spin_ssh_port.value(),
                    user=self.edit_ssh_user.text().strip(),
                    pwd=self.edit_ssh_pass.text(),
                    remote_dir=self._remote_dir())

    def on_test_ssh(self):
        """只读体检远端环境（不跑任何测试）。"""
        kw = self._remote_session_kw()
        self.append_log("")
        self.append_log("[SSH] 测试连接 %s@%s:%d ..."
                        % (kw["user"], kw["host"], kw["port"]))
        try:
            import ssh_runner as SR
        except Exception as e:
            self.append_log("[SSH][ERROR] 载入 ssh_runner 失败: %s" % e)
            return
        ok, txt = SR.check_remote(kw["host"], kw["port"], kw["user"], kw["pwd"],
                                  kw["remote_dir"])
        for ln in txt.splitlines():
            self.append_log("  " + ln)
        self.append_log("[SSH] %s" % ("连接可用 ✓" if ok else "连接失败 ✗"))

    def on_download_remote(self):
        """把远端 out/soak 的结果文件下载回本地。"""
        kw = self._remote_session_kw()
        local = os.path.join(OUT_DIR, "soak")
        self.append_log("[SSH] 下载远端结果 → %s" % local)
        try:
            import ssh_runner as SR
        except Exception as e:
            self.append_log("[SSH][ERROR] %s" % e)
            return
        dl = {"dirs": [{
            "remote": kw["remote_dir"] + "/out/soak",
            "local": local,
            "patterns": ["*_trend.csv", "*_summary.json", "*_soak.log",
                         "*_nohup.log", "*.json"],
        }]}
        try:
            s = SR.SshSession(kw["host"], kw["port"], kw["user"], kw["pwd"],
                              kw["remote_dir"], on_line=self.append_log)
            s.connect()
            try:
                # ★ 用 download_all：主动取结果时不做"只下新增"过滤，
                #   否则刚跑完的文件会被当成历史文件跳过。
                s.download_all(dl)
            finally:
                s.close()
        except Exception as e:
            self.append_log("[SSH][ERROR] %s" % e)

    def on_stop_remote(self):
        """在远端优雅停止 soak（保留 summary.json）。

        ★ 检测存活必须用 `[s]oak_test.py` 这种方括号写法：
          直接写 `pgrep -f soak_test.py` 会匹配到【执行这条命令的 shell 自己】
          （它的命令行里含这个字符串），于是永远报"仍在运行"。
          实测踩过：没有 soak 进程时也输出"仍在运行"。
        """
        kw = self._remote_session_kw()
        if not self._ask("确认停止", "在远端执行 pkill -INT -f soak_test.py？\n"
                                    "（优雅停止，会写出 summary.json）"):
            return
        self.append_log("[SSH] 远端停止 ...")
        try:
            import ssh_runner as SR
            rc = SR.run_remote(kw["host"], kw["port"], kw["user"], kw["pwd"],
                               kw["remote_dir"],
                               "pkill -INT -f soak_test.py; sleep 1; "
                               "pgrep -f '[s]oak_test.py' >/dev/null "
                               "&& echo 仍在运行 || echo 已停止",
                               on_line=self.append_log)
        except Exception as e:
            self.append_log("[SSH][ERROR] %s" % e)

    def _sync_soak_visibility(self):
        on = self.chk_soak.isChecked()
        self.soak_params.setVisible(on)
        self._sync_soak_button()

    def _sync_soak_mode(self):
        """按时长/按轮数二选一，把用不上的那个置灰（别让人填了以为生效）。"""
        by_rounds = self._soak_by_rounds()
        self.spin_soak_rounds.setEnabled(by_rounds)
        self.spin_soak_hours.setEnabled(not by_rounds)

    def _sync_soak_button(self, running=None):
        """「运行稳定性测试」只在【勾选了 soak】且【空闲】时可点。

        用显式标志 _soak_running 追踪：worker 在 set_running(True) 时会置位，
        因为 GUI 立即返回（soak 在子进程里跑），不能只看 worker.isRunning()。
        """
        if not hasattr(self, "btn_soak"):
            return
        if running is not None:
            self._soak_running = bool(running)
        self.btn_soak.setEnabled(
            self.chk_soak.isChecked() and not getattr(self, "_soak_running", False))

    def _set_ifaces_hint(self, flow):
        """业务流模式时，在接口勾选区显示"这些勾选不参与"的说明。"""
        lbl = getattr(self, "lbl_ifaces_note", None)
        if lbl is None:
            return
        if flow:
            lbl.setText("⚠ 已启用「业务流模式」：接口固定按 create → modify → remove "
                        "顺序跑，这里的接口勾选【不参与】稳定性测试。")
        lbl.setVisible(bool(flow))

    def _sync_soak_flow(self):
        """业务流模式下，把与之冲突的控件置灰。

        soak_test.py 里 --flow 的硬约束（这些控件勾了也没用，灰掉免得误解）：
          · 不能配 --rotate（每轮要重生成 modify/remove 表，行号轮换无意义）
          · 只能 --type normal（业务流发的是合法报文）
          · 只能 --clean monitor（清理回包流会干扰 refs 抓取）
        """
        flow = self.chk_soak_flow.isChecked()
        self.chk_soak_rotate.setEnabled(not flow)
        self.combo_soak_clean.setEnabled(not flow)
        self.combo_type.setEnabled(not flow)
        # 业务流模式的接口是固定的（create→modify→remove），
        # 「1. 测试数据」的接口勾选不参与 —— 灰掉并说明，免得以为勾了才生效。
        for chk in getattr(self, "chk_ifaces", {}).values():
            chk.setEnabled(not flow)
        self._set_ifaces_hint(flow)
        if flow:
            # 业务流恒用 normal + monitor；不强行改用户的选择值，
            # 只让它们"看起来不生效"，避免来回切换时丢掉用户设置。
            self.chk_soak_flow.setToolTip(
                self.chk_soak_flow.toolTip().split("\n\n⚠ 业务流模式")[0] +
                "\n\n⚠ 业务流模式下：用例类型固定 normal、清理固定 monitor、"
                "「轮换用例」不生效（已置灰）。")
        self._sync_refecho_hint()
        self._sync_soak_midgap()
        self._update_soak_hint()

    def _sync_soak_midgap(self):
        """中场停顿只在【业务流模式】下有意义，按模式启用/置灰。

        单接口模式（反复发一个接口）没有 modify→remove 这个中途位置，
        soak_test.py 会直接拒绝 --mid-gap 并退出 —— 置灰免得点了报错。
        """
        flow = self.chk_soak_flow.isChecked()
        on = self.chk_soak_midgap.isChecked()
        self.chk_soak_midgap.setEnabled(flow)
        # 秒数框：业务流 + 勾了停顿 才可编辑
        self.spin_soak_midgap.setEnabled(flow and on)
        if not flow:
            self.chk_soak_midgap.setToolTip(
                "⚠ 只有勾选上面的「业务流模式」时才生效。\n\n"
                "单接口模式没有 modify→remove 这个中途位置，\n"
                "soak_test.py 会拒绝 --mid-gap。")
        else:
            self.chk_soak_midgap.setToolTip(
                "把一组业务流改成：create → modify →【停 N 秒】→ remove。\n\n"
                "用途：观察「改单后停留一段时间再删单」的时序场景 ——\n"
                "比如平台在 modify 之后需要一段时间才把改动落库，\n"
                "立刻 remove 和等一会儿 remove 的行为可能不同。\n\n"
                "· 停顿期间【完全不碰 Redis】，是一个干净的静默窗口\n"
                "· 停顿计入本组耗时（trend.csv 的耗时/速率会相应变慢）")
        # 勾选/取消会改变"将执行"的描述，提示要跟着刷新
        self._update_soak_hint()

    def _sync_refecho_hint(self):
        """业务流模式依赖 Mock 的「回包带回 Ref」，缺了就明确提醒。

        不自动改用户的勾选（那是服务启动参数，静默改动会让人困惑），
        但要把后果说清楚 —— 否则只会得到一句莫名其妙的
        「create 没有返回任何 Ref」。
        """
        chk = getattr(self, "chk_svc_refecho", None)
        if chk is None:
            return
        base = ("仿真实平台：回包时把请求里的 Ref 原样带回。\n\n"
                "★ 跑「业务流模式」（create→modify→remove）【必须勾上】：\n"
                "  业务流要从 create 的回包抓真实单号，去生成 modify/remove 表。\n"
                "  不勾的话 Mock 只回固定的 {\"status\":\"OK\"}，抓不到单号，\n"
                "  第一步就会失败（提示「create 没有返回任何 Ref」）。\n\n"
                "打真平台时不用管它（真平台本来就带 Ref）。")
        if self.chk_soak_flow.isChecked() and not chk.isChecked():
            chk.setStyleSheet("color:#b00; font-weight:bold;")
            chk.setToolTip("⚠ 你已勾选「业务流模式」，但这里没勾！\n"
                           "没勾的话 create 抓不到单号，业务流第 1 步就会失败。\n\n" + base)
        else:
            chk.setStyleSheet("")
            chk.setToolTip(base)

    def _update_soak_hint(self):
        """把当前配置换算成人话，避免误填。"""
        try:
            n = len(self.selected_interfaces())
            b = self.spin_soak_batch.value()
            flow = self.chk_soak_flow.isChecked()
            if flow:
                # 业务流：一组 = 三段各 b 条
                mid = ""
                if self.chk_soak_midgap.isChecked():
                    mid = " + 中场停 %gs" % self.spin_soak_midgap.value()
                if self._soak_by_rounds():
                    desc = "共 %d 组 × (create+modify+remove 各 %d 条%s) = 约 %d 条" % (
                        self.spin_soak_rounds.value(), b, mid,
                        self.spin_soak_rounds.value() * b * 3)
                else:
                    desc = ("时长 %.2fh，每组 create+modify+remove 各 %d 条%s"
                            "（组数取决于每组耗时）"
                            % (self.spin_soak_hours.value(), b, mid))
                desc += "；目标流 %s" % self._stream_name()
            else:
                if self._soak_by_rounds():
                    desc = "共 %d 轮 × %d 条 = 约 %d 条" % (
                        self.spin_soak_rounds.value(), b,
                        self.spin_soak_rounds.value() * b)
                else:
                    # 按时长只能给个下限提示（真实轮数取决于每轮耗时）
                    desc = "时长 %.2fh，每轮 %d 条（轮数取决于每轮耗时）" % (
                        self.spin_soak_hours.value(), b)
                if n:
                    desc += "；%d 个接口依次各跑一场" % n
            # 判据也提示出来：默认哪些是关的，避免"以为在判其实没判"
            mr = self.spin_soak_minreply.value()
            if mr < 0:
                # -1 = 按类型自动，和 soak_test.default_min_reply_rate_for 同口径
                mr_txt = ("99%(normal)" if (self.combo_type.currentText() or ""
                                            ).lower() == "normal" else "不判")
            elif mr == 0:
                mr_txt = "不判"
            else:
                mr_txt = "%g%%" % mr
            on = []
            if self.spin_soak_maxlag.value() > 0:
                on.append("lag>%d" % self.spin_soak_maxlag.value())
            if self.spin_soak_maxpend.value() > 0:
                on.append("未ACK>%d" % self.spin_soak_maxpend.value())
            if self.spin_soak_maxtout.value() > 0:
                on.append("超时未回>%d" % self.spin_soak_maxtout.value())
            if self.spin_soak_maxout.value() > 0:
                on.append("在途>%d" % self.spin_soak_maxout.value())
            mb = self.spin_soak_maxbiz.value()
            if mb < 0:
                on.append("业务失败零容忍")
            elif mb > 0:
                on.append("业务失败>%d" % mb)
            desc += "\n判据：回复率%s；发送失败>0%s" % (
                mr_txt, ("；" + "；".join(on)) if on
                else "；lag/未ACK/超时/在途/业务失败 均未设阈值")
            if mb == 0:
                desc += "\n⚠ 未设「业务失败上限」：回包到了但业务被拒（如 ref already " \
                        "inserted）不会被判成异常"
            self.lbl_soak_hint.setText("将执行：" + desc)
        except Exception:
            pass

    def build_soak_cmd(self, name):
        """构造 soak_test.py 命令行（接口/类型/目标流沿用左栏选择）。

        业务流模式（勾了 chk_soak_flow）时不传 --interface，改传 --flow ——
        soak 自己固定跑 create→modify→remove。
        """
        flow = self.chk_soak_flow.isChecked()
        a = self._base_cmd("soak_test.py") + self._conn_args()
        a += ["--assign-id", str(self.spin_assign.value())]
        if flow:
            a += ["--flow"]
        else:
            a += ["--interface", name]
        # ★ 下面三项在 flow 模式下有硬约束（不满足 soak 会直接拒绝运行）。
        #   控件虽被置灰，但勾选状态还在，必须在这里按模式过滤：
        #     --type  只能 normal（业务流发的是合法报文）
        #     --clean 只能 monitor（清理回包流会干扰 refs 抓取）
        #     --rotate 不能用（每轮要重生成表，行号轮换无意义）
        t = "normal" if flow else self.combo_type.currentText()
        if t:
            a += ["--type", t]
        if self._soak_by_rounds():
            a += ["--rounds", str(self.spin_soak_rounds.value())]
        else:
            a += ["--hours", "%g" % self.spin_soak_hours.value()]
        a += ["--batch", str(self.spin_soak_batch.value())]
        # 行情代码：业务流每轮要重生成 modify/remove 表，必须沿用同一套代码，
        # 否则会把生成好的表悄悄换回默认合约。
        if self.edit_contract_code.text().strip():
            a += ["--contract-code", self.edit_contract_code.text().strip()]
        if self.edit_target_code.text().strip():
            a += ["--target-stock-code", self.edit_target_code.text().strip()]
        a += ["--gap", "%g" % self.spin_soak_gap.value()]
        # 业务流中场停顿（create→modify→停N秒→remove）：
        # 只在 flow 模式传；单接口模式传了 soak 会直接拒绝退出。
        if flow and self.chk_soak_midgap.isChecked():
            a += ["--mid-gap", "%g" % self.spin_soak_midgap.value()]
        a += ["--clean", "monitor" if flow
              else (self.combo_soak_clean.currentData() or "monitor")]
        if self.chk_soak_rotate.isChecked() and not flow:
            a += ["--rotate"]
        if self.chk_soak_keep.isChecked():
            a += ["--keep-round-stats"]
        if self.spin_soak_maxlag.value() > 0:
            a += ["--max-lag", str(self.spin_soak_maxlag.value())]
        if self.spin_soak_maxpend.value() > 0:
            a += ["--max-pending", str(self.spin_soak_maxpend.value())]
        if self.spin_soak_maxtout.value() > 0:
            a += ["--max-timeout-reply", str(self.spin_soak_maxtout.value())]
        if self.spin_soak_maxout.value() > 0:
            a += ["--max-outstanding", str(self.spin_soak_maxout.value())]
        # 业务失败上限：0=不判（不加参数）；-1=零容忍；N=超过 N 条
        if self.spin_soak_maxbiz.value() != 0:
            a += ["--max-biz-fail", str(self.spin_soak_maxbiz.value())]
        # 回复率下限：-1 表示"按类型自动"（不加参数，交给 soak 自己按 type 取）
        if self.spin_soak_minreply.value() >= 0:
            a += ["--min-reply-rate", "%g" % self.spin_soak_minreply.value()]

        # ---- 透传给 send_test.py 的发送参数（soak 原样转发）----
        a += ["--workers", str(self.spin_workers.value())]
        a += ["--wait", str(self.spin_wait.value())]
        if self.spin_rate.value() > 0:
            a += ["--rate", str(self.spin_rate.value())]
        if self.spin_seconds.value() > 0:
            a += ["--seconds", str(self.spin_seconds.value())]
        if self.chk_no_reply.isChecked():
            a += ["--no-reply"]
        if self.edit_reply_stream.text().strip():
            a += ["--reply-stream", self.edit_reply_stream.text().strip()]
        if self.chk_quiet.isChecked():
            a += ["--quiet", "1"]
        if self.chk_force_live.isChecked():
            a += ["--force-live"]
        return a

    def on_soak(self):
        """运行稳定性测试。

        单接口模式：对勾选的每个接口依次各跑一场。
        业务流模式：只跑【一场】（一组固定是 create→modify→remove），
                   接口勾选不参与 —— 否则勾了 3 个接口就会把同一套业务流跑 3 遍。
        """
        if not self.chk_soak.isChecked():
            QMessageBox.information(self, "提示",
                                    "请先勾选「启用稳定性测试」")
            return
        flow = self.chk_soak_flow.isChecked()
        names = self.selected_interfaces()
        if flow:
            # 业务流不按接口拆；给个占位名让后续日志/命令构造照常工作
            names = ["(业务流)"]
        elif not names:
            QMessageBox.warning(self, "提示", "请先勾选至少一个接口")
            return
        if not self._confirm_conn():
            return
        # 便宜的预检：只看 Excel 在不在（不读内容）
        # 业务流模式下 create 表由 soak 自己按需生成，这里不预检
        if not flow:
            missing = [n for n in names
                       if CASES and not os.path.exists(CASES.default_excel(n))]
            if missing:
                QMessageBox.warning(
                    self, "缺少用例表",
                    "这些接口的 Excel 还不存在：%s\n\n"
                    "先生成：python make_excel.py --interface all" % ",".join(missing))
                return

        # 长稳前确认：这是"要跑很久"的操作，且会持续往真流写数据
        rounds_txt = ("%d 轮" % self.spin_soak_rounds.value()) if self._soak_by_rounds() \
            else ("%.2f 小时" % self.spin_soak_hours.value())
        clean = self.combo_soak_clean.currentData() or "monitor"
        warn = ""
        if clean == "per-round":
            warn = ("\n⚠ 流处理选了「每轮清理回包流」：\n"
                    "   DataHub_reply_stream 是多条 ST-* 共用的全局流，\n"
                    "   非独占环境会清掉别人的回包。确认只有你在用再继续。\n")
        if flow:
            flow_desc = "create → modify → remove"
            if self.chk_soak_midgap.isChecked():
                flow_desc = ("create → modify → 停 %gs → remove"
                             % self.spin_soak_midgap.value())
            what = "一组 = %s 各 %d 条" % (flow_desc,
                                          self.spin_soak_batch.value())
        else:
            what = "接口      : %s" % ",".join(names)
        if not self._ask(
                "确认运行稳定性测试",
                "即将运行稳定性测试：\n\n"
                "  模式      : %s\n"
                "  %s\n"
                "  类型      : %s\n"
                "  结束条件  : %s\n"
                "  每轮条数  : %d\n"
                "  目标流    : %s\n"
                "%s\n"
                "它会持续往目标流写数据。确认继续？"
                % ("业务流（create→modify→remove）" if flow else "单接口循环",
                   what, self.combo_type.currentText(), rounds_txt,
                   self.spin_soak_batch.value(), self._stream_name(), warn)):
            return

        if self.chk_autoclear.isChecked():
            self.clear_log("[提示] 日志已自动清空（勾了「每次发送前自动清空」）")
        self.append_log("")
        self.append_log("#" * 60)
        if flow:
            fd = "create→modify→remove"
            if self.chk_soak_midgap.isChecked():
                fd = ("create→modify→停%gs→remove"
                      % self.spin_soak_midgap.value())
            self.append_log("# 开始稳定性测试：业务流 %s 各 %d 条 %s"
                            " 目标流=%s"
                            % (fd, self.spin_soak_batch.value(), rounds_txt,
                               self._stream_name()))
            self.append_log("# 每组会从 create 回包抓真实单号，现生成 modify/remove 表")
        else:
            self.append_log("# 开始稳定性测试：接口=[%s] 类型=%s %s 每轮=%d 目标流=%s"
                            % (",".join(names), self.combo_type.currentText(),
                               rounds_txt, self.spin_soak_batch.value(),
                               self._stream_name()))
        self.append_log("#" * 60)
        self._soak_running = True
        self._sync_soak_button()
        if self._remote_on():
            # ---- 远程 Linux：上传脚本+数据 → 远端执行 → 下载结果 ----
            self._start_remote_soak(names, flow)
        else:
            self.append_log("[提示] soak 在子进程里跑；进度可直接看上面的输出，"
                            "或 out/soak/soak_*_soak.log")
            cmds = [self.build_soak_cmd(n) for n in names]
            self._run(cmds, on_done=lambda rc: self._after_soak(rc, names))

    # ---------------- 远程稳定性测试 ----------------
    def _remote_uploads_for(self, flow):
        """要同步到远端的文件：脚本 + 依赖模块 + 接口定义 + 数据表。

        ★ 只传【比远端新的】，所以重复运行几乎不耗时。
        ⚠ 不传 config.ini —— 远端的 Redis 地址由命令行的 --host/--db 决定，
          免得覆盖掉远端现场配置。
        """
        rd = self._remote_dir()
        up = [
            ("soak_test.py", "soak_test.py"),
            ("send_test.py", "send_test.py"),
            ("perf_stats.py", "perf_stats.py"),
            ("excel_loader.py", "excel_loader.py"),
            ("protocol.py", "protocol.py"),
            ("safety.py", "safety.py"),
            ("resp_min.py", "resp_min.py"),
            ("make_excel.py", "make_excel.py"),
            ("config.py", "config.py"),
            ("interfaces/_common.py", "interfaces/_common.py"),
        ]
        # 业务流模式要 create/modify/remove 三个接口定义 + 三张表
        ifaces = ["create", "modify", "remove"] if flow else self.selected_interfaces()
        for n in ifaces:
            up.append(("interfaces/%s.py" % n, "interfaces/%s.py" % n))
            up.append(("data/%s.xlsx" % n, "data/%s.xlsx" % n))
        out = []
        for rel, rrel in up:
            local = os.path.join(BASE_DIR, rel.replace("/", os.sep))
            if os.path.exists(local):
                out.append((local, rd + "/" + rrel))
        return out, ifaces

    def _build_remote_cmd(self, name, flow):
        """构造在远端执行的命令（python3 + 脚本 + 参数）。

        ★ 参数必须做 shell 转义：Redis 密码形如 `QianLong@2026&`，
          其中的 `&` 在 shell 里是【后台执行符】，不转义会把命令截断
          （后半截变成独立命令，轻则参数丢失，重则误执行）。
        """
        import shlex
        a = self.build_soak_cmd(name)
        # 本地是 [PYTHON, 绝对路径/soak_test.py, 参数...]；远端要换成
        # ["python3", "soak_test.py", 参数...]（相对远端目录）
        args = [str(x) for x in a[2:]]
        parts = ["python3", "soak_test.py"] + [shlex.quote(x) for x in args]
        cmd = " ".join(parts)
        if self.chk_ssh_nohup.isChecked():
            import ssh_runner as SR
            ts = time.strftime("%Y%m%d_%H%M%S")
            safe = "flow" if flow else name
            cmd, logf = SR.build_nohup_cmd(cmd, safe, ts)
            self.append_log("[SSH] 后台模式：远端日志 %s" % logf)
        return cmd

    def _start_remote_soak(self, names, flow):
        kw = self._remote_session_kw()
        uploads, ifaces = self._remote_uploads_for(flow)
        jobs, dls = [], []
        local_soak = os.path.join(OUT_DIR, "soak")
        for n in names:
            label = "业务流 create→modify→remove" if flow else n
            jobs.append((label, self._build_remote_cmd(n, flow)))
            if self.chk_ssh_nohup.isChecked():
                dls.append(None)          # 后台模式：启动即返回，没有结果可下
            else:
                # 远端 soak 的输出按 <name> 匹配；业务流的实际前缀是
                # soak_流create-modify-remove_*，所以用通配符兜住两种情况
                pats = ["*_trend.csv", "*_summary.json", "*_soak.log", "*.json"]
                dls.append({"dirs": [{
                    "remote": kw["remote_dir"] + "/out/soak",
                    "local": local_soak, "patterns": pats}]})
        self.append_log("[SSH] 目标 %s@%s:%d  目录 %s"
                        % (kw["user"], kw["host"], kw["port"], kw["remote_dir"]))
        self.append_log("[SSH] 将同步 %d 个文件（只传比远端新的）" % len(uploads))
        if self.chk_ssh_nohup.isChecked():
            self.append_log("[SSH] 后台模式：启动后立即返回，结果不会自动下载；"
                            "跑完点「下载远端结果」取回")
        self.worker = SshSoakWorker(
            kw["host"], kw["port"], kw["user"], kw["pwd"], kw["remote_dir"],
            jobs, uploads=uploads, downloads=dls, parent=self)
        self.worker.line.connect(self.append_log)
        self.worker.finished_rc.connect(
            lambda rc: self._on_done(rc, lambda r: self._after_soak(r, names)))
        self.worker.start()

    def _after_soak(self, rc, names):
        self._soak_running = False
        self._sync_soak_button()
        d = os.path.join(OUT_DIR, "soak")
        self.append_log("[提示] 稳定性测试结果目录：%s" % d)
        self.append_log("       trend.csv=每轮趋势 / summary.json=汇总 "
                        "/ soak.log=编排日志 / *_rounds/=异常轮明细")

    # ---- 右栏：连接/身份/输出 + 服务管理 ----
    def _build_right(self):
        """右栏 = 服务管理 + 连接设置/策略平台身份/输出。

        布局参考 datahub_test：左边只管"发什么"，右边放"连哪儿、以谁的身份、
        结果存哪"，以及起停 Mock 服务。这样左栏就是一条从选数据到开跑的主流程。
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(6)

        # 服务管理
        grp = QGroupBox("服务管理（★ 只起这一个就够，再点底部「开始发送」）")
        g = QGridLayout(grp)

        self.lbl_svc_st = QLabel("未启动")
        self.btn_st_svc = QPushButton("启动 Mock 策略平台")
        self.btn_sp_svc = QPushButton("停止")
        self.btn_sp_svc.setEnabled(False)
        self.chk_svc_suffix = QCheckBox("用 _1 后缀")
        self.chk_svc_autoid = QCheckBox("自应答编号")
        self.chk_svc_autoid.setChecked(True)
        self.chk_svc_autoid.setToolTip(
            "勾选=自己占编号直接用（不需要数据中台）；\n"
            "取消=等真/Mock 数据中台分配编号（走完整握手）")
        self.chk_svc_refecho = QCheckBox("回包带回 Ref")
        self.chk_svc_refecho.setChecked(
            ini_get(self.cp, "gui", "svc_refecho", "0") == "1")
        self.chk_svc_refecho.setToolTip(
            "仿真实平台：回包时把请求里的 Ref 原样带回，例如\n"
            "  {\"Ref\":\"20260929000001\",\"Errmsg\":\"ok\",\"ErrID\":0}\n\n"
            "★ 跑「业务流模式」（create→modify→remove）【必须勾上】：\n"
            "  业务流要从 create 的回包抓真实单号，去生成 modify/remove 表。\n"
            "  不勾的话 Mock 只回固定的 {\"status\":\"OK\"}，抓不到单号，\n"
            "  第一步就会失败（提示「create 没有返回任何 Ref」）。\n\n"
            "打真平台时不用管它（真平台本来就带 Ref，且这个开关只影响 Mock）。")
        self.spin_svc_workers = QSpinBox()
        self.spin_svc_workers.setRange(1, 64)
        self.spin_svc_workers.setValue(4)
        self.spin_svc_workers.setMaximumWidth(70)
        self.spin_svc_workers.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.guard.install(self.spin_svc_workers, self.chk_svc_suffix,
                           self.chk_svc_refecho)
        # 左栏的 soak 分区先建好了，这里补接联动（见 _sync_refecho_hint）
        self.chk_svc_refecho.toggled.connect(self._sync_refecho_hint)
        self._sync_refecho_hint()

        g.addWidget(self.lbl_svc_st, 0, 0, 1, 2)
        g.addWidget(self.btn_st_svc, 1, 0)
        g.addWidget(self.btn_sp_svc, 1, 1)
        g.addWidget(self.chk_svc_autoid, 2, 0)
        g.addWidget(self.chk_svc_suffix, 2, 1)
        g.addWidget(self.chk_svc_refecho, 3, 0, 1, 2)
        g.addWidget(QLabel("消费线程"), 4, 0)
        g.addWidget(self.spin_svc_workers, 4, 1)
        lay.addWidget(grp)

        # Mock 数据中台 —— 打真实策略平台时【必起】：真平台重启/清库后会退回
        # 等编号(id=-1)状态，只有它能应答 strategyserver_online 并分配编号。
        # 常规（打自己的 Mock 策略平台）测试才不用起。
        box2 = CollapsibleBox("打真平台必起：Mock 数据中台（给策略平台分配编号）")
        box2.setExpanded(True)
        box2.btn.setToolTip(
            "【什么时候必须起它】\n"
            "策略平台启动后会一直喊 strategyserver_online {\"id\":-1} 要编号，\n"
            "没人应答它就【不建流、不消费】，你发什么都不会有回包。\n"
            "  真平台刚重启 / 清空过 Redis  → 必起\n"
            "  平台已经拿到编号并正常消费    → 不用起（它会自己一直跑）\n\n"
            "【什么时候不用起】\n"
            "打自己的 Mock 策略平台时：mock_strategy 勾了「自应答编号」\n"
            "就自己占号了，不需要中台；而发报文一直是 send_test 干的。")
        grp2 = box2.content
        g2 = QGridLayout(grp2)
        self.lbl_dh_st = QLabel("未启动")
        self.btn_st_dh = QPushButton("启动 Mock 数据中台")
        self.btn_sp_dh = QPushButton("停止")
        self.btn_sp_dh.setEnabled(False)
        self.spin_dh_alloc = QSpinBox()
        self.spin_dh_alloc.setRange(0, 100000)
        # 默认 50：137 真平台历史上用过 ST-0/ST-1，低位号段容易和真平台/历史残留撞车
        self.spin_dh_alloc.setValue(50)
        self.spin_dh_alloc.setMaximumWidth(70)
        self.spin_dh_alloc.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_dh_alloc.setToolTip(
            "从这里开始往后找没被占用的编号分配。\n"
            "分配结果会打印在下面日志里（★ 新策略平台上线 … → 分配编号 N），\n"
            "拿到 N 后要把「策略平台身份 → 分配编号」也改成 N，否则收不到回包！")
        self.guard.install(self.spin_dh_alloc)
        hint = QLabel("只负责「回应上线、分配编号」；发报文用 send_test 或底部按钮。\n"
                      "★ 启动后看日志里的「分配编号 N」，把它填到「策略平台身份」。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#0a5;")
        g2.addWidget(self.lbl_dh_st, 0, 0, 1, 2)
        g2.addWidget(self.btn_st_dh, 1, 0)
        g2.addWidget(self.btn_sp_dh, 1, 1)
        g2.addWidget(QLabel("编号起始"), 2, 0)
        g2.addWidget(self.spin_dh_alloc, 2, 1)
        g2.addWidget(hint, 3, 0, 1, 2)
        lay.addWidget(box2)

        # 连接 / 身份 / 输出（从原左栏搬过来）
        lay.addWidget(self._box_remote())
        lay.addWidget(self._box_conn())
        lay.addWidget(self._box_identity())
        lay.addWidget(self._box_misc())
        lay.addStretch(1)

        scroll.setWidget(inner)
        self.right_scroll = scroll
        return scroll

    # ---- 下栏 ----
    def _build_bottom(self):
        self.tabs = QTabWidget()

        # 日志页 = 一条小工具条 + 日志正文
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)

        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 0)
        self.btn_clear_log = QPushButton("清空日志")
        self.btn_clear_log.setToolTip(
            "只清空这个窗口里的显示内容，不影响 out/logs/ 下的日志文件。\n"
            "任务运行中清空会弹一次确认，免得把正在看的关键输出抹掉。")
        self.btn_clear_log.clicked.connect(self.on_clear_log)
        self.chk_autoclear = QCheckBox("每次发送前自动清空")
        self.chk_autoclear.setToolTip(
            "勾上后，每次点「开始发送 / 预览」会先清空日志，\n"
            "这样每次只看本次输出，不用手动清。")
        self.chk_autoclear.setChecked(
            ini_get(self.cp, "gui", "autoclear_log", "0") == "1")
        self.lbl_logstat = QLabel("")
        self.lbl_logstat.setStyleSheet("color:#888;")
        bar.addWidget(self.btn_clear_log)
        bar.addWidget(self.chk_autoclear)
        bar.addStretch(1)
        bar.addWidget(self.lbl_logstat)
        v.addLayout(bar)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        self.log.document().setMaximumBlockCount(MAX_LOG_LINES)
        # 文本变化时刷新"已清空/行数"提示
        self.log.textChanged.connect(self._update_logstat)
        v.addWidget(self.log)

        self.tabs.addTab(page, "运行日志")

        # 统计页 = 一条工具条 + 表格（与日志页对称）
        spage = QWidget()
        sv = QVBoxLayout(spage)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(2)

        sbar = QHBoxLayout()
        sbar.setContentsMargins(4, 2, 4, 0)
        self.btn_summary_refresh = QPushButton("刷新统计汇总")
        self.btn_summary_refresh.setToolTip(
            "重新扫描 out/performance/*_stats.json，把每次运行的指标汇总到下表。\n"
            "统计文件是 send_test.py 落盘的，这个按钮只是重新读一遍。")
        self.btn_summary_refresh.clicked.connect(self._refresh_summary)
        self.btn_summary_export = QPushButton("导出汇总 Excel")
        self.btn_summary_export.setToolTip(
            "把下表里的所有行导出成一个 Excel（含汇总/按秒/错误三个 sheet）。")
        self.btn_summary_export.clicked.connect(self._export_summary)
        self.lbl_summary_stat = QLabel("")
        self.lbl_summary_stat.setStyleSheet("color:#888;")
        sbar.addWidget(self.btn_summary_refresh)
        sbar.addWidget(self.btn_summary_export)
        sbar.addStretch(1)
        sbar.addWidget(self.lbl_summary_stat)
        sv.addLayout(sbar)

        self.tbl = QTableWidget(0, 15)
        self.tbl.setHorizontalHeaderLabels([
            "标签", "时间", "时长s", "发送", "回包", "失败", "超时",
            "发送/s", "回包/s", "发送MB", "均RT(us)", "P95ms", "P99ms", "CPU%", "错误类"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tbl.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive)
        self.tbl.setColumnWidth(0, 190)
        self.tbl.setAlternatingRowColors(True)
        sv.addWidget(self.tbl)
        self.tabs.addTab(spage, "统计汇总")

        return self.tabs

    # ---- 日志清空 ----
    def _update_logstat(self):
        # 别用 blockCount()-1 估算：QTextEdit 首次 append 不会多出空块，
        # 会算出比实际少 1 的行数（踩过）。直接数非空行最准。
        txt = self.log.toPlainText()
        n = sum(1 for line in txt.splitlines() if line.strip())
        self.lbl_logstat.setText("" if n == 0 else "%d 行" % n)

    def clear_log(self, note=None):
        """清空日志显示（不删 out/logs/ 下的文件）。"""
        self.log.clear()
        if note:
            self.append_log(note)

    def on_clear_log(self):
        if self.worker and self.worker.isRunning():
            if not self._ask("确认", "任务正在运行，清空后本次已输出的日志就看不到了"
                                     "（文件里还有）。\n\n确定清空？"):
                return
            self.clear_log("[提示] 日志已清空（任务仍在运行；完整日志见 out/logs/）")
        else:
            self.clear_log("[提示] 日志已清空（out/logs/ 下的文件不受影响）")

    # ---- 底部按钮 ----
    def _build_actions(self):
        row = QHBoxLayout()
        self.btn_preview = QPushButton("预览报文（不发）")
        self.btn_send = QPushButton("开始发送")
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        for b in (self.btn_preview, self.btn_send):
            b.setMinimumHeight(30)
        self.btn_send.setMinimumHeight(34)
        f = self.btn_send.font()
        f.setBold(True)
        self.btn_send.setFont(f)

        self.btn_preview.clicked.connect(self.on_preview)
        # ★ 必须读「用例类型」下拉框，不能写死 normal：
        # 早先这里是 on_send("normal")，而下拉框只被「预览」读，
        # 于是「选了 destroy 点开始发送」实际发的是 normal（踩过，白发了 5 万条）。
        self.btn_send.clicked.connect(
            lambda: self.on_send(self.combo_type.currentText()))
        self.btn_stop.clicked.connect(self.on_stop)

        # 稳定性测试按钮：只在勾选 soak 且空闲时可点（_sync_soak_button 裁决）
        self.btn_soak = QPushButton("运行稳定性测试")
        self.btn_soak.setMinimumHeight(30)
        self.btn_soak.clicked.connect(self.on_soak)
        self.btn_soak.setToolTip(
            "对勾选的每个接口依次运行 soak_test.py（反复调用 send_test.py）。\n\n"
            "需先在左栏「2. 发送参数 → 稳定性测试」勾选启用并设好参数。\n"
            "结果在 out/soak/：trend.csv（每轮趋势）/ summary.json / *_rounds/（异常轮）。")
        self._sync_soak_button()

        row.addWidget(self.btn_preview)
        row.addStretch(1)
        row.addWidget(self.btn_soak)
        row.addStretch(1)
        row.addWidget(self.btn_send)
        row.addStretch(1)
        row.addWidget(self.btn_stop)
        return row

    def _wire_services(self):
        for svc in (self.svc_strategy, self.svc_datahub):
            svc.line.connect(self._on_svc_line)
            svc.state.connect(self._on_svc_state)
        self.btn_st_svc.clicked.connect(self._start_strategy)
        self.btn_sp_svc.clicked.connect(self.svc_strategy.stop)
        self.btn_st_dh.clicked.connect(self._start_datahub)
        self.btn_sp_dh.clicked.connect(self.svc_datahub.stop)

    # ---------------- 小工具 ----------------
    def append_log(self, text):
        self.log.append(text)
        self.log.moveCursor(QTextCursor.MoveOperation.End)

    def _on_svc_line(self, tag, text):
        self.append_log("[%s] %s" % (tag, text))
        if tag == "数据中台":
            self._sniff_assigned_id(text)

    # mock_datahub 打印格式：
    #   ★ 新策略平台上线 ST-65-...QLDataHub → 分配编号 50（流 ST-50）usecount=0
    _RE_ASSIGNED = re.compile(r"→\s*分配编号\s+(\d+)")

    def _sniff_assigned_id(self, text):
        """从 mock_datahub 的输出里嗅出分配到的编号，自动填进「策略平台身份」。

        这是最容易出错的一步：编号填错 = 报文发到没人读的流上 = 永远没回包，
        而且不报任何错。所以让它自动同步，并明确提示。
        """
        m = self._RE_ASSIGNED.search(text or "")
        if not m:
            return
        sid = int(m.group(1))
        self._assigned_id = sid
        if self.spin_assign.value() != sid:
            self.spin_assign.setValue(sid)
            self._update_conn_summary()
            self.append_log(
                "★ 已自动把「策略平台身份 → 分配编号」同步为 %d（下发流 %s）"
                % (sid, "ST-%d" % sid))

    def _on_svc_state(self, tag, running):
        if tag == "策略平台":
            self.lbl_svc_st.setText("运行中" if running else "未启动")
            self.lbl_svc_st.setStyleSheet(
                "color:#0a0;font-weight:bold;" if running else "color:#888;")
            self.btn_st_svc.setEnabled(not running)
            self.btn_sp_svc.setEnabled(running)
        else:
            self.lbl_dh_st.setText("运行中" if running else "未启动")
            self.lbl_dh_st.setStyleSheet(
                "color:#0a0;font-weight:bold;" if running else "color:#888;")
            self.btn_st_dh.setEnabled(not running)
            self.btn_sp_dh.setEnabled(running)

    def _conn_args(self):
        return ["--host", self.edit_host.text().strip(),
                "--port", str(self.spin_port.value()),
                "--pwd", self.edit_pwd.text(),
                "--db", str(self.spin_db.value())]

    def _suffix(self):
        return "_1" if self.combo_suffix.currentIndex() == 1 else ""

    def _update_conn_summary(self):
        self.lbl_conn.setText(
            "Redis: %s:%d db%d    |    下发流: %s    |    频道后缀: %s"
            % (self.edit_host.text().strip(), self.spin_port.value(),
               self.spin_db.value(), self._stream_name(),
               self._suffix() or "（无）"))
        self._update_case_count()

    def _stream_name(self):
        return "ST-%d" % self.spin_assign.value()

    def _count_pool(self, interface, type_tag, cases_spec):
        """统计某接口在给定筛选下的用例数（走 Excel）。返回 (条数, 错误说明)。"""
        if not CASES:
            return 0, "excel_loader 未加载"
        excel = CASES.default_excel(interface)
        if not os.path.exists(excel):
            return 0, "缺 %s.xlsx" % interface
        want = None if type_tag in ("all", "", None) else {type_tag}
        try:
            pool = CASES.load_cases(excel, want_types=want,
                                    cases_spec=cases_spec, quiet=True)
            return len(pool), ""
        except SystemExit as e:
            return 0, str(e)
        except Exception as e:
            return 0, "%s" % e

    def _update_case_count(self):
        """只显示【当前选择】，不读 Excel。

        【为什么改成不读表】原来每次勾选接口/切类型都会去读 Excel 统计条数，
        而且是 本次 1 遍 + 全库 3 遍 = 4 遍完整扫描。实测 openpyxl 读表很慢：
            3000 行  -> 3.2 s/遍   => 点一下要等 13 秒
            30000 行 -> 32  s/遍   => 点一下要等 160 秒（界面直接卡死）
        而它跑在 Qt 主线程上，会冻住整个窗口。
        现在这里只拼一行文字（零成本），真正的读表推迟到「点击开始发送」时，
        由 send_test.py 在【子进程】里做（界面不会卡）。
        """
        names = self.selected_interfaces()
        t = self.combo_type.currentText()
        spec = self.edit_cases.text().strip()
        if not names:
            self.lbl_cases.setText("未勾选接口")
            self.lbl_cases.setStyleSheet("color:#c00;")
            return
        detail = " ".join("%s" % n for n in names)
        txt = "已选接口: %s ｜ 类型: %s" % (detail, t)
        if spec:
            txt += " ｜ 指定用例: %s" % spec
        txt += "　（条数在点「开始发送」时统计）"
        self.lbl_cases.setText(txt)
        self.lbl_cases.setStyleSheet("color:#555;")

    def _estimate_count(self):
        """点「开始发送」后真正读表统计（会阻塞，放在子线程里做）。

        返回 (总数, 明细文本, 错误文本)。读不到就返回 (0, "", 原因)。
        """
        names = self.selected_interfaces()
        t = self.combo_type.currentText()
        spec = self.edit_cases.text().strip()
        total = 0
        detail = []
        errs = []
        for n in names:
            c, err = self._count_pool(n, t, spec)
            total += c
            detail.append("%s=%d" % (n, c))
            if err:
                errs.append("%s:%s" % (n, err.splitlines()[0][:40]))
        return total, " ".join(detail), "; ".join(errs)

    def selected_interfaces(self):
        return [n for n, c in self.chk_ifaces.items() if c.isChecked()]

    def _pick_stats_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择统计输出目录",
                                             self.edit_stats_out.text().strip() or BASE_DIR)
        if d:
            self.edit_stats_out.setText(d)

    def _open_out_dir(self):
        d = self.edit_stats_out.text().strip() or PERF_DIR
        try:
            os.makedirs(d, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(d)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", d])
            else:
                subprocess.Popen(["xdg-open", d])
        except Exception as e:
            QMessageBox.warning(self, "提示", "打不开目录：%s" % e)

    # ---------------- 组装命令 ----------------
    def _base_cmd(self, script):
        return [PYTHON, os.path.join(BASE_DIR, script)]

    def _send_argv(self, iface, type_tag, preview=False):
        """组装 send_test.py 的参数。preview=True 时加 --no-send。"""
        a = self._base_cmd("send_test.py") + self._conn_args()
        a += ["--assign-id", str(self.spin_assign.value())]
        a += ["--interface", iface]
        if type_tag:
            a += ["--type", type_tag]
        cases = self.edit_cases.text().strip()
        if cases:
            a += ["--cases", cases]
        if preview:
            return a + ["--no-send"]
        a += ["--workers", str(self.spin_workers.value())]
        a += ["--max", str(self.spin_max.value())]
        a += ["--wait", str(self.spin_wait.value())]
        if self.spin_seconds.value() > 0:
            a += ["--seconds", str(self.spin_seconds.value())]
        if self.spin_rate.value() > 0:
            a += ["--rate", str(self.spin_rate.value())]
        if self.spin_sync.value() > 0:
            a += ["--sync-probe", str(self.spin_sync.value())]
        if self.chk_no_reply.isChecked():
            a += ["--no-reply"]
        if self.edit_reply_stream.text().strip():
            a += ["--reply-stream", self.edit_reply_stream.text().strip()]
        if self.edit_label.text().strip():
            a += ["--label", self.edit_label.text().strip()]
        out = self.edit_stats_out.text().strip()
        if out:
            a += ["--stats-out", out]
        if self.chk_quiet.isChecked():
            a += ["--quiet", "1"]
        if self.chk_force_live.isChecked():
            a += ["--force-live"]
        return a

    def _live_foreign(self):
        """只读探测：目标流上有没有【真平台】的消费者。返回列表或 None。"""
        info = self._stream_probe()
        return None if info is None else info[0]

    # ---------------- 运行控制 ----------------
    def _set_running(self, running):
        for b in (self.btn_preview, self.btn_send):
            b.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_summary_export.setEnabled(not running)
        # soak 按钮由 _sync_soak_button 统一裁决（需勾选 soak 且空闲）
        if hasattr(self, "btn_soak"):
            self._sync_soak_button(running)

    def _run(self, cmds, on_done=None):
        self._set_running(True)
        self.worker = Worker(cmds, BASE_DIR, parent=self)
        self.worker.line.connect(self.append_log)
        self.worker.finished_rc.connect(
            lambda rc: self._on_done(rc, on_done))
        self.worker.start()

    def _on_done(self, rc, on_done=None):
        self.append_log("=" * 60)
        self.append_log("[完成] 退出码 %s%s" % (rc, "" if rc == 0 else "  ← 非 0，看上面日志"))
        self._set_running(False)
        self._refresh_summary()
        if on_done:
            on_done(rc)

    def on_stop(self):
        if self.worker and self.worker.isRunning():
            self.append_log("[停止] 正在终止发送任务 ...")
            self.worker.stop()

    # ---------------- 操作 ----------------
    def on_preview(self):
        names = self.selected_interfaces()
        if not names:
            QMessageBox.warning(self, "提示", "请先勾选至少一个接口")
            return
        if self.chk_autoclear.isChecked():
            self.clear_log("[提示] 日志已自动清空（勾了「每次发送前自动清空」）")
        t = self.combo_type.currentText()
        cmds = [self._send_argv(n, t, preview=True) for n in names]
        if len(cmds) > 1:
            self.append_log("[预览] 共 %d 个接口，逐个预览" % len(cmds))
        self._run(cmds)

    def on_send(self, type_tag):
        names = self.selected_interfaces()
        if not names:
            QMessageBox.warning(self, "提示", "请先勾选至少一个接口")
            return
        if not self._confirm_conn():
            return
        if not self._confirm_force_live():
            return
        # 便宜的预检：只看 Excel 文件在不在（不读内容，零成本）。
        # 真正的条数统计交给 send_test.py 在子进程里做，界面不会卡。
        missing = [n for n in names
                   if CASES and not os.path.exists(CASES.default_excel(n))]
        if missing:
            QMessageBox.warning(
                self, "缺少用例表",
                "这些接口的 Excel 还不存在：%s\n\n"
                "先生成：python make_excel.py --interface all"
                % ",".join(missing))
            return
        if not self._confirm_unlimited(names, type_tag):
            return
        self._batch_start = time.time()
        if self.chk_autoclear.isChecked():
            self.clear_log("[提示] 日志已自动清空（勾了「每次发送前自动清空」）")
        self.append_log("")
        self.append_log("#" * 60)
        self.append_log("# 开始%s：接口=[%s] 类型=%s 目标流=%s"
                        % ("破坏测试" if type_tag == "destroy" else "发送",
                           ",".join(names), type_tag, self._stream_name()))
        self.append_log("#" * 60)
        cmds = [self._send_argv(n, type_tag) for n in names]
        self._run(cmds)

    def _confirm_unlimited(self, names, type_tag):
        """（已废弃，保留空实现）

        早先 send_test.py 把 --max 0 当成「不限量」（_producer 里 total=0 让
        break 永不触发），所以这里加了拦截。现已对齐 datahub_test：
        --max 0 = 「当前筛选的用例各发一次」，是安全且最常用的语义，
        不需要也不应该再拦。留着这个空函数只为不破坏调用点。
        """
        return True

    def _confirm_conn(self):
        """发送前确认连接。两台机器上都可能有真平台，靠流名区分（见 _confirm_force_live）。"""
        host = self.edit_host.text().strip()
        db = self.spin_db.value()
        if host == "192.168.1.136":
            r = QMessageBox.question(
                self, "确认连接",
                "目标 Redis 是 %s db%d。\n\n"
                "⚠ 实测 136 db0 上有一套【活的真实环境】：\n"
                "   strategysrv-0 已登记，ST-0 有消费者 ST-0 在实时处理，\n"
                "   回包里带真实订单号（OrderNo）。\n"
                "   往那里发数据可能触发真实交易！\n\n"
                "   想先确认环境可跑：python check_env.py（只读，不写任何数据）\n\n继续？"
                % (host, db))
            return r == QMessageBox.StandardButton.Yes
        if host not in ("192.168.1.137",):
            r = QMessageBox.question(
                self, "确认连接",
                "目标 Redis 是 %s db%d，不是默认的 192.168.1.137。\n\n确认要继续？"
                % (host, db))
            return r == QMessageBox.StandardButton.Yes
        # 137 也不再是"绝对安全"：现场真平台连的就是它（占用某个 ST-<n>）。
        # 但具体撞不撞车取决于编号，交给 _confirm_force_live 按流判定，
        # 这里不弹窗，免得每次发送都烦。
        return True

    def _confirm_force_live(self):
        """发送前把"这是不是真平台"讲清楚，避免三种翻车：
           目标流上【没有任何消费者】     -> 铁定收不到回包，直接拦下（最常见）
           勾了 force-live 却没意识到在打真平台  -> 额外确认
           没勾 force-live 但目标确实是真平台    -> 提前告知会被拦
        """
        armed = self.chk_force_live.isChecked()
        stream = self._stream_name()
        info = self._stream_probe()
        if info is None:             # 探测失败，不干扰
            return True
        foreign, consumers, exists = info

        # ---- 1) 目标流上根本没人读：发出去 100% 没有回包 ----
        # 典型场景：清空 Redis / 平台重启后编号变了，用户还在发旧编号。
        # 但勾了「只发不收」时他本来就不等回包（纯发压测），不该拦。
        if not consumers and not self.chk_no_reply.isChecked():
            actual = self._find_live_stream()
            extra = ("\n\n当前【有平台在消费】的流：%s" % actual) if actual else \
                    "\n\n当前没有任何流有活跃消费者 —— 平台可能还没拿到编号。\n" \
                    "去右栏「启动 Mock 数据中台」给它分配编号。"
            self._warn(
                "⚠ 这条流没人读，收不到回包",
                "目标流 %s 上【没有任何消费者】：\n\n"
                "  流存在 = %s\n\n"
                "说明没有策略平台在读这条流 —— 报文写进去只会堆着，\n"
                "永远不会有回包（而且不会报任何错）。\n"
                "最常见的原因：策略平台拿到的编号和你这里填的不一样。%s"
                % (stream, "是" if exists else "否（还没建）", extra))
            self.append_log("[安全闸] 已取消发送：%s 上没有消费者" % stream)
            return False

        if not foreign:
            if armed:
                self.append_log("[安全闸] 目标流 %s 上未发现外来消费者，"
                                "本次 --force-live 实际未生效" % stream)
            return True

        desc = "\n".join("    %s/%s (idle=%sms pending=%s)"
                         % (f["group"], f["name"], f["idle"], f["pending"])
                         for f in foreign)
        if armed:
            return self._ask(
                "⚠ 确认打真平台",
                "目标流 %s 上有【真实策略平台】的消费者：\n\n%s\n\n"
                "继续 = 给真平台下发假条件单，可能触发真实交易！\n\n"
                "确定要发送吗？" % (stream, desc))

        # 没勾 force-live：告诉他会被拦下，并给出直接改主意的入口
        if self._ask(
                "会被安全闸拦下",
                "目标流 %s 上有【真实策略平台】的消费者：\n\n%s\n\n"
                "未勾选「允许打真平台」，本次发送会被安全闸拦下（退出码 2）。\n\n"
                "要现在就勾上并发送吗？\n"
                "（选「否」= 不发，你可以先改编号，比如把编号换成没被占用的）"
                % (stream, desc)):
            self.chk_force_live.setChecked(True)
            self.append_log("[安全闸] 已勾选「允许打真平台」，本次放行")
            return True
        self.append_log("[安全闸] 已取消发送（目标流上存在真平台消费者）")
        return False

    def _stream_probe(self):
        """只读探测目标流：返回 (外来消费者, 全部消费者名, 流是否存在) 或 None。"""
        try:
            kw = {
                "host": self.edit_host.text().strip() or "192.168.1.137",
                "port": int(self.spin_port.value()),
                "password": self.edit_pwd.text(),
                "db": int(self.spin_db.value()),
            }
            from resp_min import RespClient
            import safety
            c = RespClient(**kw).connect()
            try:
                s = self._stream_name()
                exists = bool(c.cmd("EXISTS", s))
                names = []
                if exists:
                    for g in c.xinfo_groups(s):
                        for cc in c.xinfo_consumers(s, g.get("name")):
                            names.append(str(cc.get("name")))
                return (safety.foreign_consumers(c, s), names, exists)
            finally:
                c.close()
        except Exception as e:
            self.append_log("[提示] 安全探测失败（不影响发送）：%s" % e)
            return None

    def _find_live_stream(self):
        """找出当前【有消费者】的 ST-<n> 流，用于提示"平台其实在几号"。

        返回形如 "ST-50" 的字符串（多个用逗号连接），没有则返回 ""。
        """
        try:
            kw = {
                "host": self.edit_host.text().strip() or "192.168.1.137",
                "port": int(self.spin_port.value()),
                "password": self.edit_pwd.text(),
                "db": int(self.spin_db.value()),
            }
            from resp_min import RespClient
            c = RespClient(**kw).connect()
            try:
                found = []
                for k in sorted(c.keys("ST-*") or []):
                    if k.endswith("-reply"):
                        continue
                    names = []
                    for g in c.xinfo_groups(k):
                        for cc in c.xinfo_consumers(k, g.get("name")):
                            names.append(str(cc.get("name")))
                    if names:
                        found.append("%s(%s)" % (k, ",".join(names)))
                return "，".join(found)
            finally:
                c.close()
        except Exception:
            return ""

    # ---------------- 弹窗（集中在这里） ----------------
    def _warn(self, title, text):
        """告警框（只有一个"确定"）。返回 None。"""
        QMessageBox.warning(self, title, text)

    def _ask(self, title, text, default_no=True):
        """是否确认框。返回 True/False。"""
        btns = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        r = QMessageBox.question(
            self, title, text, btns,
            QMessageBox.StandardButton.No if default_no
            else QMessageBox.StandardButton.Yes)
        return r == QMessageBox.StandardButton.Yes

    def _start_strategy(self):
        a = self._base_cmd("mock_strategy.py") + self._conn_args()
        a += ["--assign-id", str(self.spin_assign.value())]
        a += ["--unique-rand", self.edit_uniq_rand.text().strip() or "761"]
        a += ["--unique-name", self.edit_uniq_name.text().strip() or "test"]
        a += ["--mac", self.edit_mac.text().strip() or "2cea7fd9d5c0"]
        a += ["--usecount", str(self.spin_usecount.value())]
        a += ["--workers", str(self.spin_svc_workers.value())]
        if self.chk_svc_refecho.isChecked():
            # 业务流模式必须开：回包带回请求里的 Ref，soak 才能抓到单号
            a += ["--ref-echo"]
        if self.chk_svc_suffix.isChecked():
            a += ["--channel-suffix", "_1"]
        if self.chk_svc_autoid.isChecked():
            # 自应答：从 assign-id 起找第一个没被占用的 ST-<n>，多开时不撞车
            a += ["--auto-assign-id"]
        else:
            # 不自应答：等真/Mock 数据中台分配编号（走完整握手）
            a += ["--no-assign"]
        ok, err = self.svc_strategy.start(a, BASE_DIR)
        if not ok:
            QMessageBox.warning(self, "启动失败", err)

    def _start_datahub(self):
        a = self._base_cmd("mock_datahub.py") + self._conn_args()
        a += ["--alloc-start", str(self.spin_dh_alloc.value())]
        if self.chk_svc_suffix.isChecked():
            a += ["--channel-suffix", "_1"]
        ok, err = self.svc_datahub.start(a, BASE_DIR)
        if not ok:
            QMessageBox.warning(self, "启动失败", err)

    # ---------------- 统计汇总 ----------------
    def _refresh_summary(self):
        d = self.edit_stats_out.text().strip() or PERF_DIR
        if self.summary_loader and self.summary_loader.isRunning():
            return
        self.summary_loader = SummaryLoader(d, since=0.0, parent=self)
        self.summary_loader.loaded.connect(self._on_summary)
        self.summary_loader.start()

    def _on_summary(self, rows, n, err):
        self._rows = rows
        self.tbl.setRowCount(len(rows))
        keys = ["label", "time", "duration", "sent", "reply", "send_fail",
                "timeout", "send_per_sec", "reply_per_sec", "sent_mb",
                "lat_avg_us", "lat_p95_ms", "lat_p99_ms", "cpu", "n_err"]
        for r, row in enumerate(rows):
            for c, k in enumerate(keys):
                v = row.get(k)
                if isinstance(v, float):
                    if k in ("sent_mb",):
                        s = "%.3f" % v
                    elif k in ("duration",):
                        s = "%.2f" % v
                    elif k in ("send_per_sec", "reply_per_sec"):
                        s = "%.1f" % v
                    elif k in ("lat_avg_us",):
                        s = "%.0f" % v
                    else:
                        s = "%.2f" % v
                else:
                    s = str(v)
                it = QTableWidgetItem(s)
                if k in ("sent", "reply", "send_fail", "timeout", "n_err"):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight |
                                        Qt.AlignmentFlag.AlignVCenter)
                self.tbl.setItem(r, c, it)
        self.tabs.setTabText(1, "统计汇总 (%d)" % len(rows))
        # 顺手显示"上次刷新时间 + 从哪个目录读的"，避免看成空表时不知道是不是没扫对目录
        try:
            d = self.edit_stats_out.text().strip() or PERF_DIR
            self.lbl_summary_stat.setText(
                "%d 条 ｜ %s ｜ %s" % (len(rows), time.strftime("%H:%M:%S"), d))
        except Exception:
            pass
        if err:
            self.append_log("[汇总] %s" % err)

    def _export_summary(self):
        if not self._rows:
            QMessageBox.information(self, "提示", "还没有可导出的统计，先跑一次发送")
            return
        default = os.path.join(self.edit_stats_out.text().strip() or PERF_DIR,
                               "批量汇总_%s.xlsx" % time.strftime("%Y%m%d_%H%M%S"))
        path, _ = QFileDialog.getSaveFileName(self, "导出汇总", default,
                                              "Excel (*.xlsx)")
        if not path:
            return
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font
        except Exception as e:
            QMessageBox.warning(self, "提示", "需要 openpyxl：%s" % e)
            return
        try:
            wb = Workbook()
            ws = wb.active
            ws.title = "汇总"
            hdr = ["标签", "时间", "时长(s)", "发送", "回包", "失败", "超时",
                   "发送/秒", "回包/秒", "发送MB", "回包MB", "均RT(us)",
                   "P95(ms)", "P99(ms)", "最大(ms)", "CPU(进程%)", "错误类数", "文件"]
            ws.append(hdr)
            for c in ws[1]:
                c.font = Font(bold=True)
            for r in self._rows:
                ws.append([r["label"], r["time"], round(r["duration"], 3),
                           r["sent"], r["reply"], r["send_fail"], r["timeout"],
                           round(r["send_per_sec"], 1), round(r["reply_per_sec"], 1),
                           round(r["sent_mb"], 3), round(r["reply_mb"], 3),
                           round(r["lat_avg_us"], 1), round(r["lat_p95_ms"], 3),
                           round(r["lat_p99_ms"], 3), round(r["lat_max_ms"], 3),
                           round(r["cpu"], 2), r["n_err"], r["file"]])
            widths = [22, 15, 9, 9, 9, 7, 7, 10, 10, 9, 9, 10, 9, 9, 9, 11, 9, 30]
            for i, w in enumerate(widths):
                col = ws.cell(row=1, column=i + 1).column_letter
                ws.column_dimensions[col].width = w

            # 每个 run 的按秒明细，各占一个 sheet（最多 20 个，避免 sheet 爆炸）
            for r in self._rows[:20]:
                ser = (r["raw"].get("series") or [])
                if not ser:
                    continue
                name = str(r["label"])[:28] or "run"
                for ch in "[]:*?/\\":
                    name = name.replace(ch, "_")
                sh = wb.create_sheet(name)
                sh.append(["秒", "发送", "回包", "发送字节", "回包字节"])
                for c in sh[1]:
                    c.font = Font(bold=True)
                for x in ser:
                    sh.append([x.get("sec", 0), x.get("sent", 0), x.get("reply", 0),
                               x.get("sent_bytes", 0), x.get("reply_bytes", 0)])
            wb.save(path)
            self.append_log("[导出] %s" % path)
            QMessageBox.information(self, "完成", "已导出：\n%s" % path)
        except Exception as e:
            QMessageBox.warning(self, "导出失败", "%s" % e)

    # ---------------- 关闭 ----------------
    def closeEvent(self, event):
        running = (self.worker and self.worker.isRunning()) or \
                  self.svc_strategy.running or self.svc_datahub.running
        if running:
            r = QMessageBox.question(
                self, "确认退出",
                "还有任务/服务在运行，退出会一并停止。确定？")
            if r != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
        self.svc_strategy.stop()
        self.svc_datahub.stop()
        self._save_ui_state()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei", 9))
    w = MainWindow()
    w.show()
    w._refresh_summary()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
