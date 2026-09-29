# 使用指南

面向使用者。查参数请去 [reference.md](reference.md)；
改代码前请看 [dev-notes.md](dev-notes.md)。

---

## 1. 环境准备

Python 3.8+。用仓库根目录的 `venv`（`D:\Code\Python\多线程\venv`）即可，
已装好 PySide6 + openpyxl。依赖清单见 `requirements.txt`。

下文命令都假设已经 `cd strategy_test`；不想 `cd` 就把
`..\venv\Scripts\python.exe` 换成绝对路径
`D:\Code\Python\多线程\venv\Scripts\python.exe`。

---

## 2. 图形界面（推荐）

```powershell
cd strategy_test
..\venv\Scripts\python.exe gui_test.py
```

**首次跑起来的三步**：

1. **右栏 → 服务管理 → 「启动 Mock 策略平台」**
   （默认「自应答编号」勾着，它会自己占编号 1，即下发流 = `ST-1`）
2. **左栏 → 1. 测试数据**：勾 `create`
   （这里**不会**读 Excel，勾选是瞬时的；条数在点发送时才统计）
3. **左栏 → 2. 发送参数 → 发送范围**：用例类型选 `normal`
4. **左栏 → 2. 发送参数 → 规模与速率**：并发 8、总条数 2000
5. **底部 → 「开始发送」**

### 界面布局

* **左栏**管「发什么」，从上到下一条主流程：
  * **1. 测试数据** —— 管**磁盘上的数据**：勾接口（读哪个 `data/*.xlsx`）、
    批量账号数、Ref 回填、生成压测数据。只写文件，不发送。
  * **2. 发送参数** —— 管**这一批怎么发**，内含四个分区：
    发送范围（`--type` / `--cases`）→ 规模与速率 → 回复处理 → 稳定性测试。
* **右栏**管「连哪儿、以谁身份、结果存哪」＋服务管理
* **下栏**是运行日志 / 统计汇总

> 「用例类型」「指定用例」是**发送时的筛选器**（`send_test.py` 的
> `--type` / `--cases`），不改任何数据，所以归在「2. 发送参数 → 发送范围」，
> 与 `datahub_test` 的摆法一致。而「接口勾选」虽然也是 `--interface`，
> 但它决定**读哪张表**，所以留在「1. 测试数据」。

实时日志在「运行日志」页，跑完自动切到「统计汇总」，可一键导出 Excel。
界面参数会记忆到 `config.ini`。

### 日志页工具条

| 控件 | 作用 |
|---|---|
| **清空日志** | 只清窗口显示，**不动** `out/logs/` 下的文件；任务运行中会先确认 |
| **每次发送前自动清空** | 勾上后每次发送/预览先清空，只看本次输出 |

> 日志缓冲区有上限，超了丢最旧的；完整日志始终在 `out/logs/{label}.log`。

### 底部按钮

| 按钮 | 作用 |
|---|---|
| 预览报文（不发） | 只打印将要 XADD 的内容，不写 Redis |
| 运行稳定性测试 | 对勾选的接口依次跑 `soak_test.py`，见 §7 |
| 开始发送 | 按「用例类型」下拉框 + 当前筛选发送（normal / error / destroy / all） |
| 停止 | 中止正在跑的发送任务 |

「统计汇总」页上方还有「刷新统计汇总」（扫 `out/performance/*_stats.json`
汇总成表）和「导出汇总 Excel」。

---

## 3. 命令行

开**两个**终端，都先 `cd strategy_test`：

```powershell
# 终端 1：模拟策略平台（占编号 1，即下发流 = ST-1）
..\venv\Scripts\python.exe mock_strategy.py --host 192.168.1.137 --db 0 --assign-id 1

# 终端 2：手动 XADD 发报文（8 线程发 2000 条正常单，并测真实 RTT）
..\venv\Scripts\python.exe send_test.py --host 192.168.1.137 --db 0 --assign-id 1 `
    --interface create --type normal --workers 8 --max 2000 --wait 8 --sync-probe 20
