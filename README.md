# Strategy 策略平台测试工具

针对**数据中台 → 策略平台**方向的测试工具集。

之前做的是*委托服务器 → 数据中台*（见 `../datahub_test/`）。这次方向反过来：
数据中台主动把条件单推给策略平台，策略平台收下、处理、回包。
但**策略平台暂未开放**，所以本目录提供一个 Mock 策略平台顶替它。
另外以前靠 MQ 插件发送，现在插件没了，**一律手动 XADD**。

---

## 0. 怎么跑（先看这段）

需要 Python 3.8+。本目录用 `venv`（`D:\Code\Python\多线程\venv`）即可，它已装好 PySide6 + openpyxl。

### 0.1 图形界面（推荐）

```powershell
cd strategy_test
..\venv\Scripts\python.exe gui_test.py
```

打开后按这个顺序点：

1. **右栏 → 服务管理 → 「启动 Mock 策略平台」**
   （默认「自应答编号」勾着，它会自己占编号 1，即下发流 = ST-1）
2. **左栏 → 3. 测试数据**：勾 `create`，类型选 `normal`
   （这里**不会**去读 Excel，勾选是瞬时的；条数在点发送时才统计，见下）
3. **左栏 → 4. 发送参数**：并发 8、总条数 2000
4. **底部 → 「开始发送」**

> **「3. 测试数据」为什么不再显示条数**：原来每次勾接口/切类型都要读一遍
> Excel 来算条数，而且是「本次 1 遍 + 全库 3 遍」= **4 遍完整扫描**。
> 实测 openpyxl 读表很慢，且跑在 Qt 主线程上会冻住界面：
>
> | 行数 | 单遍读表 | 点一下要等（×4） |
> |---|---|---|
> | 3 000 | 3.2 s | 13 s |
> | 30 000 | 32 s | **160 s**（界面卡死） |
> | 100 000 | 106 s | **534 s** |
>
> 现在勾选只更新一行文字提示（零成本），真正的读表交给
> `send_test.py` 在**子进程**里做 —— 界面不卡，而且 `--max` 小时
> 还能提前终止（不必读完整张表）。

> **只需起这一个服务。** 下面那个「Mock 数据中台」面板在打真平台时**必起**，
> 常规（打自己的 Mock 策略平台）测试才用不到。

实时日志在下方「运行日志」，跑完自动切到「统计汇总」页，可一键导出 Excel。

日志页上方有一条小工具条：

| 控件 | 作用 |
|---|---|
| **清空日志** | 只清空窗口里的显示，**不动** `out/logs/` 下的文件；任务运行中会先确认一次 |
| **每次发送前自动清空** | 勾上后，点「开始发送 / 预览」会先清空，每次只看本次输出（该偏好记在 `config.ini`） |

> 日志缓冲区有上限（`MAX_LOG_LINES`），超了会自动丢最旧的；完整日志始终在
> `out/logs/{label}.log` 里，所以「清空」不会丢任何东西。

#### 想看发出去的 JSON 报文，有四种方式

| 方式 | 怎么做 | 特点 |
|---|---|---|
| **① GUI 预览**（最方便） | 勾好接口/类型 → 点 **「预览报文（不发）」** | 日志里打印 `XADD ... task {...}`；**不发数据**。⚠ 每条只打印前 **1500** 字符，长报文会被截 |
| **② 看流里真实的**（最接近真相） | `python tests/_show_stream.py --stream ST-50 -n 5` | 直接从 Redis 读回**真正写进去的**内容，含 `request_id`，不截断 |
| **③ 落盘成文件** | `--no-send --dump out/payload.jsonl` | 完整 JSON 逐行写入，适合存档/对比 |
| **④ 看历史** | `out/logs/{label}.log` | 只记条数/统计，**不含报文内容**（要看报文用 ①②③） |

```bash
# ② 看实际发出去的（推荐排查用）
python tests/_show_stream.py --stream ST-50 -n 5          # 最近 5 条
python tests/_show_stream.py --stream ST-50 -n 1 --json   # 格式化缩进
python tests/_show_stream.py --stream ST-50 --full        # 不截断
python tests/_show_stream.py --stream ST-50 --out d.jsonl # 导出

# ③ 发送前预览并落盘（不写 Redis）
python send_test.py --interface create --cases C001 --no-send --dump out/payload.jsonl
```

> **什么时候用哪个**：只想确认"将要发什么" → ① 或 ③；
> 怀疑"实际发出去的跟预览不一样" → ②（它是唯一读**真实流**的，时间戳、
> `request_id` 都是最终值）；要给同事看完整报文 → ③ 或 ②`--out`。

其它几个按钮：

底部按钮：

| 按钮 | 作用 |
|---|---|
| 预览报文（不发） | 只打印将要 XADD 的内容，不写 Redis |
| 开始发送 | 按「用例类型」下拉框 + 当前筛选发送（normal / error / destroy / all） |
| 停止 | 中止正在跑的发送任务 |

> **类型由谁决定**：「开始发送」和「预览报文」都读左栏「3. 测试数据 → 用例类型」
> 下拉框。想发破坏用例就把下拉框选成 `destroy` 再点「开始发送」——
> 旧版这里有个独立的「破坏测试」按钮，已于 `7d2a015` 移除。

「**统计汇总**」标签页上方还有两个按钮（就在它们的用武之地）：

| 按钮 | 作用 |
|---|---|
| 刷新统计汇总 | 扫 `out/performance/*_stats.json` 汇总成表（跑完发送会自动刷一次） |
| 导出汇总 Excel | 把汇总表导出成 Excel |

> 早先这几个按钮放在右栏的「快捷操作」里，还混着「一键自测 / 真插件复核 / 环境体检」。
> 现已精简：**右栏只留服务管理**。那三个已随自测脚本一起删除，
> 需要只读体检时直接跑 `python check_env.py`。

界面参数会记忆到 `config.ini`，下次打开还在。

#### 生成压测数据（GUI 里也能做）

「3. 测试数据」面板下方多了四个控件，**只重写 `data/*.xlsx`，不会发送任何数据**：

| 控件 | 作用 |
|---|---|
| **批量账号数** | 勾选的接口生成 N 行不同账号（`--bulk-normal N`）。0 = 不启用 |
| **账号起始** | 账号 6 位序号起点。0 = 接口默认（11301）。**用 Ref 回填时必须保持 0** |
| **Ref回填** + 浏览… | 选 `send_test` 发 create 时落盘的 `*_refs.json`，生成 modify/remove 时按账号换成**真实单号** |
| **生成压测数据** | 执行 `make_excel.py`（只写文件）。旁边还有「打开 data 目录」 |

典型流程（**推荐**）：

```
1. 勾 create，点「开始发送」 → 日志出现「★ 抓到 N 个条件单号，已写入: ...」
2. 「Ref回填」浏览… 选那个 refs.json，「批量账号数」填 N（与 create 一致）
3. 勾 modify / remove，点「生成压测数据」 → 单号被填成真实值
4. 再发送 modify / remove
```

