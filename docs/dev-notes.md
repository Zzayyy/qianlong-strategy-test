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

### 2.3 destroy 用例"不回包"是正常的 —— 但这条**别当成固定规律**

平台对畸形报文**有容错**，且对某些错误路径**故意不回包**。

**实测（2026-09-29 上午 ST-13）**：发 `AD232`（`Pwd_raw='!!!bad!!!'`，非法 base64）

```
服务端：ST-13 组 user_group  已读=2  未ACK=0  lag=0
```

`未ACK=0` = 平台处理完并确认了，但**没回包** —— 平台主动丢弃。
同族用例（`Pwd_raw` 类，非法密文）：`A205` / `AD232` / `AD235` / `AD236`。

对比：task 不是合法 JSON 时平台**会**回 `{"Errmsg":"json parse error","ErrID":-8}`。
即平台对 JSON 解析错误回错误码，对 Pwd 解密失败却沉默 ——
这是平台侧行为不一致，发送方无法区分"故意不回"和"挂了"，
只能靠 `未ACK` 判断。

> ⚠️ **2026-09-29 晚些时候这条被推翻了：destroy 的回包行为会变。**
> 同一天同一接口（account / destroy / 96 条）实测：
>
> | 时间 | 流 | 发送/回包 | 服务端 |
> |---|---|---|---|
> | 10:29 | ST-19 | 96 / **0** | `已读=None lag=96`（平台压根没读） |
> | 11:26 | ST-25 | 96 / **96** | `已读=102 未ACK=0 lag=0`（全回） |
> | 14:40 | ST-34 | 41 / **41** | `未ACK=0 lag=0` |
>
> 所以"destroy 不回包"是**平台当时的状态**，不是用例属性。
>
> ⇒ **不要用「回复率」当 destroy 的硬判据**（会出假异常），
> 但**也不要以为 destroy 一定不回包**。现在的做法是默认按类型分档
> （normal=99、destroy/error=不判）+ 允许显式覆盖，
> 见 [soak_test.py](../soak_test.py) 的 `DEFAULT_MIN_REPLY_RATE_BY_TYPE`。

### 2.4 回复率的分子必须是"能对上本次 request_id 的回包"

**踩坑记录（2026-09-29）**：`send_test.py` 的 `_reader_loop` 从
`DataHub_reply_stream` 读到什么就计什么。而这条流是**多条 `ST-*` 共用**的
**全局流** —— 流上不止有本轮的回复，还有：

* 别人同时在测的 `ST-*` 的回包
* `--sync-probe` 自己额外发的那 N 条的回复（它从不进 `pending`）

**实测后果**（扫 169 份历史 stats）：

```
account_normal_20260928_092824   发 1     回 21    → 2100%
create_destroy_20260924_111908   发 50    回 70    →  140%   (正好 +20 = --sync-probe 20)
quickstart                       发 2000  回 2020  →  101%
```

回复率能超过 100%，拿它当判据必然误判。

**修法**：`PerfCollector.record_reply` 只把**能对上本进程 `pending`** 的
算进 `reply`（分子），对不上的进 `reply_foreign`（只展示、不判）。
超时后被 `expire_pending` 清掉的 id 记进 `_expired`，迟到时仍算"我们的"，
不会从分子里漏掉。详见 [../perf_stats.py](../perf_stats.py)。

> 顺带：这也解释了为什么 `lat_count` 一直是对的 —— 它本来就只统计
> 能配上 `pending` 的那些，是这个 bug 的"正确参照物"。

### 2.5 回复率 100% 也可能是"假绿"：业务失败要单独统计

**踩坑记录（2026-09-30，真实平台实测）**：跑业务流时 `create` 三条里有一条
被平台拒了，但**回复率仍是 100%**：

```
Ref=20260930000001  ErrID=-5  ref already inserted    ← 失败
Ref=20260930000002  ErrID=0   insert success
Ref=20260930000003  ErrID=0   insert success
```

原因：单号是「当天日期 + 序号」，**同一天从 1 开始复用**。之前单独试的那 1 条
已经建了 `...0001`，批次里又用了同样的号段。

**问题在于**：`send_test.py` 的回包核对只按「有没有回包」算 —— 这条业务失败
被算成"成功回包"，回复率 100%、`rounds_abnormal: 0`。**报告会显示"全绿"。**

