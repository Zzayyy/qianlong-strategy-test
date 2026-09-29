# 开发备忘

改本目录代码前先看这里,尤其是「硬约束」。使用说明见
[guide.md](guide.md),参数见 [reference.md](reference.md)。

> 内容来源:原 `AGENTS.md`(2026-11 并入本文档)。原文件未入 git,
> 本文件是这些踩坑记录的唯一载体 —— **改动前请保留证据段落**。

---

## 1. 硬约束（碰了会出事）

### 1.1 不要改 `request_id` 的格式

格式固定为 `STTEST_<epoch毫秒>_<序号>`（下划线分隔、后两段是数字）。

**原因**：策略平台会**解析**这个字段。真 DataHub 发的是
`ST_<ip>_<epoch>_<n>`，是同一种"下划线 + 后两段数字"的形态。

**踩坑记录（2026-09-28）**：曾把用例编号塞进去（`STTEST_C201#1`），
想让排查时能直接看出是哪条用例。结果平台**读到第 1 条就再也不动了**：

```
ST-51  entries-read=1  lag=95
ST-55  entries-read=1  lag=96
```

对比同一台机器上的旧格式：`ST-50 entries-read=96`（96 条全读完）。
即格式一改，平台只读完第一条就停。已回退。

**为什么本地 mock 测不出来**：`mock_strategy.py` 只把 `request_id`
原样回带、**完全不解析**（`fields.get("request_id","")` 转发）。
所以拿自家 mock 验证一切正常，**只有真平台才暴露**。
→ 涉及"平台会解析的字段"的改动，必须在真平台上小批量试 1 条，
不能靠 mock 跑通就算数。

想关联用例请用**本地映射**：`send_test.py` 内存里有
`_rid_meta`（`rid -> (用例编号, 账号)`，回包抓 Ref 也靠它）。
不写进报文。

### 1.2 不要动 `--max` 的语义（0 = 全部）

`--max 0` = **把当前筛选出的用例各发一次**，不是"不限量"。
只有 `--seconds > 0` 才是不限量。参数表见 [reference.md](reference.md)。

**踩坑记录**：早先 `send_test.py` 把 `0` 当成"不限量"
（`_producer` 里 `total=0` 让 `break` 永不触发），于是
"想发 96 条 destroy，结果把 10000 条 normal 循环发到 5 万条"
才被手动停下。已修正为 `effective_max = len(pool)`。

实施位置：[../send_test.py](../send_test.py) 的 `effective_max` 解析段。

### 1.3 `--cases` 写错必须报错，不能静默忽略

**踩坑记录（2026-09-28）**：原实现解析不出 token 就**静默丢弃**；
若整串都解析不出，`want_nos`/`want_rows` 双双为空，
筛选条件整体失效 → **变成发全表**。实测：

| 输入 | 旧行为 | 期望 |
|---|---|---|
| `xyz`（打错字） | **发全表 10103 条** | 报错 |
| `A201;A202`（分号） | **发全表** | 选中 2 条 |
| `a201` / `A0201` | 0 条 | 命中 `A201` |

已改为照搬 `datahub_test` 的 `parse_case_spec`：报错退出，
并支持 `,` `;` 空格混用、忽略大小写与前导零、任意位数编号
（`CB00001-CB00003` 这种 5 位编号以前会被 `%03d` 重建错）。

实施位置：[../excel_loader.py](../excel_loader.py)。

---

## 2. 排查判据的由来

用户向的排查流程见 [guide.md](guide.md) 第 8 节；这里记**判据怎么来的**，
以及为什么不能用别的判据。

### 2.1 三种状态与「未ACK ≠ 没回包」

看目标流消费组的三个数（只读命令即可）：

| 状态 | 含义 | 判据 |
|---|---|---|
| `ACK` | 平台处理完并确认 | `id <= last-delivered-id` 且不在 PEL |
| `PEND` | 读了但没确认 | 在 PEL（`XPENDING`）里 |
| `NEVER` | 从没读过 | `id > last-delivered-id` |