```

跑完终端 2 打印统计表，并在 `out/` 下留三份产物：

```
out/logs/quickstart.log                      # 完整日志
out/performance/quickstart_stats.json        # 汇总指标
out/performance/quickstart.xlsx              # 汇总 + 按秒 + 错误（3 个 sheet）
```

### 常用变体

```powershell
# 破坏测试（畸形报文）：类型选 destroy
... send_test.py --assign-id 1 --type destroy --workers 8 --max 1000 --wait 8

# 只发不收回包（纯压发送端）
... send_test.py --assign-id 1 --no-reply --workers 8 --seconds 10

# 按时间跑 10 秒（填了它「总条数」被忽略，GUI 里会自动置灰）
... send_test.py --assign-id 1 --seconds 10 --workers 8

# 限速 100 条/秒
... send_test.py --assign-id 1 --rate 100 --max 1000

# 只发指定的破坏用例（写 --cases 自动把 --type 放宽为 all）
... send_test.py --assign-id 1 --cases C201,C202,M201

# 列出所有用例
... send_test.py --list-cases --type all
```

> 目标 Redis 默认 `192.168.1.137 db0`。要改就编辑 `config.ini`，或每次都带
> `--host/--db`。**不要往 136 的 Redis 写**（同事在那边测别的）。
> GUI 里 host 不是 137 会弹窗二次确认。

### 3.1 看发出去的报文

| 方式 | 怎么做 | 特点 |
|---|---|---|
| **① GUI 预览**（最方便） | 勾好接口/类型 → 点 **「预览报文（不发）」** | 打印 `XADD ... task {...}`，**不发数据**。⚠ 每条只打印前 **1500** 字符 |
| **② 看流里真实的**（最接近真相） | `python tests/_show_stream.py --stream ST-50 -n 5` | 直接从 Redis 读回**真正写进去的**内容，含 `request_id`，不截断 |
| **③ 落盘成文件** | `--no-send --dump out/payload.jsonl` | 完整 JSON 逐行写入，适合存档/对比 |
| **④ 看历史** | `out/logs/{label}.log` | 只记条数/统计，**不含报文内容** |

```bash
# ② 看实际发出去的（推荐排查用）
python tests/_show_stream.py --stream ST-50 -n 5          # 最近 5 条
python tests/_show_stream.py --stream ST-50 -n 1 --json   # 格式化缩进
python tests/_show_stream.py --stream ST-50 --full        # 不截断
python tests/_show_stream.py --stream ST-50 --out d.jsonl # 导出

# ③ 发送前预览并落盘（不写 Redis）
python send_test.py --interface create --cases C001 --no-send --dump out/payload.jsonl
```

---

## 4. 打真平台

真平台和 mock 走**同一套协议**，所以基本不用改代码，只要做对两件事：
**目标指向真平台那条流**、**别把 mock 一起起**。

### 第 0 步：只读体检

```bash
python check_env.py                       # 只读，扫 136 与 137 的 db0/db1
python check_env.py --host 192.168.1.136 --db 0
```

它会告诉你：真平台在哪、编号几（看有没有 `strategysrv-<id>` 键，
有则 `下发流 = ST-<id>`）；环境活不活；有没有人在订阅频道。

> 该脚本**只读**（PING/KEYS/TYPE/XLEN/XINFO/NUMSUB/CLIENT LIST/GET），
> 可在生产环境安全运行。

### 第 1 步：三条铁律

| # | 铁律 | 为什么 |
|---|---|---|
| 1 | **不要再起 `mock_strategy.py`** | 它会用 `ST-<id>-w0` 加入 `user_group`。Streams 同组是**负载均衡不是广播**，它会**抢走真平台一半的消息** |
| 2 | **不要再起 `mock_datahub.py`** | 真中台自己会分配编号，你再起一个会跟它抢应答 |
| 3 | **只跑 `send_test.py`** | 它就是"数据中台发报文"的角色 |

### 第 2 步：发送

```bash
# --assign-id 填 check_env.py 查到的真实编号（例如 0）
python send_test.py --host 192.168.1.136 --db 0 --assign-id 0 \
    --interface create --type normal --workers 1 --max 1 --wait 10
