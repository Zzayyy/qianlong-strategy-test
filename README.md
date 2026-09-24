# Strategy 策略平台测试工具

针对**数据中台 → 策略平台**方向的测试工具集。

之前做的是*委托服务器 → 数据中台*（见 `../datahub_test/`）。这次方向反过来：
数据中台主动把条件单推给策略平台，策略平台收下、处理、回包。
但**策略平台暂未开放**，所以本目录提供一个 Mock 策略平台顶替它。
另外以前靠 MQ 插件发送，现在插件没了，**一律手动 XADD**。

---

## 0. 怎么跑（先看这段）

需要 Python 3.8+。本目录用 `venv`（`D:\Code\Python\多线程\venv`）即可，它已装好 openpyxl。

### 0.1 两行命令跑起来（最常用）

开**两个**终端，都先 `cd strategy_test`：

```powershell
# 终端 1：模拟策略平台（占编号 1，即下发流 = ST-1）
..\venv\Scripts\python.exe mock_strategy.py --host 192.168.1.137 --db 0 --assign-id 1

# 终端 2：手动 XADD 发报文（8 线程发 2000 条正常单，并测真实 RTT）
..\venv\Scripts\python.exe send_test.py --host 192.168.1.137 --db 0 --assign-id 1 `
    --interface create --type normal --workers 8 --max 2000 --wait 8 --sync-probe 20
```

不用 `cd` 的话，把 `..\venv\Scripts\python.exe` 换成绝对路径
`D:\Code\Python\多线程\venv\Scripts\python.exe` 即可。

跑完终端 2 会打印统计表，并在 `out/` 下留三份产物：

```
out/logs/quickstart.log                      # 完整日志
out/performance/quickstart_stats.json        # 汇总指标
out/performance/quickstart.xlsx              # 汇总 + 按秒 + 错误（3 个 sheet）
```

实测参考值（2026-09-24）：

```
发送 2000 条 / 8 线程 → 1238 条/秒，回包 2000/2000，失败 0，在途 0
批量口径响应时间 均 7.3ms；同步口径（链路真实 RTT）均 1.1ms
```

### 0.2 破坏测试

```powershell
# 先预览要发什么（不发）
..\venv\Scripts\python.exe send_test.py --type destroy --no-send

# 真发：1000 条破坏用例，8 线程
..\venv\Scripts\python.exe send_test.py --host 192.168.1.137 --db 0 --assign-id 1 `
    --type destroy --workers 8 --max 1000 --wait 8
```

### 0.3 不用手开两个终端：一键自测

```powershell
..\venv\Scripts\python.exe _e2e_test.py      # 5 发 5 回，核对 request_id 与回包
..\venv\Scripts\python.exe _e2e_destroy.py   # normal 2000 + destroy 1500
```

这两个脚本会自己把 mock 拉起来、跑完自己收掉。

### 0.4 常用变体

```powershell
# 只发不收回包（纯压发送端）
... send_test.py --assign-id 1 --no-reply --workers 8 --seconds 10

# 按时间跑 10 秒
... send_test.py --assign-id 1 --seconds 10 --workers 8

# 限速 100 条/秒
... send_test.py --assign-id 1 --rate 100 --max 1000

# 只发指定的破坏用例
... send_test.py --assign-id 1 --cases D001,D005,D008

# 列出所有用例
... send_test.py --list-cases --type all
```

> 目标 Redis 默认 `192.168.1.137 db0`。要改就编辑 `config.ini`，
> 或每次都带 `--host/--db`。**不要往 136 的 Redis 写**（同事在那边测别的）。

---

## 1. 协议（实测确认，不是抄文档）

下面每一条都是在 `192.168.1.136` 上**跑真的 `libdatahub_strategy_plug.so`
＋ Redis `MONITOR` 抓包**验证出来的。文档和实际情况有出入，以本节为准。

### 1.1 完整时序

