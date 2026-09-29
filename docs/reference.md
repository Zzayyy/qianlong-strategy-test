# 参考手册

参数速查、文件结构、指标口径。使用流程见 [guide.md](guide.md)。

---

## 1. send_test.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `--interface` | 配置(create) | `create` `modify` `remove` `pwdUpdate` `account` `all` |
| `--type` | 配置(normal) | `normal` `destroy` `all` |
| `--cases` | 空 | 按编号/行号筛选。分隔符 `,` `;` 空格可混用；带字母按「用例编号」匹配（忽略大小写与前导零，如 `C201`/`CB00001`），纯数字按数据行号（1 起始）。写它自动放宽 `--type`。**写错会报错退出** |
| `--assign-id` | 配置(1) | 目标流 = `ST-<id>` |
| `--stream` | 空 | 直接指定流名，覆盖 `--assign-id` |
| `--workers` | 配置(4) | 并发线程数 |
| `--max` | 配置(0) | 见下方说明 |
| `--seconds` | 0 | 按时间跑（优先于 `--max`）。填了它就是**不限量** |
| `--rate` | 0 | 全局限速 条/秒，0=不限 |
| `--wait` | 配置(5) | 发完等回包秒数 |
| `--no-reply` | 关 | 不读回包（纯发） |
| `--reply-stream` | 空 | 回包流名，默认 `DataHub_reply_stream` |
| `--sync-probe` | **0（关闭）** | 压测后单发单收 N 次测链路真实 RTT。⚠ 会**额外真实写入 N 条**、不计入统计。要测再填，建议 1~3 |
| `--refs-out` | 空 | 发 create 时把回包里的真实单号写成 JSON，供 `make_excel --ref-map` 用 |
| `--no-refs-out` | 关 | 不写上面那个 refs JSON |
| `--no-send` | 关 | 只预览报文 |
| `--list-cases` | 关 | 只列出用例 |
| `--dump` | 空 | 预览时把报文写 jsonl |
| `--quiet` | 1 | 1=安静 |
| `--no-stats` / `--stats-out` / `--stats-interval` | 开/`out/performance`/1.0 | 统计开关、输出目录、采样间隔 |
| `--force-live` | 关 | 跳过安全闸（发现外来消费者也照发） |

### `--max` 的语义（与 `datahub_test` 对齐）

| 取值 | 行为 | 例子 |
|---|---|---|
| `0`（或不填） | 筛选出的用例**各发一次** | `account --type destroy` → 发 96 条就结束 |
| `< 用例数` | 只发前 N 条 | `--max 5` → 发 5 条 |
| `> 用例数` | 循环复用凑够 N 条 | `--max 200` → 96 条用例循环，发满 200 条 |

只有 **`--seconds > 0`** 才是"不限量"。

> **响应时间有两套口径**：`send_test.py` 是"多线程狂发 + 单读取线程收"，
> 读到的延迟含读取线程攒批等待。`--sync-probe N` 另给干净的链路 RTT。

---

## 2. make_excel.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `--interface` | `all` | 接口名或 `all` |
| `--list` | 关 | 只列出各接口用例数，不生成 |
| `--bulk-normal` | 0 | 五个接口都支持：normal 段换成 N 行**不同账号**的压测数据 |
| `--bulk-start` | 0（=11301） | 批量账号的 6 位序号起点 |
| `--ref-map` | 空 | ★**推荐**：读 `send_test` 落盘的 `refs.json` 回填**真实单号** |
| `--ref-seq` | 1 | 备选：靠行序对齐时用，当天已建过 N 张就填 N+1 |
| `--ref-spec` | 空 | **仅 modify/remove**：按单号区间生成引用行，如 `7,100` |

用法与数据规则见 [guide.md](guide.md) 第 6 节。

---

## 3. mock_strategy.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `--assign-id` | 配置(1) | 数据中台分配的编号 / 下发流 `ST-<id>` |
| `--auto-assign-id` | 关 | 从 `--assign-id` 起找第一个未被占用的流（多开防撞车） |
| `--no-assign` | 关 | 不自己应答上线，等真中台分配编号 |
| `--no-reply` | 关 | 只收不回（观察模式） |
| `--reply-stream` | `auto` | `auto`=DataHub_reply_stream、`reply`=ST-N-reply、`both` |
| `--reply-data` | `{"status":"OK"}` | 回包内容（固定串） |
| `--ref-echo` | 关 | 回包时把请求里的 `Ref` 原样带回（仿真实平台）。<br>★ 跑 `--flow` 业务流**必须开**：soak 要从 create 回包抓真实单号，固定回包抓不到 |
| `--read-count` | 100 | 每次取多少条。真插件是 **1**；用 1 会让 mock 成为瓶颈（实测仅 ~475 条/秒） |
| `--channel-suffix` | 空 | 现场有带 `_1` 与不带后缀两套；匹配现场真中台用 `--channel-suffix _1` |
| `--workers` | 配置(4) | 消费线程数 |
| `--unique` / `--unique-rand` / `--unique-name` / `--mac` | 自动拼 | 上线身份 |
| `--online-interval` / `--beat-interval` | 2.0 / 5.0 | 上线重发 / 心跳间隔 |
| `--seconds` | 0 | 运行秒数，0=一直跑 |
| `--force-live` | 关 | 跳过安全闸 |