**关键：`未ACK` 和「回包」是两条独立通道。**
ACK 走流自己的消费组（内部记账），回包走 `DataHub_reply_stream`（业务回答）。
平台完全可以 ACK 了却不回包。

`lag` = 还没被读走的条数；`lag + entries-read = XLEN`。
`lag` 可能是 `None`（Redis <7、或流被 XTRIM/XDEL 过）——
`None` 只表示算不出来，**不等于没有积压**，此时改用
`entries-read` 与 `last-delivered-id` 判断。

工具：[../tests/_ack_gap.py](../tests/_ack_gap.py)（只读）。

### 2.2 平台是多线程的 → 事后反推凶手是无效的

完成顺序 ≠ 发送顺序。看到"ACK 到第 51 条为止"只能说明
"有一批没做完"，**不能说明是哪一条**。

**实测佐证（2026-09-29 ST-10）**：

```
ST-10 XLEN=96  entries-read=96  lag=0     ← 96 条全被读走
ACK=51  PEND=45  NEVER=0
PEL idle: 45 条全部 = 518138ms，跨度 0      ← 一模一样 = 一次性批量派发
```

**正确做法：消除并发这个变量。** 一次只发一条、等确认
（回包或 XACK），再发下一条 → 顺序重新确定，
"上一条成功、这一条卡住"即锁定凶手。

工具：[../tests/_find_culprit.py](../tests/_find_culprit.py)。
判定：`OK` / `OK_NO_REPLY`（平台处理了但不回包）/ `STUCK_PEND`（读了不确认）
/ `NOT_READ`（压根没读）。

**安全约束**：只用只读命令探测（`XPENDING`/`XRANGE`/`XINFO`），
**绝不 `XREADGROUP`** —— 那会加入平台的消费组抢走消息
（Streams 同组是负载均衡不是广播），真平台单子会莫名少掉且不报错。

若单条都不挂 → 触发条件是**并发/组合**（线程池耗尽、连接池打满），
不是单条毒丸，该去查平台侧。

### 2.3 destroy 用例"不回包"是正常的，不是故障

平台对畸形报文**有容错**，且对某些错误路径**故意不回包**。

**实测（2026-09-29 ST-13）**：发 `AD232`（`Pwd_raw='!!!bad!!!'`，非法 base64）

```
服务端：ST-13 组 user_group  已读=2  未ACK=0  lag=0
```

`未ACK=0` = 平台处理完并确认了，但**没回包** —— 平台主动丢弃。
同族用例（`Pwd_raw` 类，非法密文）：`A205` / `AD232` / `AD235` / `AD236`。

对比：task 不是合法 JSON 时平台**会**回 `{"Errmsg":"json parse error","ErrID":-8}`。
即平台对 JSON 解析错误回错误码，对 Pwd 解密失败却沉默 ——
这是平台侧行为不一致，发送方无法区分"故意不回"和"挂了"，
只能靠 `未ACK` 判断。

→ **不要用「回复率」当异常判据**（会把正常跑 destroy 判成一堆假异常）。
参见 [../soak_test.py](../soak_test.py) 的判据设计。

---

## 3. 设计约定

### 3.1 与 `datahub_test` 的关系

`strategy_test` 是 `datahub_test` 的策略方向姊妹项目，**布局与口径刻意对齐**：

* `--cases` / `--max` 语义与 `datahub_test/send_test.py` 一致
* GUI 布局：左栏放"发什么"，右栏放"连哪儿/以谁身份/结果存哪"
* `soak_test.py` 复用 `datahub_test/soak_test.py` 的编排思路

差异点都是有原因的，改之前先看：
`--max 0` 见 §1.2；soak 轮换见 §3.2。

### 3.2 soak 轮换按"本类型的行号"算，不是总行数