```
【1】策略平台上线（策略→中台），每 2 秒重发直到拿到编号
    PUBLISH strategyserver_online
    {"id":-1,"unique_string":"ST-761-2cea7fd9d5c0test",
     "ip":"192.168.1.136","mac":"2cea7fd9d5c0test","usecount":1}
    同时插件 SUBSCRIBE 自己的 unique_string

【2】数据中台分配编号（中台→策略）
    PUBLISH <unique_string> {"id":7,"dataHubString":"test"}
    插件收到后回调 id=1，data1='7'（编号）
    紧接着插件再 PUBLISH 一次 strategyserver_online，这次 id=7（上线确认）

【3】建流（策略平台自己做）
    XGROUP CREATE ST-7       user_group 0 MKSTREAM
    XGROUP CREATE ST-7-reply user_group 0 MKSTREAM

【4】下发业务报文（中台→策略）
    XADD ST-7 * request_id <rid> task <json>

【5】策略平台收报文
    XREADGROUP GROUP user_group ST-7 BLOCK 100 COUNT 1 STREAMS ST-7 ST-7-reply > >

【6】策略平台回包（策略→中台）
    XADD DataHub_reply_stream * request_id <rid> task {"status":"OK"}
    XACK ST-7 user_group <条目ID>

【7】心跳（策略→中台），每 5 秒
    PUBLISH strategyserver_beat {"id":7,"unique_string":"...",...,"usecount":1}

【8】中台心跳（中台→策略），约每 10 秒
    PUBLISH <unique_string> {"id":1,"dataHubString":"test1"}

【9】下线（策略→中台）
    PUBLISH strategyserver_offline {"id":7}
```

### 1.2 与「数据中台to策略平台.txt」逐条对照

**结论：文档与实测一致。** 你同事文档里写的频道名和流程都对，实测可以逐条对上。

| 文档条目 | 实测 |
|---|---|
| 4 `strategyserver_online` | ✅ 一致。策略插件首发 `{"id":-1,"unique_string":"ST-761-<mac><name>",...,"usecount":1}`；中台回 `PUBLISH <unique_string> {"id":41,"dataHubString":"test"}`，插件回调确认拿到编号 41 |
| 5 `strategyserver_beat` 心跳 | ✅ 一致。拿到编号后每 5s 发 `{"id":41,"unique_string":"ST-761-..",...,"usecount":1}` |
| 6 `strategyserver_offline {"id":1}` | ⚠️ 文档有，但实测**抓不到**：`DestroyMQ` 不返回、SIGTERM 也不发。策略 `.so` 里有 `"publish offline_data error,"` 错误串（说明有 offline 分支），但没有委托 `.so` 里那两个 `offline` / `createOfflineJsonStr` 符号。判为**逻辑存在但未能触发**，本工具仍照发 |
| 9 中台→在线服务器心跳 | ✅ 一致。`PUBLISH <unique_string> {"id":1,"dataHubString":"test1"}`，约每 10s |
| 7/8 `datahub_online` / `datahub_beat` | ✅ 现场存在，抓到 `datahub_beat_1 {"level":2,"role":1}` |

你同事的两点补充也**都对**：
> "监听 strategyserver_online，取里面的 unique_string 作为回编号的 CHANNEL"
> "之后心跳包会发到 strategyserver_beat"

第一点正是 `mock_datahub.py` 的核心逻辑；第二点实测确认。

**补充一点文档没写、但现场存在的细节**（不是矛盾，是两套并存）：

```
不带后缀  strategyserver_online     ← 插件(.so) 发的是这套
带 _1     strategyserver_online_1   ← 现场真数据中台订阅的是这套
```

`PUBSUB NUMSUB` 实测（136 本地 Redis）：不带后缀的全是 **0 个订阅者**，带 `_1` 的各 **1 个**
（`CLIENT LIST` 显示 192.168.1.137 连 db1、`sub=11`，正是整套 `_1` 频道）。
`tradeserver_*` / `datahub_*` 同规律。