---

## 4. mock_datahub.py（○ 可选，常规测试用不到）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--alloc-start` | 配置(1) | 分配编号起始值 |
| `--observe-only` | 关 | 只观察，不分配编号/不发心跳（看真中台给谁分了号） |
| `--channel-suffix` | 空 | 默认**同时监听**两套后缀；指定后只听一套 |
| `--beat-interval` | 10.0 | 给策略平台发心跳的间隔 |

> 本脚本**只负责**「回应上线、分配编号、发心跳」。要造业务报文请用 `send_test.py`。

**为什么它是可选的**：`send_test.py` 直接往 `ST-N` 写报文，本身就承担了
"数据中台发报文"的角色；而"分配编号"这件事，`mock_strategy.py` 用
`--assign-id N` 自应答即可绕过。`mock_datahub` 唯一不可替代的是
**回应上线、分配编号**那一段。

完整上线握手（三进程）：

```bash
# 终端1：Mock 数据中台（负责分配编号）
python mock_datahub.py --host 192.168.1.137 --db 0 --alloc-start 1
# 终端2：Mock 策略平台（--no-assign = 等中台分配编号）
python mock_strategy.py --host 192.168.1.137 --db 0 --no-assign
# 终端3：发报文
python send_test.py --host 192.168.1.137 --db 0 --assign-id 1 --type normal --max 5000
```

这套也用于**观察真数据中台**：`mock_datahub --observe-only` 只旁听不应答。
匹配现场真中台（带 `_1` 后缀）加 `--channel-suffix _1` 即可。

---

## 5. soak_test.py

用法与判据说明见 [guide.md](guide.md) 第 7 节。

### 编排参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--interface` | 空 | 接口名（**单接口模式必填**；`--flow` 时忽略） |
| `--flow` | 关 | **业务流模式**：一组 = `create → modify → remove` 各 `--batch` 条，循环跑。每轮 modify/remove 表用本轮 create 回包抓到的真实单号重新生成 |
| `--type` | `normal` | 用例类型，**同时决定回复率的默认档位**（见下）。`--flow` 只能配 `normal` |
| `--assign-id` / `--stream` | 必填其一 | 目标流 `ST-<id>` / 直接指定流名 |
| `--hours` | 8.0 | 总时长（小时） |
| `--rounds` | 0 | 按轮数跑；指定后忽略 `--hours`（短测用）。`--flow` 下是"组数" |
| `--batch` | 500 | **每轮/每段条数**（`--flow` 时三段各发这么多） |
| `--gap` | 0.0 | 轮间间隔秒 |
| `--rotate` | 关 | 每轮换一批用例（按**本类型的行号**分段，末尾回绕）。⚠ 与 `--flow` 互斥 |
| `--clean` | `monitor` | `monitor`=只监控；`per-round`=每轮清回包流（⚠ 该流是多条 `ST-*` 共用，非独占环境别开；`--flow` 只允许 `monitor`） |
| `--keep-round-stats` | 关 | 保留每轮 stats（默认只留异常轮） |
| `--round-timeout` | 600 | 单轮最长秒数，防卡死 |
| `--soak-out` | `out/soak` | 输出根目录 |

### 判据参数（0 / 负值 = 不判）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--min-reply-rate` | **按类型** | 回复率下限%。不给就按 `--type` 取：`normal`=99，`error`/`destroy`/`all`=0。显式给 0 = 不判 |
| `--max-lag` | 0 | 目标流 `lag` 超它算异常（平台**不读**的信号） |
| `--max-pending` | 0 | 目标流未ACK 超它算异常（平台**读了卡住**的信号） |
| `--max-timeout-reply` | 0 | 单轮「超时未回」超它算异常 |
| `--max-outstanding` | 0 | 单轮结束时「在途未回」超它算异常 |