**GUI 会做一致性校验**（照 `datahub_test` 的做法，宁可先问也别生成无效数据）：

* 填了 Ref 回填但「批量账号数」是 0 → **拦下**（回填靠账号查，没多账号行就不生效）
* 填了 Ref 回填但「账号起始」非 0 → 提示**可能生成无效数据**（账号序列会错位，全部查不到）
* 填了 Ref 回填但没勾 modify/remove → 提示忽略
* 批量账号是 0 且没有回填 → 提醒"生成出来还是小表，压测会循环复用"

> 命令行等价写法见 §5.2。

#### 打真平台：勾「允许打真平台」
「4. 发送参数」最下面有个红色复选框：

```
☐ 允许打真平台（--force-live，跳过安全闸）
```

**默认关，而且故意不记忆**（每次打开都从关开始）—— 这个开关一旦被记住，
下次顺手点「开始发送」就可能直接打到真平台。

它对应的就是命令行的 `--force-live`。为什么需要它：`send_test.py` 发现
目标流上存在**真平台消费者**（名字 = 流名本身，如 `ST-50`）时会拒绝发送
（退出码 2，见 §1.5）。GUI 里不勾这个框，发送就会**直接失败**。

不勾也会被拦住 —— 但 GUI 会**提前**弹窗告诉你（而不是等你发完才看到退出码 2）：

```
目标流 ST-50 上有【真实策略平台】的消费者：
    user_group/ST-50 (idle=93ms pending=0)

未勾选「允许打真平台」，本次发送会被安全闸拦下（退出码 2）。
要现在就勾上并发送吗？
  选「否」= 不发，你可以先改编号
```

勾上之后再点发送，会**再确认一次**（这是危险操作）：

```
⚠ 确认打真平台
继续 = 给真平台下发假条件单，可能触发真实交易！
确定要发送吗？          ← 默认按钮是「否」
```

目标流上没有真平台（打自己的 mock）时，这个框勾不勾都无所谓 ——
GUI 只会在日志里提一句「本次 `--force-live` 实际未生效」，不打扰你。

### 0.2 命令行（不喜欢界面时）

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

> ⚠️ `--sync-probe N` 会**额外真实写入 N 条**报文（要发真请求才能量 RTT），
> 所以流里条数 = 总条数 + N。它们不计入「发送/回包」统计。
>
> **默认已是 0（关闭）**。早先默认 20，等于「默认多发给真平台 20 条」，
> 而且收不到回包时每条要干等 5 秒（20 条 = 最多白等 100 秒，很容易被
> 当成界面卡死）。要测真实 RTT 再主动填，建议先填 `1`~`3`。
> GUI 里填 0 就不会带上这个参数。

### 0.3 破坏测试

图形界面：左栏「用例类型」选 `destroy`，再点底部「开始发送」。
命令行：

```powershell
# 先预览要发什么（不发）
..\venv\Scripts\python.exe send_test.py --type destroy --no-send

# 真发：1000 条破坏用例，8 线程
..\venv\Scripts\python.exe send_test.py --host 192.168.1.137 --db 0 --assign-id 1 `
    --type destroy --workers 8 --max 1000 --wait 8
```

### 0.4 常用变体

```powershell
# 只发不收回包（纯压发送端）
... send_test.py --assign-id 1 --no-reply --workers 8 --seconds 10

# 按时间跑 10 秒（注意：填了它「总条数」会被忽略，GUI 里会自动置灰）
... send_test.py --assign-id 1 --seconds 10 --workers 8

# 限速 100 条/秒
... send_test.py --assign-id 1 --rate 100 --max 1000

# 只发指定的破坏用例
... send_test.py --assign-id 1 --cases C201,C202,M201

# 列出所有用例
... send_test.py --list-cases --type all
```

> 目标 Redis 默认 `192.168.1.137 db0`。要改就编辑 `config.ini`，
> 或每次都带 `--host/--db`。**不要往 136 的 Redis 写**（同事在那边测别的）。
> GUI 里如果 host 不是 137，发送前会弹窗二次确认。

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
给出干净的链路 RTT。**默认 0（关闭）**，要用时显式指定。实测同一环境下：

```
批量口径（8线程/3000条）：平均 5.17 ms
同步口径（单发单收 30 次）：平均 0.84 ms   ← 链路真实往返
```

---

## 1.5 连接真实策略平台（重要，先读）

**结论：基本不用改代码。** 因为 mock 和真平台走的是**同一套协议**，
你只要把「目标指向真平台那条流」+「别把 mock 一起起」这两件事做对即可。

### 第 0 步：务必先做只读体检

```bash
python check_env.py                       # 只读，扫 136 与 137 的 db0/db1
python check_env.py --host 192.168.1.136 --db 0
```

它会告诉你三件事：
1. **真平台在哪、编号是几** —— 看有没有 `strategysrv-<id>` 这个 ReJSON 键；
   有的话，`下发流 = ST-<id>`，那就是你要打的目标。
2. **环境活不活** —— `traderserver` 的 `last-active-time` 是不是新鲜的。
3. **有没有人在订阅频道** —— `PUBSUB NUMSUB` 非 0 说明有真进程在跑。

> 这个脚本**只读**（只有 PING/KEYS/TYPE/XLEN/XINFO/NUMSUB/CLIENT LIST/GET），
> 可以在生产环境上安全运行。

### 第 1 步：三条铁律

| # | 铁律 | 为什么 |
|---|---|---|
| 1 | **不要再起 `mock_strategy.py`** | 它会用 `ST-<id>-w0` 加入 `user_group`。Redis Streams 的同一 consumer group 是**负载均衡不是广播** —— 它会**抢走真平台一半的消息**，导致真平台的单子漏处理 |
| 2 | **不要再起 `mock_datahub.py`** | 真中台自己会分配编号。你再起一个会跟它抢着应答上线，编号可能冲突 |
| 3 | **只跑 `send_test.py`** | 它就是"数据中台发报文"的角色，直接往 `ST-<id>` 里 XADD |

### 第 2 步：指向真平台的流

```bash
# --assign-id 填 check_env.py 查到的真实编号（例如 0）
python send_test.py --host 192.168.1.136 --db 0 --assign-id 0 \
    --interface create --type normal --workers 1 --max 1 --wait 10
```

**建议先只发 1 条、并发 1**，确认真平台能正常收到并回包，再逐步放量。

### 第 3 步：安全闸（已内置）

从 v2 起 `send_test.py` 带了一道**自动安全闸**：发送前它会检查目标流的
`user_group` 里有没有「不是本工具创建的消费者」（本工具的 mock 用
`ST-<id>-w0/w1/...`，真平台/真插件用 `ST-<id>`）。一旦发现外来消费者，
它会**拒绝发送**并提示你：

```
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
【安全闸】目标流 ST-0 上已存在不是本工具创建的消费者：
    user_group/ST-0(idle=37ms)
