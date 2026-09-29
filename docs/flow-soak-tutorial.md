# 业务流稳定性测试 · 完整教程

从零跑通 **一组 = create → modify → remove**(各 N 条)的稳定性测试,
外加**跑在远程 Linux + nohup 后台**的做法。

> 适用版本：`soak_test.py` 的 `--flow` 模式 + GUI 的「业务流模式 / 远程 Linux」。
> 判据参数速查见 [reference.md](reference.md) 第 5 节,踩坑背景见 [dev-notes.md](dev-notes.md) §3.2。

---

## 0. 先理解"一组"是什么

```
一组 = create N 条  →  modify N 条  →  remove N 条
      （造单）          （改单）         （删单）
然后循环下一组
```

**为什么必须这个顺序**:`modify`/`remove` 的 `Ref` 必须是平台上**真实存在**的单号,
而单号是 `create` 回包里平台给的。所以工具会在每组的 create 之后:

```
① create 发 N 条  ──→ 从回包抓真实单号 → out/soak/<组>/refs.json
② 用这些单号现生成 modify 表 → 发 modify N 条
③ 同样现生成 remove 表      → 发 remove N 条
   └─ 删干净了,下一组 create 才能重建同样的号
```

**一组的实际发送量 = 3 × N**。`--batch 10000` 就是一组 3 万条。

---

## 1. 准备:让平台能返回单号

`--flow` 依赖平台回包里的 `Ref` 字段。分两种情况:

### 情况 A:打真平台 → 什么都不用做

真平台的 create 回包本来就带,例如:

```json
{"Ref":"20260929000001","Errmsg":"insert success","ErrID":0}
```

### 情况 B:用自带 Mock → **必须勾「回包带回 Ref」**

Mock 默认只回固定的 `{"status":"OK"}`,**抓不到单号**,业务流第 1 步就会失败。

* GUI:右栏「服务管理」→ 勾 **「回包带回 Ref」** → 再启动 Mock
* 命令行:加 `--ref-echo`

> GUI 会帮你看着:如果你勾了业务流模式却没勾这个,Mock 那个复选框会**变红加粗**警告。

---

## 2. 本地试跑(先在 Windows 验证流程)

**目标**:5 分钟确认三段都通,再考虑长稳。

### 2.1 起平台

| 场景 | 操作 |
|---|---|
| 用 Mock | 右栏「服务管理」→ 勾「自应答编号」+「回包带回 Ref」→「启动 Mock 策略平台」 |
| 打真平台 | 先 `python check_env.py` 查编号,然后**不要**起 Mock |

### 2.2 勾业务流模式

左栏「2. 发送参数 → 稳定性测试」:

1. 勾 ☑ **启用稳定性测试**
2. 勾 ☑ **业务流模式:一组 = create → modify → remove**

勾上后会自动发生(不用你管):

* 「1. 测试数据」的接口勾选框**置灰** —— 业务流的接口是固定的,这里勾什么都不参与
* 「用例类型」「轮换用例」「流处理」也置灰 —— 固定 normal / 不轮换 / monitor

### 2.3 设参数(第一次这样填)

| 项 | 值 | 说明 |
|---|---|---|
| 结束条件 | 按轮数 | 先用轮数,确定性强 |
| 轮数 | **2** | 跑通就行 |
| 每轮条数 | **10** | 一组 = 30 条 |
| 并发线程 | 2 | |
| 等回包 | 5 | |
| 回复率下限% | -1 | = 按类型自动(normal 判 99%) |

提示行会显示:

```
将执行：共 2 组 × (create+modify+remove 各 10 条) = 约 60 条；目标流 ST-94
判据：回复率99%(normal)；发送失败>0；lag/未ACK/超时/在途 均未设阈值
```

### 2.4 跑

点底部 **「运行稳定性测试」** → 确认弹窗 → 看下面日志:

```
--- 第 1 组开始（剩余 1 轮）: create → modify → remove，各 10 条 ---
    第 1 组 create: 发送=10 回包=10 失败=0 回复率=100.0%
  create 抓到 10 个真实单号 -> refs.json        ← 关键：抓到真单号
  生成 modify 表: OK（0.4s）
    第 1 组 modify: 发送=10 回包=10 失败=0 回复率=100.0%
  生成 remove 表: OK（0.4s）
    第 1 组 remove: 发送=10 回包=10 失败=0 回复率=100.0%
第 1 组完成: 三段共发送=30 回包=30
```

**判断跑通的标准**:

* 三段都是「回复率 100%」
* 出现 `create 抓到 N 个真实单号`
* 两次「生成 xx 表: OK」
* `rounds_abnormal: 0`

### 2.5 命令行等价写法

```bash
python mock_strategy.py --host 192.168.1.137 --db 0 --assign-id 94 --ref-echo
# 另开一个终端
python soak_test.py --assign-id 94 --flow --batch 10 --rounds 2 \
    --workers 2 --wait 5
```

---

## 3. 正式长稳(本地)

跑通了就把量提上去:

```bash
python soak_test.py --assign-id 94 --flow --batch 10000 --hours 8 \
    --workers 8 --wait 30 --max-lag 20000 --max-pending 20000
```

**参数怎么定**:

| 参数 | 建议 | 理由 |
|---|---|---|
| `--batch` | 现场峰时量,或 10000 | 一组 = 3×batch |
| `--hours` | 8 | 长稳看的是"跑够时长" |
| `--wait` | batch 大时给 30 | 回包需要时间,太小会虚报丢包 |
| `--max-lag` / `--max-pending` | batch 的 1~2 倍 | **默认 0=不判**,不设等于白跑 |

> ⚠️ **`--hours` 的轮数不可预测**:时间到了只在"每组开始时"检查一次,
> 最后一组总会跑完,实际可能超时几分钟。要精确控制用 `--rounds`。
>
> 参考实测(137 平台 + Mock,`--wait 30`):**一组 batch=10000 约 47 秒**,
> 其中生成两张表固定要 8.5 秒、三段发送约 6 秒,其余是等回包。

---

## 4. 跑在远程 Linux(推荐做长稳)

**为什么要跑 Linux**:长稳跑几小时甚至过夜,Windows 会休眠/断网/锁屏;
而且现场环境就是 Linux。

### 4.1 一次性配置

右栏「**远程 Linux（稳定性测试跑在远端）**」:

| 字段 | 填什么 |
|---|---|
| ☑ 启用远程执行 | 勾上 |
| 主机 | `192.168.1.136`(SSH 目标机) |
| 端口 | `22` |
| 用户 / 密码 | `yangsh` / 你的密码 |
| 远端目录 | `/home/yangsh/so_test/strategy_soak` |

点 **「测试连接」**,应看到:

```
主机名   : localhost.localdomain
python3  : Python 3.9.25
openpyxl : 3.1.5
远程目录 : .../strategy_soak  不存在（首次运行会自动创建）
[SSH] 连接可用 ✓
```

> **远端目录必须用独立的**,别指向现场在用的目录 —— 上传会**覆盖同名文件**。
> 首次运行自动创建,不需要你手动 mkdir。

> **远端依赖**:只需 Python 3.8+ 和 `openpyxl`。
> **不需要装 `redis-py`**(本工具用纯 socket 的 `resp_min.py`)。

### 4.2 前台跑(适合短测,能看实时输出)

不勾「后台运行」,直接点「运行稳定性测试」。GUI 会:

```
[SSH] 已连接 yangsh@192.168.1.136:22
[SSH] 将同步 16 个文件（只传比远端新的）    ← 自动上传脚本+数据表
--- 第 1 组开始: create → modify → remove，各 10 条 ---
  ...实时回传远端输出...
[SSH] 已下载结果: out\soak\..._trend.csv    ← 跑完自动下载
```