我另外验证了：这个后缀**与 `DataHub.ini` 的 `REDISSELECT` 无关** —— 依次设成 0/1/2，
插件都仍连 db0、都仍发不带后缀的名字（MONITOR 确认）。

⚠️ 但要说清楚：我分别往**两个**频道都发过格式完全一致的上线报文，**都没收到分配编号回复**。
所以"没应答"不能只归因于后缀，更可能就是策略平台方向尚未开放（真中台在 137 上仍正常发
`datahub_beat_1`，说明它本身是活的）。两条路都测了，这点可以放心。

用 `--channel-suffix _1` 可切到带后缀的一套。

### 1.3 关键结论（容易踩坑的点）

| 事项 | 实测结论 |
|---|---|
| 上线频道名 | `strategyserver_online`，**不带** `_<REDISSELECT>` 后缀。把 `DataHub.ini` 的 `REDISSELECT` 从 0 改成 1，频道名不变。（136 上另有 `strategyserver_online_1` 在跑，是旁路，不用管） |
| **下发流名** | `ST-<数据中台分配的编号>`，**不是** unique_string。文档说"编号 1 → 流 ST-1"，含义是分配 id=1 就读 ST-1 |
| 消费组名 | 固定 `user_group` |
| consumer 名 | 流名本身（`ST-7`）；本工具用 `ST-7-w0/w1/...` 区分多线程 |
| 回包流 | `DataHub_reply_stream`（MONITOR 实证）。插件虽建了 `ST-7-reply` 但从不写它 |
| `ST-<id>-reply` 里是什么 | 历史上有 4553 条 `{"Err":0,"Msg":"insert trade success"}`，消费者是 `user_group`/`ST-0` —— 看起来是**中台写给策略**的应答流，不是策略写给中台的 |
| MsgType | 实测 37567 条里只有 `4`(create) 和 `18`(Account)；协议另有 `8`(remove) `11`(modify) `17`(pwdUpdate) |
| task 键顺序 | 真实流量是 `{"create":{...},"MsgType":4}`（**create 在前**），文档写的是反的 |
| 真中台当前状态 | 对 `strategyserver_online` **完全无应答**（跑真插件 14 次上线、0 回调）→ 符合"策略平台暂未开放" |

### 1.4 响应时间的两套口径

`send_test.py` 是"多线程狂发 + 一个读取线程收"，读到的延迟**含读取线程攒批等待**，
并发高时明显大于单条真实往返。所以另提供 `--sync-probe N`：压测后单发单收 N 次，
给出干净的链路 RTT。实测同一环境下：

```
批量口径（8线程/3000条）：平均 5.17 ms
同步口径（单发单收 30 次）：平均 0.84 ms   ← 链路真实往返
```

---

## 2. 文件结构

| 文件 | 说明 |
|---|---|
| `protocol.py` | **协议常量与报文构造**，每一条都注明实测来源；文件头含「与文档逐条对照」结论 |
| `resp_min.py` | 纯 socket 的 Redis RESP 客户端（不依赖 redis-py，Linux 3.9 也能跑） |
| `cases.py` | 用例库：normal + destroy，含 token 展开（超长/控制字符/SQL/XSS…） |
| `mock_strategy.py` | **模拟策略平台**（收 ST-N、回 DataHub_reply_stream、上线+心跳） |
| `mock_datahub.py` | 模拟数据中台（策略方向：应答上线、分配编号、发心跳、可选推报文） |
| `send_test.py` | **手动 XADD 发送器** + 性能统计 |
| `perf_stats.py` | 吞吐 / 字节 / CPU / 延迟分位 / 落盘 JSON+Excel |
| `config.py` `config.ini` | 共享配置（CLI 参数优先） |
| `out/logs/` `out/performance/` | 运行日志、性能统计输出 |
| `_e2e_test.py` `_e2e_destroy.py` `_lat_test.py` | 自测脚本（Windows 侧联调验证） |
| `_plugin_e2e.py` | 自测脚本（SSH 驱动 136 上真插件，跑完自动还原 `DataHub.ini`） |