这通常意味着【真实策略平台】正在消费这条流。
往它写数据 = 给真实平台下假条件单，可能触发真实交易！
先跑只读体检确认：  python check_env.py
确认无害后要强行发送，加：  --force-live
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
```

确认无害（比如那其实是你自己另一个测试进程）后，加 `--force-live` 放行。

### 怎么区分"真平台"和"我的 mock"

| 特征 | 真实策略平台 / 真插件 | 我的 mock |
|---|---|---|
| `ST-<id>` 的 consumer 名 | `ST-0`（就是流名） | `ST-0-w0`、`ST-0-w1`… |
| `strategysrv-<id>` 注册键 | 有（真中台写的） | 无 |
| 回包内容 | `{"Err":0,"Msg":"insert trade success"}` 等真实业务结果 | `{"status":"OK"}` |
| `ST-<id>-reply` 里 | 有真实单号 `OrderNo` | 空 |
| 频道订阅 | 真中台订 `_1` 后缀那套 | 我的 mock 订不带后缀的 |

### ⚠️ 切换 Redis 后风险会反转（务必留意）

据 2026-09-24 了解到的安排：真实策略平台**当前**连的是 `192.168.1.136`，
之后会**切换到 192.168.1.137**。而本工具的**默认目标恰好是 137**：

```
今天：  默认 137 = 干净环境      → 直接跑默认命令是安全的
切换后：默认 137 = 真实环境      → 直接跑默认命令会打到真平台！
```

所以**切换之后**，无论用 GUI 还是命令行，都必须：

1. **先跑 `check_env.py`** 确认 137 上是不是已经有真平台（看 `strategysrv-<id>`）；
2. **别在真平台占用的编号上起 `mock_strategy.py`**（会抢消息）；
3. 要测就挑一个**没人用的编号**（例如 `--assign-id 50`）。

安全闸会替你拦一道（见下），但别把它当唯一防线。

### 安全闸（已内置，`send_test` 和 `mock_strategy` 都有）

`safety.py` 是两者共用的判据。发送/消费前它会检查目标流的 `user_group` 里
有没有「不是本工具创建的消费者」（本工具 mock 用 `ST-<id>-w0/w1/...`，
真平台/真插件用 `ST-<id>`）。发现外来消费者就**拒绝**并提示：

```
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
【安全闸 / send_test】目标流 ST-0 上已存在不是本工具创建的消费者：
    user_group/ST-0 (idle=86ms pending=0)

往它写数据 = 给真实策略平台下发假条件单，可能触发真实交易！

真平台与 mock 的区分：
    真平台/真插件 consumer 名 = ST-0（就是流名）
    本工具 mock  consumer 名 = ST-0-w0 / -w1 / ...（带 -wN）

先跑只读体检确认：  python check_env.py
确认无害后要强行继续，加：  --force-live
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
```

对 `mock_strategy` 的措辞不同，因为它的危害更隐蔽：

```
加入它的消费组 = 把本该真实策略平台处理的消息【抢走一部分】。
Redis Streams 同组是负载均衡不是广播 —— 真平台的单子会莫名少掉，
而且不会报任何错，极难排查！
```

> **为什么这道闸必须有**：我曾把闸错放在 worker 线程里，worker 1..N 抢在
> `stop` 传播之前就注册成了消费者 —— 实测在 136 的 `ST-0` 上真的多出了
> `ST-0-w1/w2/w3` 三个消费者。现已改为在 `_ensure_streams()` 里**同步**判定，
> 所有 worker 等结论（`_guard_ok`）后才开始 `XREADGROUP`。
> 万一又出现误污染，可用 `XINFO CONSUMERS` 找到 `-wN` 的消费者，
> 确认 `pending=0`（没扣住消息）后再 `XGROUP CREATECONSUMER` 的反向操作删除。

### 怎么区分"真平台"和"我的 mock"

| 特征 | 真实策略平台 / 真插件 | 我的 mock |
|---|---|---|
| `ST-<id>` 的 consumer 名 | `ST-0`（就是流名） | `ST-0-w0`、`ST-0-w1`… |
| `strategysrv-<id>` 注册键 | 有（真中台写的） | 无 |
| 回包内容 | `{"Err":0,"Msg":"insert trade success"}` 等真实业务结果 | `{"status":"OK"}` |
| `ST-<id>-reply` 里 | 有真实单号 `OrderNo` | 空 |
| 频道订阅 | 真中台订 `_1` 后缀那套 | 我的 mock 订不带后缀的 |

### ⚠️ 已知的真实环境（2026-09 实测，两台都要当心）

| 机器 | 真平台登记 | 下发流 | 消费者 | 备注 |
|---|---|---|---|---|
| **192.168.1.136 db0** | `strategysrv-0`，承载 147 个账号 | `ST-0` | `ST-0` | 真数据中台也在这台，回包里带真实订单号（`OrderNo=20242` 等） |
| **192.168.1.137 db0** | 插件在跑，靠 `mock_datahub` 才拿到编号 | `ST-50` | `ST-50` | 现场真平台连的是**这台**；137 上没有数据中台，见 §1.7 |

⇒ 两台的风险都高（回包里出现订单号说明可能触发真实交易）。
只想验证联调的话，**另开一个高位编号的干净流**做（如 `--assign-id 94`），别碰 `ST-0` / `ST-50`。

---

## 1.6 Pwd 字段加密（MsgType=17 / 18 必读）

`account`（用户信息，MsgType=18）和 `pwdUpdate`（改密码，MsgType=17）里的
`Pwd` **都不是明文密码**，而是**两层 AES-256-CBC + Base64** 的密文。
策略平台要拿它去柜台登录 / 改密，所以必须按它的规则加密，否则登录不上。

```
内层 = encrypt_string(明文密码,   账号)            # 账号不带 _7_6 后缀！
外层 = encrypt_string(内层结果,   当前日期字符串)    # 形如 "20260901"
Pwd  = 外层
```

算法细节（逐行对齐 `pwdEncode.cpp`）：

* `encrypt_string(data, key)` = `base64( IV(16字节随机) || AES-256-CBC(pkcs7(data)) )`
* key 拷进 32 字节数组、不足补 `0x00`（零填充，超长截断）
* **IV 随机生成并前置**在密文最前
* ⚠️ 随机 IV ⇒ 同一个密码每次加密结果都不同，**这是正常的**，不是 bug

### 怎么用

**不用手动算** —— 在 Excel 的 `Pwd` 列里填**明文**，发送时自动加密：

```
data/account.xlsx 里 A001 行： Pwd 列 = 123123
               ↓ build_payload 时
报文里： "Pwd": "skD3CmdjhSoX9PaaB69RTnqcrEOHA25m4/uMRCN+iuR3Ha...=="
```

想手工算/反解（排查用）：

```bash
python pwd_encode.py                          # 自测（含真实样本验证）
python pwd_encode.py --help                   # 见文件头注释