```

**建议先只发 1 条、并发 1**，确认能正常收到回包，再逐步放量。

### 第 3 步：安全闸

从 v2 起 `send_test.py` 带**自动安全闸**：发送前检查目标流的 `user_group`
里有没有「不是本工具创建的消费者」。发现外来消费者就**拒绝发送**并提示，
确认无害后加 `--force-live` 放行。`mock_strategy.py` 有同样的闸（措辞不同）。

`safety.py` 是两者共用的判据；区分方法与实现细节见
[dev-notes.md](dev-notes.md) 第 4 节。

### ⚠️ 风险会随 Redis 切换而反转

真实策略平台**当前**连 `192.168.1.136`，之后会**切到 192.168.1.137**，
而本工具**默认目标恰好是 137**：

```
今天：  默认 137 = 干净环境  → 跑默认命令是安全的
切换后：默认 137 = 真实环境  → 跑默认命令会打到真平台！
```

切换后务必：① 先跑 `check_env.py` 确认 137 有没有真平台；
② 别在真平台占用的编号上起 `mock_strategy.py`；
③ 要测就挑没人用的编号（如 `--assign-id 50`）。

**已知的真实环境**（详见 [dev-notes.md](dev-notes.md) 第 5.4 节）：

| 机器 | 下发流 | 消费者 | 备注 |
|---|---|---|---|
| 192.168.1.136 db0 | `ST-0` | `ST-0` | 真数据中台也在这台，回包带真实订单号 |
| 192.168.1.137 db0 | `ST-50` | `ST-50` | 现场真平台连的是**这台** |

⇒ 两台风险都高。只想验证联调就**另开一个高位编号的干净流**（如 `--assign-id 94`）。

---

## 5. Pwd 字段加密（`account` / `pwdUpdate` 必读）

`account`（MsgType=18）和 `pwdUpdate`（MsgType=17）里的 `Pwd` **不是明文密码**，
而是**两层 AES-256-CBC + Base64** 密文。策略平台要拿它去柜台登录/改密，
不按规则加密就登录不上。

```
内层 = encrypt_string(明文密码, 账号)          # 账号不带 _7_6 后缀！
外层 = encrypt_string(内层结果, 当前日期字符串)  # 形如 "20260901"
Pwd  = 外层
```

算法细节（逐行对齐 `pwdEncode.cpp`）：`base64( IV(16字节随机) || AES-256-CBC(pkcs7(data)) )`；
key 拷进 32 字节、不足补 `0x00`；**IV 随机前置**在密文最前。
⚠️ 随机 IV ⇒ 同一密码每次加密结果都不同，**这是正常的**。

**不用手动算** —— Excel 的 `Pwd` 列填**明文**，发送时自动按本行账号加密：

```
data/account.xlsx 里 A001 行： Pwd 列 = 123123
               ↓ build_payload 时
