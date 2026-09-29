# -*- coding: utf-8 -*-
"""
ssh_runner.py —— 通过 SSH 在远程 Linux 上跑稳定性测试
======================================================
为什么需要它：**稳定性测试要跑在 Linux 上**（长时间、不受 Windows 休眠/断网影响，
且与现场环境一致）。本模块负责：

  1. 连接远程（paramiko，密码认证）
  2. 上传脚本 + 数据表（只传比远程新的，避免每次全量）
  3. 远程执行命令，**实时回传 stdout/stderr**
  4. 执行完把结果文件（trend.csv / summary.json / soak.log）下载回本地

参考 datahub_test/gui_test.py 的 SshWorker 实现（那边的 .so 也必须在 Linux 上跑），
差异点：
  * 本模块把「要上传哪些文件」交给调用方（strategy_test 的依赖清单不同）
  * 业务流模式要上传 create/modify/remove 三张表 + interfaces/ 下对应定义
  * 支持 --nohup 后台模式：长稳时启动完立即返回，不占住 SSH 连接

用法（GUI 里勾选「远程 Linux」即可，也可直接命令行调）：

    from ssh_runner import run_remote
    rc = run_remote(host, port, user, pwd, remote_dir, "python3 soak_test.py ...",
                    uploads=[(local, remote), ...],
                    download={...}, on_line=print)
"""
import fnmatch
import os
import posixpath
import time

DEFAULT_SSH_PORT = 22


def paramiko_or_none():
    """延迟导入：没装 paramiko 时给友好提示，而不是 import 阶段就崩。"""
    try:
        import paramiko
        return paramiko
    except ImportError:
        return None


def check_remote(host, port, user, pwd, remote_dir="", timeout=15):
    """只读体检：连通性 + python3 + openpyxl + 到 Redis 的可达性。

    返回 (ok, 多行文本)。GUI 的「测试连接」按钮用它。
    """
    pk = paramiko_or_none()
    if pk is None:
        return False, "缺少 paramiko，请先执行: pip install paramiko"
    lines = []
    try:
        c = pk.SSHClient()
        c.set_missing_host_key_policy(pk.AutoAddPolicy())
        c.connect(host, port=port, username=user, password=pwd, timeout=timeout)
    except Exception as e:
        return False, "连接失败: %s" % e

    def sh(cmd, t=30):
        try:
            _, o, e = c.exec_command(cmd, timeout=t)
            out = o.read().decode("utf-8", "replace").strip()
            err = e.read().decode("utf-8", "replace").strip()
            return out or err
        except Exception as ex:
            return "(执行失败: %s)" % ex

    try:
        lines.append("主机名   : %s" % sh("hostname"))
        lines.append("python3  : %s" % sh("python3 -V 2>&1"))
        lines.append("openpyxl : %s" % sh(
            "python3 -c 'import openpyxl;print(openpyxl.__version__)' 2>&1"))
        lines.append("paramiko : (不需要，远端只用标准库)")
        if remote_dir:
            exists = sh("test -d %s && echo YES || echo NO" % remote_dir)
            lines.append("远程目录 : %s  %s" % (remote_dir,
                                            "已存在" if "YES" in exists else "不存在（首次运行会自动创建）"))
        lines.append("到 Redis : 见下方命令行的 --host 目标（远端执行时检测）")
        return True, "\n".join(lines)
    finally:
        try:
            c.close()
        except Exception:
            pass


def build_nohup_cmd(cmd, name, ts, remote_dir=""):
    """把命令包成 setsid+nohup 后台形式，返回 (完整命令, 日志相对路径)。

    ★ 为什么要 setsid + 重定向三个 fd：
      SSH 通道会因为"还有进程持有 stdout"而不释放。setsid 脱离会话、
      nohup 忽略 HUP、`< /dev/null` 断开 stdin，实测 0.2s 内通道就结束，
      而后台进程照跑。datahub_test 那边验证过的做法。
    """
    logf = "out/soak/soak_%s_%s_nohup.log" % (name, ts)
    full = ("mkdir -p out/soak && setsid nohup %s > %s 2>&1 < /dev/null & disown; "
            "echo '[NOHUP] 已后台启动，日志: %s'" % (cmd, logf, logf))
    return full, logf