python -c "import pwd_encode as p; print(p.encode_pwd('123123','010100011300'))"
python -c "import pwd_encode as p; print(p.decode_pwd('<密文>','010100011300'))"
```

要发**畸形密文**（测解密容错）用 `Pwd_raw` 列，填什么就发什么、不再加密。

### ⚠️ 明文会被静默吞掉（2026-09 在 137 真平台实测）

踩过的坑：**传明文密码，真平台不报错、也不回包** —— 消息被正常消费
（`lag=0`、`pending=0`），但 `DataHub_reply_stream` 上永远等不到回包，
很容易被误判成"平台挂了"或"网络丢包"。

| 发出去的内容 | 真平台反应 |
|---|---|
| `"Pwd":"123456"` 明文 | **无响应**（消息被消费，无回包、无错误日志） |
| `"Pwd":"<两层加密密文>"` | `{"Account":{...},"Errmsg":"update password success","ErrID":0}` ✅ |

`account` 与 `pwdUpdate` 现在都走同一个 `pwd_encode.encode_pwd()`；
`pwdUpdate` 的 `P105` 用例把"传明文"当负例保留下来（期望：不回包）。

### 验证情况

三层验证，都通过：

| 验证 | 方法 | 结果 |
|---|---|---|
| 真实样本 | 用 136 真实流里抓的 `Pwd`，日期解外层→`s2l8keD5C/...`，账号解内层→`123123` | ✅ 与签署密码一致 |
| 双向交叉 | 在 136 上编译**真 `pwdEncode.cpp`**：C++ 加密→Python 解密、Python 加密→C++ 解密 | ✅ 4 组用例全一致 |
| 依赖兜底 | 本机 `cryptography` 与内置纯 Python AES 输出逐字节相同 | ✅ 一致 |
| **真平台闭环** | 在 137 真平台发 `pwdUpdate` / `account`，都拿到 `ErrID:0` | ✅ 见 §1.7 |

> 目标 Linux 机通常没装 `cryptography`，所以 `pwd_encode.py` 内置了纯 Python
> AES-256-CBC 兜底（对齐 C++ 的轮顺序），两条路径输出完全一致。

---

## 1.7 打真策略平台：实测记录（2026-09，137 db0）

现场真平台连的是 **137 db0**（不是 136）。但 137 上**没有数据中台**，
所以插件一直卡在上线抢编号那一步：

```
PUBLISH strategyserver_online {"id":-1,"unique_string":"ST-65-2cea7fd9d5c0QLDataHub",...}
   ↑ 每 2 秒重发一次，永不停 —— 因为没人在 strategyserver_online 上应答
SUBSCRIBE ST-65-2cea7fd9d5c0QLDataHub      ← 它已订好自己的会话频道
```

**用 `mock_datahub.py` 补上这个缺口即可**（分配编号 → 它自己建流 → 开始消费）：

```bash
python mock_datahub.py --alloc-start 50 --beat-interval 10
```

拿到编号后它**秒级**反应：建 `ST-50` / `ST-50-reply`，消费者 `ST-50`
进入 `XREADGROUP ... BLOCK 100 COUNT 1000 STREAMS ST-50 ST-50-reply >` 阻塞等待。
然后正常发报文即可（真平台 consumer 名 = `ST-50`，安全闸会拦，要加 `--force-live`）：

```bash
python send_test.py --interface create --cases C001 --assign-id 50 \
                    --workers 1 --max 1 --wait 15 --force-live
```

### 五个接口实测结果（都是 `--max 1` 各一条）

| 接口 | MsgType | 回包 `Errmsg` | `ErrID` | 延迟 |
|---|---|---|---|---|
| create | 4 | `insert success` | 0 | 20 ms |
| modify | 11 | `update success` | 0 | 1.0 ms |
| remove | 8 | `remove success` | 0 | 1.6 ms |
| pwdUpdate | 17 | `update password success` | 0 | 1.0 ms |
| account | 18 | `insert success` | 0 | 1.4 ms |

回包都落在 `DataHub_reply_stream`，格式与 request_id 均对得上，例如：

```
ST-50  -> XADD * request_id STTEST_1790232971097_1 task {"create":{...},"MsgType":4}
回包   -> #1790232971272-0 request_id=STTEST_1790232971097_1
          {"Ref":"20260924000001","Errmsg":"insert success","ErrID":0}
```

> ⚠ **不要改 `request_id` 的格式**。策略平台会**解析**它，真 DataHub 发的是
> `ST_<ip>_<epoch>_<n>`（下划线分隔、后两段是数字）。2026-09-28 曾把用例编号
> 塞进去试过（`STTEST_C201#1`），结果平台读到**第 1 条就再也不动了**：
> `ST-51 entries-read=1 lag=95`、`ST-55 entries-read=1 lag=96`。
> 想关联用例，用本地映射（见 §2.2），别动报文里的这个字段。

> **modify / remove 的 Ref 必须真实存在**。`__REF1__` 展开成
> `20260924000001`，正好是当天 create 造出来的单号，所以顺序不能乱：
> **先 create，再 modify / remove**。用别的日期造的单会得到 `ref not exist`。

### 两个纠正（之前判断错了，记在这里免得再踩）

1. **"平台休眠"是误判。** 之前以为插件有个 `checkDatahubEnableThread`
   门控、要中台心跳才消费 —— 实测**不需要**：停掉 `mock_datahub` 后，
   它照样每 5 秒 `PUBLISH strategyserver_beat {"id":50,...}` 并持续
   `XREADGROUP BLOCK 100 ...`。它"卡住"**只**是因为没拿到编号，
   而编号只有应答 `strategyserver_online` 才给。
2. **真平台 consumer 名 = 流名本身**（`ST-50`），本工具 mock 是
   `ST-50-w0/-w1`。两者在**同一个消费组**里，Redis 是**负载均衡**
   不是广播 —— 混进去会**偷走**真平台的消息。安全闸就是拦这个的。

### 收尾清理

测试完记得清掉自己造的号段流（**真平台在用的那条一个都不能碰**）。
先确认上面没有真实消费者，再删：

```bash
# 看某条流上有哪些消费者（mock 的名字带 -wN 后缀）
redis-cli -h 192.168.1.137 -a 'QianLong@2026&' XINFO CONSUMERS ST-50 user_group

# 确认是空壳（XLEN=0 且没有 pending）后再删
redis-cli -h 192.168.1.137 -a 'QianLong@2026&' DEL ST-50 ST-50-reply
```

---

## 2. 文件结构