**修法**：`_on_reply_body` 里**复用已有的 `json.loads`** 顺手取 `ErrID`，
交给 `perf_stats.record_biz_result()` 分类计数：

| 回包 | 记为 |
|---|---|
| `ErrID == 0` | `biz_ok` |
| `ErrID != 0` | `biz_fail`（按 `Errmsg` 归类到 `biz_fail_msgs`） |
| 没有 `ErrID` 字段（如 Mock 的 `{"status":"OK"}`） | `biz_unknown` —— **既不算成功也不算失败** |

判据 `--max-biz-fail`：`0`=不判、`-1`=零容忍、`N`=超过 N 条才报。

**两个实现要点**（都是踩过的坑）：

1. **不能重复解析**：`json.loads` 本来就在跑（为了抓 `Ref`），所以只多一次
   `dict.get()`。别为了这个功能再加一次解析。
2. **只统计"我们的"回包**：`record_reply()` 现在返回 `True/False` 表示这条
   是否对得上本次 `request_id`，`_on_reply_body(..., ours=...)` 据此决定要不
   要记业务结果 —— 否则会把**别人的回包**的业务成败混进来，
   与 §2.4 的回复率 >100% 是同一个坑。

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

### 3.2.1 业务流模式（`--flow`）为什么必须每轮重生成 modify/remove 表

**背景**：`datahub_test` 那边只跑 query 查询接口，所以 soak 是"单接口循环"。
策略方向这条链路是**增加/修改/删除**，要按业务顺序成组跑才有意义：
一组 = `create → modify → remove` 各 batch 条，循环。

**硬约束**：`modify`/`remove` 的 `Ref` 必须是平台上**真实存在**的单号
（否则 `ref not exist`），而单号是**平台在 create 回包里给的**。

单号的生成规则（[../interfaces/_common.py](../interfaces/_common.py) 的 `make_ref`）：

```python
make_ref(n) = "YYYYMMDD" + "%06d" % n     # 如 20260929000001
```

`__REF{i}__` 在**发送当天**展开成这个固定值 —— 注意它**不随轮次变化**。
推论（这个坑很关键）：

* 同一轮里，create 第 i 行永远会去建 `2026092900000i` 号单；
* 如果上一轮的 remove **没删干净**，这一轮的 create 必然 `ref already inserted`；
* 反过来，只要每轮 remove 真删掉，下一轮 create 就能**重建同样的号** ——
  **一个完整生命周期刚好可以干净循环**，这正是 `--flow` 成立的前提。

所以 `--flow` 的实现是：
① create 用 `--refs-out` 把**本轮回包里的真实单号**落盘成 `refs.json`
（`send_test.py` 的 `_on_reply_body` 抓 `Ref`）；
② 调 `make_excel.py --interface modify --bulk-normal N --ref-map <refs.json>`
用**账号→单号**映射回填，现生成 `modify.xlsx`；③ 发 modify；
④/⑤ 对 remove 重复 ②③。

**为什么不能"只生成一次反复用"**：`__REF` 展开带当天日期。跨天后
单号整体变成新的日期段，预先写死的旧表会一条都对不上（全 `ref not exist`）。
用 `--ref-map` 拿回包真值还额外解决了一个问题：**不依赖"create 一定按行序成功"**，
跳过失败单也不会错位（对比 `--ref-seq` 的"靠行序对齐"，那个是备选方案）。

**实施位置**：`run_flow_cycle()` / `gen_table()` / `ensure_create_table()`。

### 3.2.2 mock 的 `--ref-echo`：不补这个，业务流跑不起来

原 `mock_strategy.py` 的回包是**固定字符串** `{"status":"OK"}`，
**不含 `Ref`** —— 这是 mock 的一个保真缺口：

* 真实平台的 create 回包是
  `{"Ref":"20260929000001","Errmsg":"insert success","ErrID":0}`
  （见 `out/performance/*_refs.json` 实测：客户端发 `__REF1__` 展开值，
  平台回的 `ref` 是同一个值）；
* 而 mock 固定回包时，`send_test` 的 `_on_reply_body` 取不到 `Ref` →
  `refs.json` 不落盘 → 第 ② 步直接失败（**任何人都跑不通**）。