报文里： "Pwd": "skD3CmdjhSoX9PaaB69RTnqcrEOHA25m4/uMRCN+iuR3Ha...=="
```

手工算/反解（排查用）：

```bash
python pwd_encode.py                          # 自测（含真实样本验证）
python -c "import pwd_encode as p; print(p.encode_pwd('123123','010100011300'))"
python -c "import pwd_encode as p; print(p.decode_pwd('<密文>','010100011300'))"
```

要发**畸形密文**（测解密容错）用 `Pwd_raw` 列，填什么就发什么、不再加密。

> ⚠️ **传明文会被静默吞掉**：真平台不报错、也不回包，消息被正常消费
> （`lag=0`、`pending=0`），但回包流上永远等不到 —— 容易误判成"平台挂了"。
> `pwdUpdate` 的 `P105` 用例把"传明文"当负例保留（期望：不回包）。

---

## 6. 用例

### 类型

| 类型 | 含义 |
|---|---|
| `normal` | 合法报文（照抄真实流量的字段结构），用于性能/联通性基线 |
| `error` | 业务层非法但结构合法（空字段、账号不匹配、Ref 不存在…），期望平台明确拒 |
| `destroy` | 畸形/极端报文，测策略平台健壮性（不崩、不泄漏、不误处理） |

### 数量

`make_excel.py --interface all --list` 可随时打印：

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

### `destroy` 覆盖什么

* **结构级**（x201~x2xx）：非法 JSON、非对象、空对象、MsgType 缺失/未知/负数/
  字符串/null、子对象类型错、多子对象并存、MsgType 与子对象不匹配、500 个垃圾字段、
  200 层嵌套、重复键注入、1MB 大 JSON、Shareholders 异常、账号字段互相矛盾等
* **字段级 fuzz**（D200+）：对关键路径逐个灌 25 种畸形值 —— 空串、全空格、
  JSON null、超长 1000/10000、控制字符、NUL 字节、SQL 注入、XSS、格式化串、
  emoji 长串、超大/极小整数、科学计数、NaN/inf、十六进制、非法布尔等

token 机制与 `datahub_test/interfaces/_common.py` 一致：配置里存占位符，发送时才展开。
动态日期有两种格式，**别混用**：

| token | 展开 |
|---|---|
| `__TODAY__` / `__TODAY_D30__` / `__TODAY_D_1__` | `YYYY-MM-DD`（`ValidDate` 用） |
| `__TODAY8__` / `__TODAY_PLUS7__` | `YYYYMMDD`（`CondTime.TriggerDate` 用） |

### 压测数据扩充（`--bulk-normal`）

默认每个接口 normal 只有 2~5 条，压测时 `--max` 会**循环复用**同一批报文 ——
create 用重复 `Ref` 会得到 `ref already inserted`，测出来的是"业务失败路径"。
所以提供批量扩充（**五个接口都支持**）：

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

**五个接口共用同一账号号段**（`010100011301`~`010100021300`）—— 同一个账号
既要能登录、又要能下单/改单/删单，必须对齐。生成规则要点：

* `UniqueAccount` = `<FAccount>_<AccountType>_<AccAtt>`，**必须跟着 FAccount 变**
  （照抄模板会被判"账号与唯一账号不一致"）
* `create.Ref` 用 `__REF1__`~`__REF10000__`（逐行不同），发送当天展开成
  `YYYYMMDD+6位序号`，天然唯一
* `account.Pwd` 存**明文**，发送时按**本行账号**加密（账号不同密文就不同，这是对的）
* 股东号：沪 `A`+9 位、深 10 位数字（实测样本 `A442523077` / `0199908393`）
* 字段名用实测的大写形式（`CondPrice`/`Op`/`TriggerPercent`…）；
  同事样本里的小写形式（`cond_price`/`op`…）实测 **0 次**出现，不能用

> ⚠️ **modify / remove 在真实流量里一条都没有**（136 ST-0 只有 create=4 和 account=18）。
> 它们的结构依据是协议文档 + 我们实测发过去拿到 `update success`/`remove success`
> 的那几条，**证据强度弱于 create/account**，现场被拒要优先怀疑字段名/结构。

> ⚠️ **账号只是"格式合法"，不代表柜台上真的存在。** 要真能登录/下单成功，
> 账号必须先在柜台批量开立。否则这一万条大概率是"查不到账号"的业务失败。

> ⚠️ 批量行会**替换**掉原来手写的 normal（error/destroy 全部保留）。
> 恢复小表：`python make_excel.py --interface all`。
> `--bulk-normal` 与 `--ref-spec` **互斥**。

### 发送顺序是硬约束

`modify`/`remove` 的 `Ref` 必须是平台上**真实存在的单号**，否则得到 `ref not exist`。

```
1. account    账号先能"登录"
2. create     产生 1..N 号单（同时落盘 refs.json）
3. modify     改第 1..N 号（可选）
4. remove     删第 1..N 号
```

> **删除不可逆**。同时测 modify 和 remove 就按上面顺序；只想测 remove 就 `create → remove`。

**做法 A（推荐）：用 create 实跑落盘的 `refs.json`**

`send_test.py` 发 `create` 时会自动从回包抓真实单号并落盘：

```
★ 抓到 10000 个条件单号，已写入: out/performance/create_xxx_refs.json
  下一步可用它生成 modify/remove 用例：
    python make_excel.py --interface remove --bulk-normal 10000 --ref-map <该文件>