| 文件 | 说明 |
|---|---|
| `gui_test.py` | **图形界面（PySide6）**，封装下面这些命令行工具 |
| `check_env.py` | **只读环境体检**：真平台在哪、编号几、环境活不活（连之前先跑它） |
| `safety.py` | 安全闸判据（`send_test` / `mock_strategy` 共用），防误伤真平台 |
| `pwd_encode.py` | **Pwd 字段加密**（两层 AES-256-CBC + Base64，对齐 `pwdEncode.cpp`） |
| `protocol.py` | **协议常量与报文构造**，每一条都注明实测来源；文件头含「与文档逐条对照」结论 |
| `resp_min.py` | 纯 socket 的 Redis RESP 客户端（不依赖 redis-py，Linux 3.9 也能跑） |
| `make_excel.py` | **用例生成器**：按 `interfaces/` 定义生成 `data/{接口}.xlsx` |
| `excel_loader.py` | 读 Excel 用例（口径与 `datahub_test` 一致） |
| `interfaces/` | **接口定义**（create/modify/remove/pwdUpdate/account）+ `_common.py` |
| `data/` | **生成的 Excel 用例**（`make_excel.py --interface all` 产出，勿手改后忘记重生成） |
| `mock_strategy.py` | ★**必起** — 模拟策略平台（收 ST-N、回 DataHub_reply_stream、上线+心跳） |
| `send_test.py` | ★**必起** — 手动 XADD 发送器 + 性能统计（它自己就扮演了"数据中台发报文"） |
| `perf_stats.py` | 吞吐 / 字节 / CPU / 延迟分位 / 落盘 JSON+Excel |
| `mock_datahub.py` | ○**可选** — 模拟数据中台。**默认流程用不到**，仅在「测完整上线握手」或「观察真中台」时需要，见 3.2 |
| `config.py` `config.ini` | 共享配置（CLI 参数优先） |
| `out/logs/` `out/performance/` | 运行日志、性能统计输出 |
| `tests/` | 两个**日常工具**（不是测试），见下 |

**为什么 `mock_datahub` 是可选的**：`send_test.py` 直接往 `ST-N` 写报文，
它本身就在承担"数据中台发报文"这个角色；而"数据中台分配编号"这件事，
`mock_strategy.py` 用 `--assign-id N` 自应答即可绕过。两者职责重叠：

```
send_test.py          ──XADD ST-N──►  ST-N  ──► mock_strategy 收下 → 回包
（扮演：数据中台发报文）                 ▲
                                      │ 分配编号
mock_datahub.py       ──PUBLISH───────┘
（扮演：数据中台分配编号）  ↑ 这一段才是它不可替代的
```

`mock_datahub` 唯一不可替代的是**回应上线、分配编号**那一段
（收 `strategyserver_online` 的 `id=-1`，回 `PUBLISH <unique_string> {"id":N,...}`）。
实测：用 `--no-assign` 起 `mock_strategy` 而不起 `mock_datahub`，`ST-N` 永远不会被创建。

> 早先 `mock_datahub` 还有个 `--push`（持续推报文）功能，因与 `send_test.py` 完全重复
> 且没有统计能力，**已删除**。

### 2.1 `tests/` 里的日常工具

从项目根执行：

| 文件 | 说明 |
|---|---|
| `_show_stream.py` | **看实际发出去的报文**：从 Redis 流里读回（`--json` 格式化 / `--full` 不截断 / `--out` 导出）。`out/logs/` 里**不含**报文内容，只有它能看到 |
| `_verify_bulk.py` | **复核批量数据**：账号唯一性、`UniqueAccount` 是否跟随、`Ref` 是否唯一、号段是否对齐、`Pwd` 能否解回 |
| `_ack_gap.py` | **看平台停在哪**：对着服务端 PEL 列出「已 ACK / 未 ACK / 从没读」的分界 |
| `_find_culprit.py` | **定位是哪条报文把平台弄挂**：单条注入 + 等确认，消除并发这个变量（详见 §2.3） |

```bash
python tests/_show_stream.py --stream ST-50 -n 5      # 最近 5 条
python tests/_verify_bulk.py                          # 复核 data/*.xlsx
python tests/_ack_gap.py --stream ST-10               # 平台停在哪
python tests/_find_culprit.py --assign-id 10 --type destroy   # 逐条揪凶手
```

> 早先这里还有一套 GUI 自测（`_gui_*`、`_e2e_*`、`_run_suites` 等 18 个），
> **已全部删除** —— 它们只在开发期用来防回归，日常用不到，
> 与 `datahub_test` 的结构保持一致（那边也没有测试目录）。

### 2.3 平台是多线程的，怎么知道是哪条请求弄挂的

**先说结论：事后从"卡住的状态"反推是无效的。** 平台多线程处理，
完成顺序 ≠ 发送顺序，所以看到"ACK 到第 51 条为止"只能说明"有一批没做完"，
**不能说明是哪一条**。典型佐证（2026-09-29 ST-10 实测）：

```
ST-10 XLEN=96  entries-read=96  lag=0   ← 96 条全被读走
ACK=51  PEND=45  NEVER=0
PEL idle: 45 条全部 = 518138ms，跨度 0    ← 一模一样，说明是一次性批量派发的
```

还有 9 条「已 ACK 但无回包」（A205/AD215/AD223/AD232/AD235~AD239），
以及回包流里的 `{"Errmsg":"json parse error","ErrID":-8}` ——
都说明平台对畸形报文**有容错**，不是一碰就死；"没回包"也不等于"挂了"。

**正确做法：消除并发这个变量 —— 一次只发一条，等它确认。**

用 `tests/_find_culprit.py`：

```bash
# 先确认平台在线（脚本会自己做健康检查，不在线就直接中止，不白发）
python tests/_find_culprit.py --assign-id 10 --interface account --type destroy

# 只试可疑的一段
python tests/_find_culprit.py --assign-id 10 --type destroy --cases AD240-AD503

# 平台慢就把等待调大
python tests/_find_culprit.py --assign-id 10 --type destroy --wait 8
```

它每条都发一个「正常」基线用例确认平台本来是活的，然后逐条发并等确认，
一旦某条超时未确认就停下并报告——**上一条成功、这一条卡住，就是它**。

三种判定：

| verdict | 含义 | 说明 |
|---|---|---|
| `OK` / `OK_NO_REPLY` | 平台确认了 | `OK_NO_REPLY` = 平台处理了但没回包（平台自己的错误路径） |
| `STUCK_PEND` | **读了但一直不确认** | 最可疑，就是让平台卡住的那条 |
| `NOT_READ` | 压根没读 | 平台已经不干活了（先修平台再定位） |

**它只用只读命令探测**（`XPENDING`/`XRANGE`/`XINFO`），
**绝不 `XREADGROUP`** —— 那样会加入平台的消费组抢走消息（Streams 同组是
负载均衡不是广播），真平台的单子会莫名少掉且不报错。

> 如果单条都不挂：说明触发条件是**并发/组合**（线程池耗尽、连接池打满、
> 某几条叠加），不是单条毒丸。这时用 `--cases` 把可疑段一起发来复现，
> 或去查平台侧的线程/连接池。

### 2.2 平台挂了，怎么知道是哪条请求挂的

**用例编号取自本地映射，不写进报文。** `send_test.py` 内存里有
`rid -> (用例编号, 账号)`（`_rid_meta`，回包抓 Ref 也靠它），
所以排查走本地 + 服务端两边对照，**不需要**动 `request_id`
（它的格式不能改，原因见 §1.7 的警告）。