**缺点**:SSH 一直连着,关掉 GUI 就断了。

### 4.3 后台跑 nohup(长稳推荐)

勾 ☑ **「后台运行(nohup):启动后立即返回,断开也不停」** → 点「运行稳定性测试」。

行为:

```
[NOHUP] 已后台启动，日志: out/soak/soak_flow_20260929_171708_nohup.log
```

* 命令以 `setsid + nohup` 提交,**实测 0.7 秒就返回**(不占 SSH)
* **关掉 GUI / 断网,远端照样继续跑**
* ⚠ **后台模式不会自动下载结果**(启动瞬间还没有结果)

**看进度**(GUI 日志框不给实时输出了,要自己 ssh 看):

```bash
ssh yangsh@192.168.1.136
cd /home/yangsh/so_test/strategy_soak
tail -f out/soak/soak_flow_*_nohup.log          # 终端输出
tail -f out/soak/soak_流create-modify-remove_*_soak.log   # 编排日志(每段一行指标)
```

**取结果**(两种都行):

* **GUI(推荐)**:点 **「下载远端结果」** → 存到本地 `out/soak/`
* 手动 `scp`:

```bash
scp yangsh@192.168.1.136:/home/yangsh/so_test/strategy_soak/out/soak/*_trend.csv .
scp yangsh@192.168.1.136:/home/yangsh/so_test/strategy_soak/out/soak/*_summary.json .
```

> ⚠️ **`scp` 会提示输密码**。在 GUI 里点按钮不会有这问题(用 paramiko 传的),
> 但在**没有交互终端的自动化脚本里直接调 `scp` 会挂住等输入**。
> 自动化场景请改用 `sshpass`、密钥免密,或直接用 GUI 的按钮。

**提前停止**:

* GUI:点 **「停止远端」**
* 手动:`ssh` 上去执行
  ```bash
  pkill -INT -f '[s]oak_test.py'
  ```
  ⚠ **用 `-INT` 别用 `-9`**:`-9` 会丢掉 `summary.json`。
  ⚠ 方括号 `[s]oak_test.py` 是**故意的**:直接写 `pgrep -f soak_test.py`
  会匹配到执行这条命令的 shell 自己,永远报"仍在运行"。

### 4.4 纯命令行版(不经过 GUI)

```bash
# 本机:打包上传
scp -r strategy_test yangsh@192.168.1.136:/home/yangsh/so_test/strategy_soak

# 远端
ssh yangsh@192.168.1.136
cd /home/yangsh/so_test/strategy_soak
mkdir -p out/soak
setsid nohup python3 soak_test.py --host 192.168.1.137 --db 0 \
    --assign-id 94 --flow --batch 10000 --hours 8 \
    --workers 8 --wait 30 --max-lag 20000 --max-pending 20000 \
    > out/soak/nohup.log 2>&1 < /dev/null &
```

> `< /dev/null` 和重定向不能省 —— 否则 SSH 通道会因为"还有进程持有 stdout"而不释放。

---

## 5. 看结果

输出在 `out/soak/soak_流create-modify-remove_<时间>_*`:

| 文件 | 内容 |
|---|---|
| `..._trend.csv` | **核心产物**。业务流下**每段一行**,多一个「阶段」列,可直接画图 |
| `..._summary.json` | 整体汇总(含实际生效的 `criteria`) |
| `..._soak.log` | 编排日志,每段一行关键指标 |
| `..._rounds/` | 异常组的明细 + 该组的 `refs.json` |

`trend.csv` 长这样:

```
轮次,时间,阶段,发送数,回包数,发送失败,超时未回,在途,回复率%,非本次回包,...
1,2026-09-29 17:03:03,create,10000,10000,0,0,0,100.0,0,...
1,2026-09-29 17:03:05,modify,10000,10000,0,0,0,100.0,0,...
1,2026-09-29 17:03:06,remove,10000,10000,0,0,0,100.0,0,...
```