> `--min-reply-rate` 的分子只算**能对上本次 `request_id`** 的回包，
> 所以恒 ≤ 100%。共用回包流上别人的回复不影响它（记进 trend 的「非本次回包」列）。
>
> ⚠️ `destroy` 的回包行为**不稳定**（实测同一天出现过 0% 和 100%），
> 给它设回复率下限要谨慎。详见 [dev-notes.md](dev-notes.md) 第 2.3 节。

未识别参数原样**透传**给 `send_test.py`（如 `--workers` / `--wait` / `--rate` /
`--no-reply` / `--force-live` / `--quiet` / `--sync-probe`）。

---

## 6. 用真插件当策略平台（可选，仅用于复核协议）

**常规测试完全不需要 `.so`。** 本目录没有任何文件加载 `.so` —— 插件原本的职责
（上线、心跳、收 `ST-N`、回包）已全部由 `mock_strategy.py` 用纯 socket 实现。
`.so` 只在一种场景有用：**拿真插件当"标准答案"，验证 mock 仿真得对不对**。

`.so` 是 Linux 库，只能在 136 上跑（`libdatahub_strategy_plug.so` 已在
`/home/yangsh/so_test/strategy/`）。

> **踩坑提醒：`.so` 读的是 `DataHub.ini`，不是 `config.ini`。**
> 插件的 Redis 地址来自它旁边的 `DataHub.ini`（`REDISHOST/REDISPORT/REDISSELECT`），
> 本目录 `config.ini` 只管本工具。两边不一致时插件和 mock 会各连各的 Redis，
> 表现为**插件一直拿不到编号、ST-N 永不出现**。

```bash
# 136 上：先确认/改好 DataHub.ini（应指向 137），再跑插件
cd /home/yangsh/so_test/strategy
grep -E 'REDISHOST|REDISPORT|REDISSELECT' DataHub.ini
python3 strategy_client.py --so ./libdatahub_strategy_plug.so \
    --unique test --autoreply 1 --reply-data '{"status":"OK"}' --wait 75

# 本机：给它分配编号
python mock_datahub.py --host 192.168.1.137 --db 0 --alloc-start 1
```

---

## 7. 文件结构

| 文件 | 说明 |
|---|---|
| `gui_test.py` | **图形界面（PySide6）**，封装下面这些命令行工具 |
| `send_test.py` | ★**必起** — 手动 XADD 发送器 + 性能统计（扮演"数据中台发报文"） |
| `mock_strategy.py` | ★**必起** — 模拟策略平台（收 ST-N、回包、上线+心跳） |
| `soak_test.py` | 稳定性测试编排 |
| `make_excel.py` | **用例生成器**：按 `interfaces/` 定义生成 `data/{接口}.xlsx` |
| `excel_loader.py` | 读 Excel 用例（口径与 `datahub_test` 一致） |
| `interfaces/` | **接口定义**（五个接口）+ `_common.py` |
| `data/` | **生成的 Excel 用例**（勿手改后忘记重生成） |
| `perf_stats.py` | 吞吐 / 字节 / CPU / 延迟分位 / 落盘 JSON+Excel |
| `pwd_encode.py` | Pwd 字段加密（见 [guide.md](guide.md) 第 5 节） |
| `protocol.py` | 协议常量与报文构造 |
| `safety.py` | 安全闸判据（`send_test` / `mock_strategy` 共用） |
| `resp_min.py` | 纯 socket 的 Redis RESP 客户端（不依赖 redis-py，Linux 3.9 也能跑） |
| `check_env.py` | 只读环境体检（连之前先跑它） |
| `mock_datahub.py` | ○**可选** — 模拟数据中台，仅在「测完整上线握手」或「观察真中台」时需要 |
| `config.py` `config.ini` | 共享配置（CLI 参数优先） |
| `tests/` | 辅助工具（不是测试） |
| `out/logs/` `out/performance/` `out/soak/` | 运行日志、性能统计、稳定性测试输出 |
| `docs/` | 使用指南、参数手册、[开发备忘](dev-notes.md) |

---

## 8. 输出与指标口径

| 产物 | 内容 |
|---|---|
| `out/logs/{label}.log` | 完整运行日志（只记条数/统计，**不含报文内容**） |
| `out/performance/{label}_stats.json` | 汇总指标 |
| `out/performance/{label}.xlsx` | 汇总 + 按秒明细 + 错误分布（无 openpyxl 时退化成 CSV） |

指标口径：每秒发送/回包条数、请求/回包字节数（按秒）、客户端 CPU 利用率、
平均响应时间（微秒）＋ P50/P90/P95/P99/Max、错误分布。

稳定性测试产物（`out/soak/`）见 [guide.md](guide.md) 第 7 节。