发起一次发送后，结尾会自动打一段核对：

```
★ 未回包 96 条，涉及 96 个用例：A201 A202 A203 A204 ...
  服务端：ST-55 组 user_group  已读=1 未ACK=1 lag=95
  未ACK 前 20 条的用例编号：A201
  （? = 该条不是本次运行发的，本地没有它的用例映射）
```

看这两行就能区分三种情况：

| 本地未回包 | 服务端 PEL | 结论 |
|---|---|---|
| 有 | **有** | 平台**收到了但卡住/崩了** —— 最像"挂了"，未 ACK 的那几条就是元凶 |
| 有 | 没有 | 平台**压根没读**（消费者不在 / 流名不对 / 编号错了） |
| 没有 | — | 平台正常 |

只想看一眼当前状态（不发送）：
`python tests/_ack_gap.py --stream ST-50`

> **为什么不用 request_id 传用例编号**：试过了，会让平台卡死（见下）。
> 本地映射够用 —— 前提是**在同一次运行的输出里看**；进程退出后映射就没了。
> 跨进程追查只能用 `_ack_gap.py` 看 entry_id，或用 `_show_stream.py`
> 看报文内容来人工判断。

---

## 3. 快速开始

三个组件的分工（记住这个就不会多起进程）：

```
mock_strategy.py   ★必起   扮演"策略平台"：占编号、收 ST-N、回包、发心跳
send_test.py       ★必起   扮演"数据中台发报文"：手动 XADD 进 ST-N
mock_datahub.py    ○可选   只在要测"完整上线握手"或"观察真中台"时才起
```

### 3.1 默认流程（**两个进程就够**，推荐从这里开始）

```bash
# 终端1：Mock 策略平台，自己占编号 1，下发流 = ST-1
python mock_strategy.py --host 192.168.1.137 --db 0 --assign-id 1

# 终端2：手动 XADD 发报文
python send_test.py --host 192.168.1.137 --db 0 --assign-id 1 \
    --interface create --type normal --workers 8 --max 10000 --sync-probe 30
```

不需要 `mock_datahub`：`send_test` 直接写 `ST-1`，而编号由 `mock_strategy` 自应答占掉。

### 3.2 可选：走完整上线握手（**三个进程**）

只有在你想验证"策略平台上线 → 数据中台分配编号 → 确认 → 心跳"这一整套
时序时，才需要 `mock_datahub`。它负责的就是**分配编号**那一步
（`mock_strategy` 用 `--no-assign` 把这一步让出去）：

```bash
# 终端1：Mock 数据中台（负责分配编号）
python mock_datahub.py --host 192.168.1.137 --db 0 --alloc-start 1

# 终端2：Mock 策略平台（--no-assign = 等中台分配编号）
python mock_strategy.py --host 192.168.1.137 --db 0 --no-assign

# 终端3：发报文
python send_test.py --host 192.168.1.137 --db 0 --assign-id 1 --type normal --max 5000
```

同样这套也用于**观察真数据中台**：`mock_datahub --observe-only` 只旁听不应答，
能看到真中台给策略平台分了哪个编号。

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

> 早先有个自动化脚本 `tests/_plugin_e2e.py` 做这件事，**已随自测脚本删除**。
> 现在按上面的手工步骤跑即可（改 `DataHub.ini` → 起插件 → 本机起 `mock_datahub`
> 分配编号 → 观察 `ST-N` 是否被创建、回包是否落在 `DataHub_reply_stream`）。

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
python send_test.py --cases C201,C203-C210 --no-send      # 先预览
python send_test.py --cases C201,C203-C210 --assign-id 1  # 再发
```

> **`--cases` 的错误处理（2026-09-28 对齐 `datahub_test`）**
>
> 表达式写错（`xyz`、`C201-`、`C201-A205` 前缀不一致）或**一条都没匹配到**时，
> 直接报错退出，**不再静默忽略**。以前静默忽略会让筛选条件整体失效，
> 结果变成"发全表"—— 打错一个字就发上万条。现在宁可报错。
>
> 同时补齐了 `datahub_test` 的便利写法：`;` 和空格也能当分隔符、
> 编号忽略大小写与前导零（`a201`/`A0201` 都能命中 `A201`）、
> 支持任意位数编号（`CB00001-CB00003` 这种 5 位批量编号以前会解析错）。

---

## 4. 常用参数

### send_test.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `--interface` | 配置(create) | `create` `modify` `remove` `pwdUpdate` `account` `all` |
| `--type` | 配置(normal) | `normal` `destroy` `all` |
| `--cases` | 空 | 按编号/行号筛选（与 `datahub_test` 同口径）。分隔符 `,` `;` 空格可混用；带字母按「用例编号」列匹配（忽略大小写与前导零，如 `C201`/`CB00001`），纯数字按数据行号（1 起始）。写它自动放宽 `--type`。**写错会报错退出，不会退化成发全表** |
| `--assign-id` | 配置(1) | 目标流 = `ST-<id>` |
| `--stream` | 空 | 直接指定流名，覆盖 `--assign-id` |
| `--workers` | 配置(4) | 并发线程数 |
| `--max` | 配置(0) | 最多处理多少条（在 `--type`/`--interface`/`--cases` 筛选**之后**算）。**0 = 全部**：筛选出的用例各发一次。小于用例数=只发前 N 条；大于用例数=循环复用凑够 N 条 |
| `--seconds` | 0 | 按时间跑（优先于 `--max`）。填了它就是**不限量**，发到时间到点 |
| `--rate` | 0 | 全局限速 条/秒，0=不限 |
| `--wait` | 配置(5) | 发完等回包秒数（收齐或稳定后提前结束） |
| `--no-reply` | 关 | 不读回包（纯发） |
| `--reply-stream` | 空 | 回包流名，默认 `DataHub_reply_stream` |
| `--sync-probe` | **0（关闭）** | 压测后单发单收 N 次，测链路真实 RTT。⚠ 会**额外真实写入 N 条**、不计入统计；收不到回包时每条干等 5 秒。要测再填，建议 1~3 |
| `--refs-out` | 空 | 发 create 时把回包里的真实单号写成 JSON（默认 `<stats-out>/<标签>_refs.json`），供 `make_excel --ref-map` 用 |
| `--no-refs-out` | 关 | 不写上面那个 refs JSON |
| `--no-send` | 关 | 只预览报文 |
| `--list-cases` | 关 | 只列出用例 |
| `--dump` | 空 | 预览时把报文写 jsonl |
| `--quiet` | 1 | 1=安静 |
| `--no-stats` / `--stats-out` / `--stats-interval` | 开/`out/performance`/1.0 | 统计开关、输出目录、采样间隔 |

> **`--max` 的语义（与 `datahub_test` 对齐）**
>
> `--max 0`（也是配置里的默认值）= **把当前筛选出的用例各发一次**，不是"不限量"。
> 三种取值：
>
> | 取值 | 行为 | 例子 |
> |---|---|---|
> | `0`（或不填） | 用例各发一次 | `account --type destroy` → 发 96 条就结束 |
> | `< 用例数` | 只发前 N 条 | `--max 5` → 发 5 条 |
> | `> 用例数` | 循环复用凑够 N 条 | `--max 200` → 96 条用例循环，发满 200 条 |
>
> 只有 **`--seconds > 0`** 才是"不限量"（发到时间到点）。
> 早先 `send_test.py` 把 `0` 当成"不限量"（`_producer` 里 `total=0` 让 break
> 永不触发），于是"想发 96 条 destroy，结果把 1 万条 normal 循环发到 5 万条"
> —— 已修正。

### make_excel.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `--interface` | `all` | 接口名或 `all` |
| `--list` | 关 | 只列出各接口用例数，不生成 |
| `--ref-spec` | 空 | **仅 modify/remove**：把 normal 段换成按单号区间生成的引用行，如 `7,100` 或 `20260904000001-20260904000100` |
| `--bulk-normal` | 0 | **五个接口都支持**：把 normal 段换成 N 行**不同账号**的压测数据（见 §5.1 / §5.2） |
| `--bulk-start` | 0（=11301） | 批量账号的 6 位序号起点 |
| `--ref-seq` | 1 | **仅 modify/remove**：做法 B 用，批量行引用的「当天全局起始单号」。当天已建过 N 张就填 N+1 |
| `--ref-map` | 空 | ★**推荐**：做法 A 用，读 `send_test` 落盘的 `refs.json` 回填**真实单号**（比 `--ref-seq` 可靠） |

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

### mock_datahub.py（○ 可选，常规测试用不到）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--alloc-start` | 配置(1) | 分配编号起始值 |
| `--observe-only` | 关 | 只观察，不分配编号/不发心跳（看真中台给谁分了号） |
| `--channel-suffix` | 空 | 频道后缀；默认会**同时监听**不带后缀与 `_1` 两套，指定后只听一套 |
| `--beat-interval` | 10.0 | 给策略平台发心跳的间隔 |