```

```bash
python make_excel.py --interface remove --bulk-normal 10000 \
    --ref-map out/performance/create_xxx_refs.json
```

单号是**平台回包给的真值**，不依赖"create 一定按行序成功"，跳过失败单也不会错位。

**做法 B（备选）：靠行序对齐 `__REF{i}__`**

五张表批量行行序一一对应 —— create 第 i 行建出当天第 i 号单，
modify/remove 第 i 行引用它。若当天**已经发过** N 张单，序号要接着排：

```bash
python make_excel.py --interface remove --bulk-normal 10000 --ref-seq 501
```

不填就是 `1`。**做法 B 的数据不能跨天用**（表里存的是占位符，单号发送时才按当天日期展开）；
**做法 A 写的是静态单号，不受日期影响**。

---

## 7. 稳定性测试

不做一次性压测，而是**连续跑几小时**，看指标是否随时间劣化。
做法：不改发送逻辑，靠"反复调用 `send_test.py` + 汇总"实现。

**有两种模式，按需要选**：

| 模式 | 一轮是什么 | 适用 |
|---|---|---|
| **单接口**（默认） | 重复发同一个接口 | 纯压某个接口 / 发 destroy 测健壮性 |
| **业务流**（`--flow` / GUI 勾选） | `create → modify → remove` 各 batch 条 | **模拟真实业务循环**（推荐做长稳） |

> `datahub_test` 那边只跑 query 查询接口，所以是"单接口循环"；
> 策略方向这条链路是 **增加/修改/删除**，所以要按业务顺序成组跑。

### 7.1 业务流模式（`--flow`）——推荐

一组 = **1w 个 create → 1w 个 modify → 1w 个 remove**（数量由 `--batch` 定），
一组跑完接着下一组，如此循环。

```bash
# 一组 1w 条：create 1w -> modify 1w -> remove 1w，跑 5 组
python soak_test.py --assign-id 94 --flow --batch 10000 --rounds 5 \
    --workers 8 --wait 30 --clean monitor

# 长稳 8 小时
python soak_test.py --assign-id 94 --flow --batch 10000 --hours 8 \
    --workers 8 --wait 30
```

**Ref 是动态的，所以 modify/remove 表每轮都要重生成** —— 这正是流程的核心：

```
① create  发 batch 条  ── 从回包抓真实单号 -> out/soak/<组>/refs.json
② 生成 modify 表          make_excel --interface modify --bulk-normal N --ref-map <refs.json>
③ modify  发 batch 条     引用第 ① 步真实存在的单号
④ 生成 remove 表          同样用 refs.json
⑤ remove  发 batch 条     把第 ① 步造的单删掉
   └─ 删干净了，下一组 create 才能重新造出同样的号（否则 ref already inserted）