所以给 [../mock_strategy.py](../mock_strategy.py) 加了 `--ref-echo`：
从请求的 `create`/`modify`/`remove` 子对象里取 `Ref` 原样回带，
仿真实平台行为。跑 `--flow` 必须开它。

> 这个坑的教训与 §1.1 是同一类：**mock 比真平台"更宽容"或"更简陋"时，
> 本地跑通不代表链路成立**。改任何依赖回包内容的逻辑，都要先确认
> mock 的回包是否具备真实平台的那些字段。

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
构造 `MainWindow` 会真实改写 `config.ini`，测完要还原
（它现在不进版本库，所以是"先备份再还原"，不能靠 `git checkout`）。

### 3.5.1 `config.ini` 为什么不进版本库

它含 **Redis 密码 / SSH 密码 / 各人的绝对路径**（`stats_out`），
提交上去等于把凭据写进仓库。已在 `.gitignore` 屏蔽。

**没有 example 模板**（曾加过一个，用户不要）：仓库里干脆不放配置文件，
首次使用由 GUI 退出时自动生成，或用户自己照 `config.py` 的 `DEFAULTS` 写。

**代码在 config.ini 缺失时必须能跑**（新克隆的人不会一开始就有它）：

* `config.py:load()` 先读文件、缺的项用 `DEFAULTS` 补 —— 已实测缺文件也能工作
* `gui_test.py` 的 `load_config()` 同理（`if os.path.exists(CONFIG_PATH)`）
* 命令行参数优先级永远高于配置（`redis_kwargs` 的 `pick()`）

> ⚠️ **遗留问题：密码仍硬编码在代码里。** `config.py` 的
> `"pwd": "QianLong@2026&"`，以及 `check_env.py` / `resp_min.py` /
> `soak_test.py` / `gui_test.py` 的默认值里都有（共 6 处）。
> 屏蔽 `config.ini` **拦不住**这些 —— 它们照样随代码进仓库。
> 要彻底干净得把默认值换成空串或占位符（如 `"CHANGE_ME"`），
> 由 `config.ini` / `--pwd` 传入。**尚未处理**（用户明确表示暂不管密码）。

### 3.6 稳定性测试跑远程 Linux（ssh_runner.py）

**为什么**：长稳跑几小时甚至过夜，Windows 会休眠/断网/被锁屏影响；
而且现场环境是 Linux（`datahub_test` 的 `.so` 同样只能在 Linux 上跑）。
实现参考 `datahub_test/gui_test.py` 的 `SshWorker`，抽成独立模块
[../ssh_runner.py](../ssh_runner.py)（便于复用与单测）。

**三个必须记住的点**（都踩过）：

1. **shell 转义**：Redis 密码是 `QianLong@2026&`，而 `&` 在 shell 里是
   **后台执行符**。远端命令必须对每个参数 `shlex.quote`，否则
   `--pwd QianLong@2026& --db 0 ...` 会被切成两条命令，`&` 之后的参数全丢。
   实施位置：`gui_test._build_remote_cmd`。

2. **nohup 要断开三个 fd**：只写 `nohup ... &` 不够 —— SSH 通道会因为
   "还有进程持有 stdout/stderr"而不释放。要 `setsid nohup CMD > LOG 2>&1
   < /dev/null & disown`。实测这样 0.7 秒就返回，后台进程照跑。
   实施位置：`ssh_runner.build_nohup_cmd`。

3. **主动取结果不能用"只下新增"逻辑**：`run(cmd, download=...)` 会在执行前
   记快照、执行后只下新增文件（避免拉一堆历史）。但"跑完后再点下载结果"
   这个场景**快照是空的**，走同一逻辑会把刚产出的文件当成历史文件跳过。
   所以另有 `download_all()` 无条件下载。实施位置：`ssh_runner._download`
   的 `only_new` 参数 + `download_all`。

**远端依赖**：只需 Python 3.8+ 与 `openpyxl`（缺了退化成 CSV）。
**不需要 `redis-py`** —— 本目录一律用纯 socket 的 `resp_min.py`（§见 requirements
的说明），所以 Linux 3.9 上不用装任何 Redis 客户端。

⚠ **上传会覆盖远端同名文件**：GUI 默认用独立目录
`/home/yangsh/so_test/strategy_soak`。别把远端目录指到现场在用的目录
（那边有 `send_test.py` / `soak_test.py`，会被本项目的版本覆盖）。

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