> 本脚本**只负责**「回应上线、分配编号、发心跳」。要造业务报文请用 `send_test.py`
> （早先的 `--push` 系列参数已删除 —— 它与 `send_test.py` 功能重复，且没有统计能力）。

---

## 5. 用例说明

### 类型

沿用 `datahub_test` 的两分法：

| 类型 | 含义 |
|---|---|
| `normal` | 合法报文（照抄真实流量的字段结构），用于性能/联通性基线 |
| `error` | 业务层非法但结构合法（空字段、账号不匹配、Ref 不存在…），期望平台明确拒 |
| `destroy` | 畸形/极端报文，测策略平台健壮性（不崩、不泄漏、不误处理） |

### 数量

默认（`make_excel.py --interface all` 生成的规模）：

```
接口        normal    error  destroy
account          5        7       96
create           5        9      287
modify           2        5       82
pwdUpdate        2        5       41
remove           2        5       34
------------------------------------
合计                                587
```

`make_excel.py --interface all --list` 可随时打印这张表。

### 5.1 压测数据扩充：`--bulk-normal`

默认每个接口 normal 只有 2~5 条，压测时 `--max` 会**循环复用**同一批报文 ——
create 用重复 `Ref` 会得到 `ref already inserted`，测出来的是"业务失败路径"，
不是"大量真实成功下单"的负载。所以提供批量扩充（**五个接口都支持**，
完整用法与依赖顺序见 §5.2）：

```bash
# 账号先"登录"（MsgType=18），再造单（MsgType=4）
python make_excel.py --interface account --bulk-normal 10000
python make_excel.py --interface create  --bulk-normal 10000
```

扩充后：

| 文件 | 结果 |
|---|---|
| `data/account.xlsx` | **10103 条**：normal 10000 + error 7 + destroy 96 |
| `data/create.xlsx` | **10296 条**：normal 10000 + error 9 + destroy 287 |

**五个接口的账号号段完全一致（`010100011301`~`010100021300`）** —— 同一个账号
既要能登录、又要能下单/改单/删单，所以必须对齐，不能各用各的号段。

生成规则（每条都有实测依据）：

| 项 | 规则 | 为什么 |
|---|---|---|
| `Account.FAccount` | `010100` + 6 位递增序号 | 与 `datahub_test` 同一号段规则，两边数据可互通 |
| `UniqueAccount` | `<FAccount>_<AccountType>_<AccAtt>` | **必须跟着 FAccount 变**；照抄模板会被判"账号与唯一账号不一致" |
| `create.Ref` | `__REF1__` ~ `__REF10000__`（逐行不同） | 发送当天展开成 `YYYYMMDD+6位序号`，天然唯一，避免 `ref already inserted` |
| `modify.Ref` / `remove.Ref` | `__REF{i}__`，**与 create 行序对齐** | 单号必须真实存在；靠行序对齐自动接上 create 造的单（见 §5.2） |
| `create.CondType` | 固定 `1`，带全套 `Cond*` 块 | 实测 136 ST-0 的 6964 条真实 create **全是 1** |
| `account.Pwd` / `pwdUpdate.Pwd` | Excel 存**明文**，发送时按**本行账号**加密 | 账号不同密文就不同，这是对的；已验证每行都能解回 `123123` |
| `account.TradeAccount` | = `FAccount` | 实测真实样本里两者一致 |
| 股东号 | 沪 `A`+9位、深 10位数字 | 实测样本 `A442523077` / `0199908393` |
| 字段名 | 实测大写 `CondPrice`/`Op`/`TriggerPercent`/`TriggerDate`/`TriggerTime`/`Method`/`ValueType`/`WithdrawType`/`Withdraw` | 同事给的样本是小写（`cond_price`/`op`/`TriggeredPercent`…），实测 **0 次**出现，不能用 |

> ⚠️ **modify / remove 在真实流量里一条都没有**（136 ST-0 只有 create=4 和 account=18）。
> 它们的结构依据是协议文档 + 我们实测发过去拿到 `update success` / `remove success`
> 的那几条，**证据强度弱于 create/account**，现场如果被拒要优先怀疑字段名/结构。

> ⚠️ **账号只是"格式合法"，不代表柜台上真的存在。** 要真能登录/下单成功，
> 账号必须先在柜台批量开立（同 `datahub_test` 的 `acc_sign` 流程）。
> 否则这一万条大概率是"查不到账号"的业务失败，压测指标同样失真。

> ⚠️ 批量行会**替换**掉原来手写的 5 条 normal（error/destroy 全部保留）。
> 想恢复成小表：`python make_excel.py --interface all`。

> ⚠️ `--bulk-normal` 与 `--ref-spec` **互斥**。`--ref-spec` 只对 `modify`/`remove`
> 生效（按单号区间生成引用行）。**五个接口现在都实现了 `--bulk-normal`。**

### 5.2 五个接口一起扩（含依赖顺序）

五个接口**共用同一账号号段**（默认 `010100011301`~`010100021300`）：