class SshSession(object):
    """一次 SSH 会话：连接 → 上传 → 执行 → 下载。

    on_line(text) 回调用来把输出实时喂给 GUI 日志框。
    """

    def __init__(self, host, port=DEFAULT_SSH_PORT, user="", pwd="",
                 remote_dir="", on_line=None, timeout=15):
        self.host = host
        self.port = int(port or DEFAULT_SSH_PORT)
        self.user = user
        self.pwd = pwd
        self.remote_dir = remote_dir.rstrip("/")
        self.on_line = on_line or (lambda s: None)
        self.timeout = timeout
        self._client = None
        self._snap = {}

    # ------------------------------------------------ 基础
    def log(self, msg):
        self.on_line(msg)

    def connect(self):
        pk = paramiko_or_none()
        if pk is None:
            raise RuntimeError("缺少 paramiko，请先执行: pip install paramiko")
        self._client = pk.SSHClient()
        self._client.set_missing_host_key_policy(pk.AutoAddPolicy())
        self._client.connect(self.host, port=self.port, username=self.user,
                             password=self.pwd, timeout=self.timeout)
        self.log("[SSH] 已连接 %s@%s:%s" % (self.user, self.host, self.port))
        return self

    def close(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _sftp(self):
        return self._client.open_sftp()

    def ensure_dir(self, sftp, path):
        """递归建远程目录。"""
        parts = [p for p in path.split("/") if p]
        cur = ""
        for p in parts:
            cur += "/" + p
            try:
                sftp.stat(cur)
            except IOError:
                try:
                    sftp.mkdir(cur)
                except IOError:
                    pass

    # ------------------------------------------------ 上传
    def upload(self, files):
        """files = [(local, remote), ...]；只传本地比远程新的。"""
        if not files:
            return 0
        sftp = self._sftp()
        n = 0
        try:
            self.ensure_dir(sftp, self.remote_dir)
            for local, remote in files:
                if not os.path.exists(local):
                    self.log("[SSH] 跳过（本地不存在） %s" % local)
                    continue
                need = True
                try:
                    st = sftp.stat(remote)
                    need = os.path.getmtime(local) > st.st_mtime
                except IOError:
                    need = True          # 远程没有 → 必须传
                if not need:
                    self.log("[SSH] 跳过上传（已是最新） %s" % os.path.basename(local))
                    continue
                self.ensure_dir(sftp, posixpath.dirname(remote))
                try:
                    sftp.put(local, remote)
                    self.log("[SSH] 已上传 %s" % os.path.basename(local))
                    n += 1
                except Exception as e:
                    self.log("[SSH] 上传 %s 失败: %s" % (os.path.basename(local), e))
        finally:
            sftp.close()
        return n

    # ------------------------------------------------ 执行
    def run(self, command, download=None):
        """远程执行并实时回传输出；返回退出码。

        download: {"dirs": [{"remote":.., "local":.., "patterns":[glob..]}, ...]}
                  执行前记快照，执行后只下载本次新增/更新的（避免全量拉历史）。
        """
        full = "cd %s && %s" % (self.remote_dir, command)
        self.log("$ %s" % full)
        if download:
            self._snap = self._snapshot(download)
        chan = self._client.get_transport().open_session()
        chan.settimeout(0)
        chan.exec_command(full)
        bufs = {"out": "", "err": ""}
        try:
            while True:
                if chan.recv_ready():
                    self._drain(bufs, "out", chan.recv(4096))
                if chan.recv_stderr_ready():
                    self._drain(bufs, "err", chan.recv_stderr(4096))
                if chan.exit_status_ready() and not chan.recv_ready() \
                        and not chan.recv_stderr_ready():
                    while chan.recv_ready():
                        self._drain(bufs, "out", chan.recv(4096))
                    while chan.recv_stderr_ready():
                        self._drain(bufs, "err", chan.recv_stderr(4096))
                    for k in ("out", "err"):
                        if bufs[k].strip():
                            for ln in bufs[k].split("\n"):
                                if ln.strip():
                                    self.log(ln.rstrip("\r"))
                    rc = chan.recv_exit_status()
                    if download:
                        try:
                            self._download(download)
                        except Exception as e:
                            self.log("[SSH] 下载结果失败: %s" % e)
                    return rc
                time.sleep(0.05)
        finally:
            try:
                chan.close()
            except Exception:
                pass

    def _drain(self, bufs, name, data):
        """按行消费缓冲，避免长输出积压。"""
        bufs[name] += data.decode("utf-8", "replace")
        while "\n" in bufs[name]:
            line, bufs[name] = bufs[name].split("\n", 1)
            if line.strip():
                self.log(line.rstrip("\r"))

    # ------------------------------------------------ 下载
    def download_all(self, cfg):
        """无条件下载（不做"只下新增"的快照过滤）。

        ★ 用在「跑完后再取结果」这种场景：那时快照是空的，
          若走 _download 的新旧比较，远程文件会被误判成"历史文件"而跳过。
        """
        self._snap = {}
        self._download(cfg, only_new=False)

    def _snapshot(self, cfg):
        snap = {}
        try:
            sftp = self._sftp()
        except Exception:
            return snap
        try:
            for d in cfg.get("dirs", []):
                try:
                    for a in sftp.listdir_attr(d["remote"]):
                        snap["%s#%s" % (d["remote"], a.filename)] = a.st_mtime
                except IOError:
                    pass        # 远程目录还不存在（第一次跑）很正常
        finally:
            sftp.close()
        return snap

    def _download(self, cfg, only_new=True):
        sftp = self._sftp()
        got = skipped = 0
        try:
            for d in cfg.get("dirs", []):
                rd, ld = d["remote"], d["local"]
                pats = d.get("patterns", [])
                os.makedirs(ld, exist_ok=True)
                try:
                    files = sftp.listdir_attr(rd)
                except IOError:
                    self.log("[SSH] 远程目录不存在，跳过: %s" % rd)
                    continue
                for a in sorted(files, key=lambda x: x.st_mtime, reverse=True):
                    if not any(fnmatch.fnmatch(a.filename, p) for p in pats):
                        continue
                    if only_new:
                        old = self._snap.get("%s#%s" % (rd, a.filename))
                        if old is not None and a.st_mtime <= old + 0.001:
                            skipped += 1
                            continue
                    try:
                        sftp.get("%s/%s" % (rd, a.filename),
                                 os.path.join(ld, a.filename))
                        self.log("[SSH] 已下载结果: %s" % os.path.join(ld, a.filename))
                        got += 1
                    except Exception as e:
                        self.log("[SSH] 下载 %s 失败: %s" % (a.filename, e))
            if got == 0:
                self.log("[SSH] 无本次新增结果文件"
                         + ("（跳过历史文件 %d 个）" % skipped if skipped else
                            "（远程可能还没产出结果）"))
        finally:
            sftp.close()


def run_remote(host, port, user, pwd, remote_dir, command,
               uploads=None, download=None, on_line=None, timeout=15):
    """一次性完成：连接 → 上传 → 执行 → 下载 → 关闭。返回退出码（失败 -1）。"""
    s = SshSession(host, port, user, pwd, remote_dir, on_line, timeout)
    try:
        s.connect()
        if uploads:
            s.upload(uploads)
        return s.run(command, download)
    except Exception as e:
        s.log("[SSH][ERROR] %s" % e)
        return -1
    finally:
        s.close()