本表行序是 `normal(10000) → error → destroy(96)`，destroy 只在
第 10008~10103 行。若照抄 datahub 按总行数(10103)切段，头几轮的
`--cases 1-500` 全是 normal，配 `--type destroy` 一条都选不到 ——
实测会让整场 soak 全变成"跳过"（3 轮全跳过）。

所以 [../soak_test.py](../soak_test.py) 先算出该类型的行号列表再轮换。

### 3.3 `--clean per-round` 在策略方向更危险

`datahub_test` 每条 WT 有自己的回复流；策略方向的
`DataHub_reply_stream` 是**多条 `ST-*` 共用**的全局流。
非独占环境清理会清掉别人的回包。默认 `monitor`。

### 3.4 GUI「开始发送」必须读用例类型下拉框

**踩坑记录**：早先写死 `on_send("normal")`，而下拉框只被「预览」读，
于是"选了 destroy 点开始发送"实际发的是 normal（白发了 5 万条）。
现在 `btn_send` / `btn_soak` 都从 `combo_type.currentText()` 读。

### 3.5 GUI 退出会写 `config.ini`

`_save_ui_state()` 在退出时把界面偏好写回 `config.ini`。
**写自动化测试时注意**：用离屏（`QT_QPA_PLATFORM=offscreen`）
构造 `MainWindow` 会真实改写 `config.ini`，测完要 `git checkout` 还原。

---

## 4. 安全闸的实现细节

`safety.py` 被 `send_test` / `mock_strategy` 共用。判"谁是外来消费者"靠
**consumer 名**：本工具 mock 是 `ST-<id>-w0/-w1/...`（带 `-wN`），
真平台/真插件是 `ST-<id>`（就是流名本身）。

**踩坑记录**：曾把安全闸错放在 worker 线程里 —— worker 1..N 会抢在
`stop` 传播之前注册成消费者，**实测在 136 的 `ST-0` 上真的多出了
`ST-0-w1/w2/w3` 三个消费者**（污染了真平台的消费组）。
现已改为在 `_ensure_streams()` 里**同步**判定，所有 worker 等
`_guard_ok` 结论后才开始 `XREADGROUP`。

万一又出现误污染：用 `XINFO CONSUMERS` 找到 `-wN` 的消费者，
确认 `pending=0`（没扣住消息）后再删。

**区分真平台 vs mock 的方法**：

| 特征 | 真实策略平台 / 真插件 | 本工具 mock |
|---|---|---|
| `ST-<id>` 的 consumer 名 | `ST-0`（就是流名） | `ST-0-w0`、`ST-0-w1`… |
| `strategysrv-<id>` 注册键 | 有（真中台写的） | 无 |
| 回包内容 | `{"Err":0,"Msg":"insert trade success"}` 等真实业务结果 | `{"status":"OK"}` |
| `ST-<id>-reply` 里 | 有真实单号 `OrderNo` | 空 |
| 频道订阅 | 真中台订 `_1` 后缀那套 | 本工具订不带后缀的 |

---

## 5. 协议验证记录

下面是"文档 vs 实测"的对照，属于开发期的验证证据，**不是使用说明**。
日常使用只需知道 [../README.md](../README.md) 的协议要点表。

### 5.1 与「数据中台to策略平台.txt」逐条对照

**结论：文档与实测一致。** 同事文档里写的频道名和流程都对，实测可逐条对上。

| 文档条目 | 实测 |
|---|---|
| 4 `strategyserver_online` | ✅ 一致。插件首发 `{"id":-1,"unique_string":"ST-761-<mac><name>",...,"usecount":1}`；中台回 `PUBLISH <unique_string> {"id":41,...}`，插件回调确认拿到编号 41 |
| 5 `strategyserver_beat` 心跳 | ✅ 一致。拿到编号后每 5s 发一次 |
| 6 `strategyserver_offline` | ⚠️ 文档有，实测**抓不到**：`DestroyMQ` 不返回、SIGTERM 也不发。策略 `.so` 里有 `"publish offline_data error,"` 错误串，但没有委托 `.so` 里那两个 offline 符号。判为**逻辑存在但未能触发**，本工具仍照发 |
| 9 中台→在线服务器心跳 | ✅ 一致。`PUBLISH <unique_string> {"id":1,"dataHubString":"test1"}`，约每 10s |
| 7/8 `datahub_online`/`_beat` | ✅ 现场存在，抓到 `datahub_beat_1 {"level":2,"role":1}` |