```bash
python make_excel.py --interface account   --bulk-normal 10000
python make_excel.py --interface create    --bulk-normal 10000
python make_excel.py --interface modify    --bulk-normal 10000
python make_excel.py --interface remove    --bulk-normal 10000
python make_excel.py --interface pwdUpdate --bulk-normal 10000
```

| 接口 | 结果 | 引用单号？ |
|---|---|---|
| `account` | 10103 条（normal 10000） | 否 |
| `create` | 10296 条（normal 10000） | 自己**产生**单号 |
| `modify` | 10087 条（normal 10000） | ✅ 引用 `__REF{i}__` |
| `remove` | 10039 条（normal 10000） | ✅ 引用 `__REF{i}__` |
| `pwdUpdate` | 10046 条（normal 10000） | 否（按账号操作） |

#### ⚠️ 发送顺序是硬约束

`modify` / `remove` 的 `Ref` 必须是**平台上真实存在的单号**，否则得到
`ref not exist`。有两种做法，**推荐第一种**：

##### 做法 A（推荐）：用 create 实跑落盘的 `refs.json`

`send_test.py` 发 `create` 时会**自动从回包里抓出真实单号**并落盘：

```
★ 抓到 10000 个条件单号，已写入: out/performance/create_xxx_refs.json
  下一步可用它生成 modify/remove 用例：
    python make_excel.py --interface remove --bulk-normal 10000 --ref-map <该文件>
```

它输出的 `refs.json` 形如（按**账号**索引）：

```json
[{"account":"010100011301","case_no":"CB00001","ref":"20260928000001"},
 {"account":"010100011302","case_no":"CB00002","ref":"20260928000002"}]
```

然后用它回填：

```bash
python make_excel.py --interface remove --bulk-normal 10000 \
    --ref-map out/performance/create_xxx_refs.json
```

**为什么比做法 B 可靠**：单号是**平台回包里给的真值**，不依赖"create 一定按行序
成功"，跳过失败单也不会错位。填不上的行会保留 `__REF__` token 并告警；
若**一行都没命中**会直接报 ERROR（说明账号起点对不上）。

```bash
# 不想自动写就用 --no-refs-out；想指定路径用 --refs-out
python send_test.py --interface create --type normal --assign-id 50 --refs-out out/my_refs.json
```

##### 做法 B（备选）：靠行序对齐 `__REF{i}__`

五张表的批量行**行序一一对应**：

```
create 第 i 行  ──建出──►  当天第 i 号单
                              ▲
modify 第 i 行  ──引用───────┘  __REF{i}__
remove 第 i 行  ──引用───────┘  __REF{i}__
```

所以**必须按顺序发**：

```
1. account    账号先能"登录"
2. create     产生 1..N 号单（同时落盘 refs.json）
3. modify     改第 1..N 号（可选）
4. remove     删第 1..N 号（若已 modify 过，删的就是改后的那张）
```

> **删除不可逆**。要同时测 modify 和 remove，就按上面 `create → modify → remove`；
> 只想测 remove，`create → remove` 即可。

#### 当天已经建过单怎么办

`__REFn__` 的实际单号是「**发送当天**的日期 + 6 位序号」，比如
`__REF1__` → `20260928000001`。若当天**已经发过** N 张单，再生成时序号要接着排：

```bash
# 做法 B：当天已有 500 张单，从第 501 号开始引用
python make_excel.py --interface remove --bulk-normal 10000 --ref-seq 501
```

不填就是 `1`，即假设"今天还没建过单"。**用做法 A（--ref-map）时不需要管这个** ——
单号直接来自 `refs.json`。

> 也正因如此，**做法 B 的数据不能跨天用** —— 表里存的是 `__REFn__` 占位符，
> 单号在**发送时**才按当天日期展开。今天生成的表明天发，引用的就是明天的单号。
> 做法 A 写的是**完整静态单号**，不受日期影响（但那个单号本身当然仍要真实存在）。

### destroy 覆盖什么

* **结构级**（各接口的 x201~x2xx）：非法 JSON、非对象、空对象、MsgType 缺失/未知/负数/字符串/null、子对象类型错、多子对象并存、MsgType 与子对象不匹配、500 个垃圾字段、200 层嵌套、重复键注入、1MB 大 JSON、Account/Entrust 类型错、Shareholders 异常、账号字段互相矛盾、过去日期、负价格、非法市场号等
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

## 7. 工具与实测结果

### 7.1 `tests/` 下的两个日常工具

```bash
# 看实际发出去的报文（out/logs 里没有报文内容，只有它能看）
python tests/_show_stream.py --stream ST-50 -n 5
python tests/_show_stream.py --stream ST-50 -n 1 --json
python tests/_show_stream.py --stream ST-50 --full
python tests/_show_stream.py --stream ST-50 --out dump.jsonl

# 复核批量数据（账号唯一 / UniqueAccount 跟随 / Ref 唯一 / 号段对齐 / Pwd 可解）
python tests/_verify_bulk.py

# 平台卡在哪一条（服务端 PEL 里已 ACK / 未 ACK 的分界）
python tests/_ack_gap.py --stream ST-50
```

> 早先这里有一套 GUI 自测（18 个脚本，含 `_run_suites.py` 回归入口），**已全部删除** ——
> 它们只在开发期防回归用，日常不需要，与 `datahub_test` 的结构保持一致。

### 7.2 实测结果（2026-09-24，目标 192.168.1.137:6379 db0）

```
normal 3000 条 / 8 线程 : 1704 条/秒，回包 3000/3000，失败 0，在途 0
                          批量口径 平均 5.17 ms；同步口径 平均 0.84 ms
destroy 1500 条 / 8 线程: 1260 条/秒，回包 1500/1500，失败 0，在途 0
```

---

## 8. 注意

* **目标 Redis 用 137**（`DataHub.ini` 里 `REDISHOST=192.168.1.137`），db0。
  136 上的 Redis 是同事在测别的东西，别去写。（GUI 里 host 不是 137 会弹窗确认）
* 本工具会往目标 Redis **真实写入** `ST-<id>`、`ST-<id>-reply`、`DataHub_reply_stream`。
  仅限测试环境；联调真中台前先确认不会污染生产流。
* `--sync-probe N` 会额外真实写入 N 条报文（要发真请求才能量 RTT），
  所以流里条数 = 总条数 + N；它们不计入「发送/回包」统计。
* `.so` 是 Linux 库，Windows 上只能跑 Mock / 发送器，加载插件要在 136。
  常规测试**不需要** `.so` 的本地副本（详见 3.3 节）。
* Linux 上的 python3.9 通常没装 `redis` 模块，所以 `resp_min.py` 用纯 socket 实现。
* Windows 控制台打印畸形字符可能报编码错，建议 `PYTHONIOENCODING=utf-8`。
* `mock_strategy.py --read-count 1` 可复刻真插件（`COUNT 1`），但会让 mock 成为瓶颈，
  压测时请用默认值 100。