```

所以：**必须按 create→modify→remove 的顺序**，不能跳步。任一步失败就中止本组、
直接进下一组（组与组独立），并在日志里说明原因。

> **前置条件**：平台回包**必须带 `Ref`**（真实平台是带的）。
> 用自带 mock 验证时记得加 `--ref-echo`，否则 mock 回固定的 `{"status":"OK"}`，
> 抓不到单号、第 ② 步会直接失败：
> ```bash
> python mock_strategy.py --host 192.168.1.137 --db 0 --assign-id 94 --ref-echo
> ```

> **`--flow` 的约束**：只能配 `--type normal`；不能与 `--rotate` 同用
> （每轮都要重生成表，行号轮换没意义）；只能 `--clean monitor`
> （清理回包流会干扰 refs 抓取）。

> **表从哪来**：`create.xlsx` 只需有足够行数（`--batch` 行 normal），
> 没有会自动 `--bulk-normal` 生成一次；`modify`/`remove` 每轮现生成。
> ⚠ 生成前**别用 Excel/WPS 打开这些表** —— 被占用时 `make_excel` 会另存成
> `_v2.xlsx` 而**原表不更新**，soak 会检测到并报错中止（不会静默发旧表）。

### 7.2 单接口模式（默认，与以前一致）

**两种入口，等价**：

1. **GUI**：左栏「2. 发送参数 → 稳定性测试」→ 勾选「启用稳定性测试」
   → 设好结束条件/每轮条数 → 点底部「运行稳定性测试」。
   接口沿用「1. 测试数据」的勾选，类型沿用同面板「发送范围」的选择，
   目标流沿用右栏「策略平台身份」；勾选的多个接口会依次各跑一场。
2. **命令行**：

```bash
# 8 小时，每轮 500 条 normal，只监控不清理（最安全）
python soak_test.py --assign-id 50 --interface account --type normal \
    --hours 8 --batch 500 --clean monitor

# 短测：跑 5 轮就正常收尾（--rounds 优先于 --hours）
python soak_test.py --assign-id 50 --interface account --type normal \
    --rounds 5 --batch 200 --workers 4

# 轮换用例（避免反复发同一批）
python soak_test.py --assign-id 50 --interface account --type normal \
    --hours 8 --batch 1000 --rotate

# 打真平台要显式加 --force-live（透传给 send_test.py）
python soak_test.py --assign-id 50 --interface account --type normal \
    --rounds 3 --batch 100 --force-live

# 长稳建议把四个阈值都设上（不设=不判，只记进 trend.csv）
python soak_test.py --assign-id 50 --interface account --type normal \
    --hours 8 --batch 500 --max-lag 1000 --max-pending 1000 \
    --max-outstanding 100 --max-timeout-reply 100