---

## 3. 快速开始

### 3.1 最简：不需要真数据中台（自应答模式）

```bash
# 终端1：Mock 策略平台，自己占编号 1，下发流 = ST-1
python mock_strategy.py --host 192.168.1.137 --db 0 --assign-id 1

# 终端2：手动 XADD 发报文
python send_test.py --host 192.168.1.137 --db 0 --assign-id 1 \
    --interface create --type normal --workers 8 --max 10000 --sync-probe 30
```

### 3.2 联调：Mock 中台 + Mock 策略（走完整握手）

```bash
# 终端1：Mock 数据中台（负责分配编号）
python mock_datahub.py --host 192.168.1.137 --db 0 --alloc-start 1

# 终端2：Mock 策略平台（--no-assign = 等中台分配编号）
python mock_strategy.py --host 192.168.1.137 --db 0 --no-assign

# 终端3：发报文
python send_test.py --host 192.168.1.137 --db 0 --assign-id 1 --type normal --max 5000
```

### 3.3 用真插件当策略平台（**可选**，仅用于复核协议）

先说结论：**常规测试完全不需要 `.so`。**
本目录没有任何一个文件 `import ctypes` / 加载 `.so` —— 插件原本的职责
（上线、心跳、收 `ST-N`、回包）已经全部由 `mock_strategy.py` 用纯 socket 实现。
`.so` 只在一种场景下有用：**拿真插件当"标准答案"，验证 mock 仿真得对不对**
（本次就是靠它才发现回包走的是 `DataHub_reply_stream` 而不是 `ST-N-reply`）。
本地不需要保留副本，136 上那份就够。

`.so` 是 Linux 库，只能在 136 上跑（`libdatahub_strategy_plug.so` 已在 `/home/yangsh/so_test/strategy/`）。

> **踩坑提醒：`.so` 读的是 `DataHub.ini`，不是 `config.ini`。**
> 插件的 Redis 地址来自它旁边的 `DataHub.ini` 里的 `REDISHOST/REDISPORT/REDISSELECT`，
> 本目录的 `config.ini` 只管本工具自己。
> 两边不一致时，插件和 mock 会各连各的 Redis，表现为**插件一直拿不到编号、ST-N 永不出现**。
> 实测该文件原本是 `REDISHOST=127.0.0.1`（指 136 本地），而本工具默认连 137 —— 必须对齐。

```bash
# 136 上：先确认/改好 DataHub.ini，再跑插件
cd /home/yangsh/so_test/strategy
grep -E 'REDISHOST|REDISPORT|REDISSELECT' DataHub.ini   # 应当指向 137
python3 strategy_client.py --so ./libdatahub_strategy_plug.so \
    --unique test --autoreply 1 --reply-data '{"status":"OK"}' --wait 75
```

```bash
# 本机：给它分配编号
python mock_datahub.py --host 192.168.1.137 --db 0 --alloc-start 1
```

一键验证（自动改 `DataHub.ini`、跑完**自动还原**）：

```bash
python _plugin_e2e.py
```

### 3.4 观察真数据中台发什么（只收不回）

```bash
python mock_strategy.py --host 192.168.1.137 --db 0 --no-reply \
    --assign-id 1 --unique ST-761-2cea7fd9d5c0test
python mock_datahub.py --host 192.168.1.137 --db 0 --observe-only
```

### 3.4.1 匹配现场真中台（带 _1 后缀频道）

```bash
# 真数据中台订阅的是带 _1 后缀的一套，用这个后缀去对齐
python mock_strategy.py --host 192.168.1.137 --db 0 --channel-suffix _1 --no-assign
python mock_datahub.py  --host 192.168.1.137 --db 0 --channel-suffix _1
```

### 3.5 破坏测试

