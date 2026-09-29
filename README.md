# Strategy 策略平台测试工具

**数据中台 → 策略平台**方向的测试工具集：手动 `XADD` 造报文压策略平台，
自带 Mock 策略平台顶替（真平台未开放时也能先联调）。

姊妹项目：[`../datahub_test/`](../datahub_test/)（委托服务器 → 数据中台，方向相反）。

## 快速开始

需要 Python 3.8+，直接用仓库根目录的 `venv`（已装 PySide6 + openpyxl）。

```powershell
cd strategy_test
..\venv\Scripts\python.exe gui_test.py
```

**图形界面（推荐）**，打开后按顺序：

1. 右栏「服务管理」→ **启动 Mock 策略平台**
2. 左栏「1. 测试数据」→ 勾 `create`
3. 左栏「2. 发送参数 → 发送范围」→ 用例类型选 `normal`
4. 左栏「2. 发送参数 → 规模与速率」→ 并发 8、总条数 2000
5. 底部 **开始发送**

界面参数会记忆到 `config.ini`，跑完自动切到「统计汇总」，可一键导出 Excel。

**命令行**（开两个终端，都先 `cd strategy_test`）：

```powershell
# 终端 1：Mock 策略平台（占编号 1 → 下发流 ST-1）
..\venv\Scripts\python.exe mock_strategy.py --host 192.168.1.137 --db 0 --assign-id 1

# 终端 2：8 线程发 2000 条正常单，并测真实 RTT
..\venv\Scripts\python.exe send_test.py --host 192.168.1.137 --db 0 --assign-id 1 `
    --interface create --type normal --workers 8 --max 2000 --wait 8
```

产物落在 `out/logs/`（完整日志）与 `out/performance/`（统计 JSON + Excel）。

## ⚠️ 安全警告（先读）

本工具会往目标 Redis **真实写入** `ST-<id>` / `DataHub_reply_stream`，
且**默认目标 `192.168.1.137 db0` 就是现场真平台所在的库**。开跑前务必：

* **不要往 136 的 Redis 写**（同事在那边测别的）。
* 目标流上有**真平台消费者**时（consumer 名 = 流名本身，如 `ST-50`），
  `send_test.py` 会**拒绝发送**（退出码 2）；GUI 提前弹窗，放行要勾
  「允许打真平台」并再确认一次（**默认关，且故意不记忆**）。
* 联调真平台先跑只读体检：`python check_env.py`。
* 别在真平台占用的编号上起 `mock_strategy.py` —— Streams 同组是
  **负载均衡不是广播**，mock 会**抢走真平台一半的消息**。
* 测完清掉自己造的号段流（**真平台在用的那条一个都不能碰**）。

已知真实环境、风险反转（Redis 从 136 切到 137）等细节见
**[docs/guide.md](docs/guide.md)** 的「打真平台」一节。

## 协议要点

| 事项 | 结论 |
|---|---|
| 上线频道 | `strategyserver_online`（现场真中台订的是带 `_1` 后缀那套） |
| **下发流名** | `ST-<数据中台分配的编号>`，**不是** unique_string |
| 消费组名 | 固定 `user_group` |
| consumer 名 | 流名本身（`ST-7`）；本工具 mock 用 `ST-7-w0/w1/...` |
| 回包流 | `DataHub_reply_stream`（插件虽建了 `ST-7-reply` 但从不写它） |
| MsgType | `4`=create `8`=remove `11`=modify `17`=pwdUpdate `18`=account |
| 心跳 | 策略→中台 每 5s；中台→策略 约每 10s |

完整时序与实测对照见 [docs/dev-notes.md](docs/dev-notes.md) 第 5 节。

## 文档

| 文档 | 内容 |
|---|---|
| **[docs/flow-soak-tutorial.md](docs/flow-soak-tutorial.md)** | 🆕 **业务流稳定性测试完整教程**（create→modify→remove 全流程 + 跑 Linux/nohup） |
| **[docs/guide.md](docs/guide.md)** | 怎么用：GUI 逐项操作、命令行变体、看报文、打真平台、Pwd 加密、用例与批量数据、稳定性测试（含**跑 Linux**）、异常排查 |
| **[docs/reference.md](docs/reference.md)** | 查参数：四个脚本的全部参数、文件结构、输出指标口径 |
| **[docs/dev-notes.md](docs/dev-notes.md)** | 改代码前必读：硬约束、踩坑记录、判据由来、协议验证证据 |

## 文件结构（摘要）

| 文件 | 说明 |
|---|---|
| `gui_test.py` | **图形界面（PySide6）**，封装下面这些命令行工具 |
| `send_test.py` | ★**必起** — 手动 XADD 发送器 + 性能统计 |
| `mock_strategy.py` | ★**必起** — 模拟策略平台（收 `ST-N`、回包、上线+心跳） |
| `soak_test.py` | 稳定性测试编排（连续跑几小时看指标劣化） |
| `ssh_runner.py` | SSH 远程执行（稳定性测试跑 Linux 用：上传/执行/回传/下载） |
| `make_excel.py` | **用例生成器**：按 `interfaces/` 定义生成 `data/{接口}.xlsx` |
| `interfaces/` | **接口定义**（五个接口）+ `_common.py` |
| `check_env.py` | **只读**环境体检（连之前先跑它） |
| `tests/` | 辅助排查工具（不是测试） |
| `out/` | 运行日志、性能统计、稳定性测试输出 |

完整表（含 `pwd_encode.py` / `safety.py` / `resp_min.py` / `mock_datahub.py` 等）
见 [docs/reference.md](docs/reference.md)。