**怎么看是否劣化**:按「时间」看 `回复率%` 是否下滑、`未ACK`/`lag` 是否单调上涨、
`延迟p99` 是否越来越大。涨 = 平台在恶化。

---

## 6. 出问题怎么排查

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `create 没有返回任何 Ref` | 平台回包不带 `Ref` | Mock 要勾「回包带回 Ref」;真平台不该有这问题 |
| `生成 modify 表: 失败` + `原文件没有更新` | `data/*.xlsx` 被 Excel/WPS 占着 | 关掉 Excel,重跑(工具会拒绝发旧表,不会静默出错) |
| `ref not exist` | 上一组 remove 没删掉,或跨天了 | 看上一组 remove 的回复率;跨天的表要重新生成 |
| `ref already inserted` | 上一组没删干净 | 同上;必要时换个 `--assign-id` 干净流重来 |
| 回复率 0%、`lag` 猛涨 | 平台没在读(消费者不在/编号错) | 用 `check_env.py` / `_ack_gap.py` 查 |
| 回复率 0%、`未ACK` 也涨 | 平台读了但卡住 | 查平台侧 |
| `[FAIL] --flow 与 --rotate 不能同用` | 传了冲突参数 | GUI 已自动过滤;命令行去掉 `--rotate` |
| 远端 `cd: 没有那个文件或目录` | 远端目录不存在且命令没走上传 | 用 GUI(会自动建目录+上传),或手动 mkdir |
| 「停止远端」总说"仍在运行" | `pgrep` 自匹配(已修) | 更新到最新代码 |

**排查工具**(都在 `tests/`,只读):

```bash
python tests/_ack_gap.py --stream ST-94        # 平台停在哪
python tests/_find_culprit.py --assign-id 94 --type destroy   # 逐条揪凶手
```

---

## 7. 收尾:别忘了清理

业务流会往目标流写**大量**数据(一组 3 万条)。测试完:

```bash
# 确认没有真实消费者、没有 pending，再删
XINFO CONSUMERS ST-94 user_group     # 看 consumer 名，ST-94-wN 才是本工具的
XPENDING ST-94 user_group
DEL ST-94
DEL ST-94-reply
```

> ⚠️ **真平台在用的流一个都不能碰**(`ST-0` / `ST-50`)。
> 所以业务流测试建议**挑一个高位干净编号**(如 `--assign-id 94`)。

远端也顺手清掉:停进程 + 删目录

```bash
pkill -INT -f '[s]oak_test.py'
rm -rf /home/yangsh/so_test/strategy_soak     # 确认不再需要时
```

---

## 8. 一页速查

```bash
# ---------- 本地试跑 ----------
python mock_strategy.py --host 192.168.1.137 --db 0 --assign-id 94 --ref-echo
python soak_test.py --assign-id 94 --flow --batch 10 --rounds 2 --wait 5

# ---------- 本地长稳 ----------
python soak_test.py --assign-id 94 --flow --batch 10000 --hours 8 \
    --workers 8 --wait 30 --max-lag 20000 --max-pending 20000

# ---------- 远端 nohup 长稳 ----------
mkdir -p out/soak
setsid nohup python3 soak_test.py --host 192.168.1.137 --db 0 \
    --assign-id 94 --flow --batch 10000 --hours 8 \
    --workers 8 --wait 30 --max-lag 20000 --max-pending 20000 \
    > out/soak/nohup.log 2>&1 < /dev/null &
tail -f out/soak/soak_*_soak.log
pkill -INT -f '[s]oak_test.py'        # 停（别用 -9）
```

**`--flow` 的三条硬约束**(违反会直接报错退出):

1. 只能 `--type normal`
2. 不能与 `--rotate` 同用
3. 只能 `--clean monitor`

GUI 里勾了业务流模式后这些会自动处理,不用记。