同事的两点补充也都对：①"监听 `strategyserver_online`，取 `unique_string`
作为回编号的 CHANNEL" —— 这正是 `mock_datahub.py` 的核心逻辑；
②"之后心跳包会发到 `strategyserver_beat`" —— 实测确认。

**频道后缀（文档没写但现场存在，两套并存）**：

```
不带后缀  strategyserver_online     ← 插件(.so) 发这套
带 _1     strategyserver_online_1   ← 现场真数据中台订这套
```

`PUBSUB NUMSUB` 实测（136 本地 Redis）：不带后缀的全是 **0 个订阅者**，
带 `_1` 的各 **1 个**。`tradeserver_*` / `datahub_*` 同规律。
已验证这个后缀**与 `DataHub.ini` 的 `REDISSELECT` 无关** ——
依次设 0/1/2，插件都仍连 db0、都仍发不带后缀的名字（MONITOR 确认）。

### 5.2 打真平台实测（2026-09-24，137 db0）

五个接口 `--max 1` 各一条，全部拿到 `ErrID:0`：

| 接口 | MsgType | 回包 `Errmsg` | 延迟 |
|---|---|---|---|
| create | 4 | `insert success` | 20 ms |
| modify | 11 | `update success` | 1.0 ms |
| remove | 8 | `remove success` | 1.6 ms |
| pwdUpdate | 17 | `update password success` | 1.0 ms |
| account | 18 | `insert success` | 1.4 ms |

吞吐参考（137 db0）：normal 3000 条/8 线程 → 1704 条/秒；
destroy 1500 条/8 线程 → 1260 条/秒。均 0 失败、0 在途。

### 5.3 两个纠正（之前判断错了，记在这里免得再踩）

1. **"平台休眠"是误判。** 之前以为插件有个 `checkDatahubEnableThread`
   门控、要中台心跳才消费 —— 实测**不需要**：停掉 `mock_datahub` 后，
   它照样每 5 秒发心跳并持续 `XREADGROUP BLOCK`。它"卡住"**只**是因为
   没拿到编号，而编号只有应答 `strategyserver_online` 才给。
2. **真平台 consumer 名 = 流名本身**（`ST-50`），本工具 mock 是
   `ST-50-w0/-w1`。两者在**同一个消费组**里，Redis 是**负载均衡**
   不是广播 —— 混进去会**偷走**真平台的消息。安全闸就是拦这个的。

### 5.4 已知的真实环境（2026-09 实测）

| 机器 | 真平台登记 | 下发流 | 消费者 | 备注 |
|---|---|---|---|---|
| **192.168.1.136 db0** | `strategysrv-0`，147 个账号 | `ST-0` | `ST-0` | 真数据中台也在这台，回包里带真实订单号（`OrderNo=20242` 等） |
| **192.168.1.137 db0** | 插件在跑，靠 `mock_datahub` 才拿到编号 | `ST-50` | `ST-50` | 现场真平台连的是**这台**；137 上没有数据中台 |

⇒ 两台风险都高（回包里出现订单号说明可能触发真实交易）。
只想验证联调的话，**另开一个高位编号的干净流**做（如 `--assign-id 94`），
别碰 `ST-0` / `ST-50`。

---

## 6. 历史记录

更早的会话记录在仓库外的 `../.codebuddy/memory/<日期>.md`。
本文件只保留"以后改代码必须知道"的部分。