```

### 7.3 输出与判据

输出（`out/soak/`）：

| 文件 | 内容 |
|---|---|
| `..._trend.csv` | **每轮指标时间序列**（核心产物，可直接画图）<br>业务流模式下每段一行，多一个「阶段」列（create/modify/remove） |
| `..._summary.json` | 整体汇总（含实际生效的 `criteria`） |
| `..._soak.log` | 编排日志（每段一行关键指标 + 异常） |
| `..._rounds/` | 异常轮的明细与 `refs.json`（默认只留异常轮） |

其余参数（`--gap` / `--keep-round-stats` / `--round-timeout` / `--soak-out`）
语义同 `datahub_test/soak_test.py`；未识别参数原样透传给 `send_test.py`。

### 判据：三类信号，各有各的用途

| 信号 | 参数 | 默认 | 能看出什么 |
|---|---|---|---|
| **回复率** | `--min-reply-rate` | **按类型**：normal=99，error/destroy/all=不判 | 平台漏处理、回包链路断了 |
| **lag / 未ACK** | `--max-lag` / `--max-pending` | 0（不判） | 区分「平台没读」与「读了卡住」 |
| **超时未回 / 在途** | `--max-timeout-reply` / `--max-outstanding` | 0（不判） | 这一轮积压了多少 |

业务流模式下**三段用同一套判据**（共用 `judge()`），任一阶段异常就把整组记为异常组。

回复率**按用例类型分档**，不是一刀切：

* `normal` = 99%：压测数据本来就该条条有回包（实测 30/30、2000/2000 全回）。
* `destroy` / `error` / `all` = 0（不判）：畸形报文大量不回包是**平台的正常行为**
  （平台读了、XACK 了、故意不回，如 Pwd 非法密文的 `AD232`）。

> ⚠️ **destroy 的回包行为并不固定，别想当然。** 2026-09-29 实测同一天里：
> 上午 account destroy 发 96 回 0（平台压根没读，`lag=96`），
> 下午同样 96 条却 96/96 全回。所以「destroy 不回包」只是"默认别误报"，
> 真要盯 destroy 的回包请显式给 `--min-reply-rate`。

> **回复率的分母分子**：分子只算**能对上本次发送 `request_id` 的回包**。
> 回包流 `DataHub_reply_stream` 是**多条 `ST-*` 共用的全局流**，XREAD 会读到
> 别人的回包以及 `--sync-probe` 自己发的那些 —— 对不上的记进 trend 的
> 「非本次回包」列，不参与判据。所以回复率**恒 ≤ 100%**。

> 阈值默认大多关闭（只记录、不判），这是刻意的：避免长稳跑了一半才发现
> 判据本身在误报。**长稳建议四个阈值都设上**，尤其是 `lag`/`未ACK` ——
> 回包为 0 时只有它能告诉你平台是"没读"还是"读了卡住"。

判据的设计理由与实测证据见 [dev-notes.md](dev-notes.md) 第 2 节。

---

## 8. 平台异常怎么排查

判据是目标流消费组的 `lag` / `未ACK`，**不是回复率**。核心区分：

| 本地未回包 | 未ACK(PEL) | 结论 |
|---|---|---|
| 有 | **有** | 平台收到了但**卡住/崩了** |
| 有 | 没有 | 平台**压根没读**，或**读了但故意不回** |

工具（都在 `tests/`，只读、可随时跑）：

```bash
python tests/_ack_gap.py --stream ST-50                      # 平台停在哪
python tests/_find_culprit.py --assign-id 50 --type destroy  # 逐条揪凶手
python tests/_verify_bulk.py                                 # 复核批量数据
```

> 平台是**多线程**的，**事后反推不出是哪一条** —— 要精确定位得用
> `_find_culprit.py` 单条注入。原理与判定标准见
> [dev-notes.md](dev-notes.md) 第 2 节。

---

## 9. 输出与注意

**输出**：

* `out/logs/{label}.log`：完整运行日志
* `out/performance/{label}_stats.json`：汇总指标
* `out/performance/{label}.xlsx`：汇总 + 按秒明细 + 错误分布（无 openpyxl 时退化成 CSV）

指标口径见 [reference.md](reference.md)。

**注意**：

* **目标 Redis 用 137**，db0。136 上的 Redis 是同事在测别的东西，别去写。
* 本工具会往目标 Redis **真实写入** `ST-<id>`、`ST-<id>-reply`、`DataHub_reply_stream`。
  仅限测试环境；联调真中台前先确认不会污染生产流。
* `--sync-probe N` 会额外真实写入 N 条报文，所以流里条数 = 总条数 + N。
* `.so` 是 Linux 库，Windows 上只能跑 Mock / 发送器，加载插件要在 136。
* Linux 上的 python3.9 通常没装 `redis` 模块，所以 `resp_min.py` 用纯 socket 实现。
* Windows 控制台打印畸形字符可能报编码错，建议 `PYTHONIOENCODING=utf-8`。
* `mock_strategy.py --read-count 1` 可复刻真插件，但会让 mock 成为瓶颈，
  压测时请用默认值 100。
* 测试完记得清掉自己造的号段流（**真平台在用的那条一个都不能碰**）：
  `XINFO CONSUMERS` 确认无真实消费者、`XLEN=0` 且无 pending 后再 `DEL`。