```bash
python send_test.py --host 192.168.1.137 --db 0 --assign-id 1 \
    --type destroy --workers 8 --max 5000 --wait 10

# 指定用例（写 --cases 会自动把 --type 放宽为 all）
python send_test.py --cases D001,D003-D010 --no-send      # 先预览
python send_test.py --cases D001,D003-D010 --assign-id 1  # 再发
```

---

## 4. 常用参数

### send_test.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `--interface` | 配置(create) | `create` `modify` `remove` `pwdUpdate` `account` `all` |
| `--type` | 配置(normal) | `normal` `destroy` `all` |
| `--cases` | 空 | 按编号筛选，如 `D001,D003-D010,N001`（写它自动放宽 `--type`） |
| `--assign-id` | 配置(1) | 目标流 = `ST-<id>` |
| `--stream` | 空 | 直接指定流名，覆盖 `--assign-id` |
| `--workers` | 配置(4) | 并发线程数 |
| `--max` | 配置(0) | 发送总条数；超过用例数会循环复用 |
| `--seconds` | 0 | 按时间跑（优先于 `--max`） |
| `--rate` | 0 | 全局限速 条/秒，0=不限 |
| `--wait` | 配置(5) | 发完等回包秒数（收齐或稳定后提前结束） |
| `--no-reply` | 关 | 不读回包（纯发） |
| `--reply-stream` | 空 | 回包流名，默认 `DataHub_reply_stream` |
| `--sync-probe` | 0 | 压测后单发单收 N 次，测链路真实 RTT |
| `--no-send` | 关 | 只预览报文 |
| `--list-cases` | 关 | 只列出用例 |
| `--dump` | 空 | 预览时把报文写 jsonl |
| `--quiet` | 1 | 1=安静 |
| `--no-stats` / `--stats-out` / `--stats-interval` | 开/`out/performance`/1.0 | 统计开关、输出目录、采样间隔 |

### mock_strategy.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `--assign-id` | 配置(1) | 数据中台分配的编号 / 下发流 `ST-<id>` |
| `--auto-assign-id` | 关 | 从 `--assign-id` 起找第一个未被占用的流（多开防撞车） |
| `--no-assign` | 关 | 不自己应答上线，等真中台分配编号 |
| `--no-reply` | 关 | 只收不回（观察模式） |
| `--reply-stream` | `auto` | `auto`=DataHub_reply_stream、`reply`=ST-N-reply、`both` |
| `--reply-data` | `{"status":"OK"}` | 回包内容 |
| `--read-count` | 100 | 每次 XREADGROUP 取多少条。真插件是 **1**；用 1 会让 mock 成为瓶颈（实测仅 ~475 条/秒），想复刻真插件行为才用 1 |
| `--channel-suffix` | 空 | 频道后缀。现场不带后缀(插件用)与 `_1`(真中台用)两套并存；要匹配现场真中台用 `--channel-suffix _1` |
| `--workers` | 配置(4) | 消费线程数 |
| `--unique` / `--unique-rand` / `--unique-name` / `--mac` | 自动拼 `ST-<rand>-<mac><name>` | 上线身份 |
| `--online-interval` / `--beat-interval` | 2.0 / 5.0 | 上线重发 / 心跳间隔 |
| `--seconds` | 0 | 运行秒数，0=一直跑 |

### mock_datahub.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `--alloc-start` | 配置(1) | 分配编号起始值 |
| `--observe-only` | 关 | 只观察，不分配编号/不发心跳 |
| `--channel-suffix` | 空 | 频道后缀；默认会**同时监听**不带后缀与 `_1` 两套，指定后只听一套 |
| `--beat-interval` | 10.0 | 给策略平台发心跳的间隔 |
| `--push` | 关 | 持续往 `ST-<id>` 推业务报文 |
| `--push-interface` / `--push-rate` / `--push-interval` / `--push-max` / `--push-destroy` | create / 0 / 1.0 / 0 / 关 | 推送配置 |

---

## 5. 用例说明

### 类型

沿用 `datahub_test` 的两分法：

| 类型 | 含义 |
|---|---|
| `normal` | 合法报文（照抄真实流量的字段结构），用于性能/联通性基线 |
| `destroy` | 畸形/极端报文，测策略平台健壮性（不崩、不泄漏、不误处理） |

### 数量

```
normal  : 5   （create/modify/remove/pwdUpdate/account 各 1）
destroy : 1000（结构级 40 + 字段级 fuzz 960）
合计    : 1005
```

### destroy 覆盖什么

* **结构级**（D001~D040, D060~D074）：非法 JSON、非对象、空对象、MsgType 缺失/未知/负数/字符串/null、子对象类型错、多子对象并存、MsgType 与子对象不匹配、500 个垃圾字段、200 层嵌套、重复键注入、1MB 大 JSON、Account/Entrust 类型错、Shareholders 异常、账号字段互相矛盾、过去日期、负价格、非法市场号等
* **字段级 fuzz**（D200+）：对真实报文里的关键路径逐个灌 25 种畸形值 —— 空串、全空格、JSON null、超长 1000/10000、控制字符、NUL 字节、SQL 注入、XSS、格式化串、emoji 长串、数学符号、嵌套 JSON 串、超大/极小整数、科学计数、NaN/inf、十六进制、非法布尔、JSON true/false、空对象、空数组

token 机制与 `datahub_test/interfaces/_common.py` 一致：配置里存占位符，发送时才展开。

动态日期（注意两种格式**别混用**）：

| token | 展开 |
|---|---|
| `__TODAY__` / `__TODAY_D30__` / `__TODAY_D_1__` | `YYYY-MM-DD`（`ValidDate` 用这种） |
| `__TODAY8__` / `__TODAY_PLUS7__` | `YYYYMMDD`（`CondTime.TriggerDate` 用这种） |

---

## 6. 输出

* `out/logs/{label}.log`：完整运行日志
* `out/performance/{label}_stats.json`：汇总指标
* `out/performance/{label}.xlsx`：汇总 + 按秒明细 + 错误分布（三个 sheet；无 openpyxl 时退化成 CSV）

指标口径：每秒发送/回包条数、请求/回包字节数（按秒）、客户端 CPU 利用率、
平均响应时间（微秒）＋ P50/P90/P95/P99/Max、错误分布。

---

## 7. 自测

```bash
# 端到端闭环：Mock中台 + Mock策略 + 发 5 条，核对 request_id 与回包
python _e2e_test.py

# 压测 + 破坏测试（normal 2000 条 + destroy 1500 条）
python _e2e_destroy.py

# 单条真实 RTT 对照
python _lat_test.py
```

实测结果（2026-09-24，目标 192.168.1.137:6379 db0）：

```
normal 3000 条 / 8 线程 : 1704 条/秒，回包 3000/3000，失败 0，在途 0
                          批量口径 平均 5.17 ms；同步口径 平均 0.84 ms
destroy 1500 条 / 8 线程: 1260 条/秒，回包 1500/1500，失败 0，在途 0
```

---

## 8. 注意

* **目标 Redis 用 137**（`DataHub.ini` 里 `REDISHOST=192.168.1.137`），db0。
  136 上的 Redis 是同事在测别的东西，别去写。
* 本工具会往目标 Redis **真实写入** `ST-<id>`、`ST-<id>-reply`、`DataHub_reply_stream`。
  仅限测试环境；联调真中台前先确认不会污染生产流。
* `.so` 是 Linux 库，Windows 上只能跑 Mock / 发送器，加载插件要在 136。
* Linux 上的 python3.9 通常没装 `redis` 模块，所以 `resp_min.py` 用纯 socket 实现。
* Windows 控制台打印畸形字符可能报编码错，建议 `PYTHONIOENCODING=utf-8`。
* `mock_strategy.py --read-count 1` 可复刻真插件（`COUNT 1`），但会让 mock 成为瓶颈，
  压测时请用默认值 100。
