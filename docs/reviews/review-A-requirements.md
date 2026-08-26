# 需求符合性审核报告（审核员 A）

## 结论

总体：**FAIL**（D1、D4、D5、D8、D10、D11 为 FAIL）

审核基线为当前工作树中的唯一权威 `docs/plans/2026-08-25-requirements-locked.md`。全量单元测试实际通过 115 项，但测试通过不能覆盖需求缺口：D1 存在精确池价反例；D4 在撤出窗口开始后先睡 60 秒；D5/D8 缺少生产动作与全自动接线；D10/D11 尚未实现。

关键依据：`docs/plans/2026-08-25-requirements-locked.md:6-23,25-64,66-103,125-165`、`openspec/changes/add-xlayer-lp-automation/tasks.md:7-79`、相关源码与测试。项目没有 `graphify-out/graph.json` 或 `graphify-out/GRAPH_REPORT.md`，故按规则降级为源码、测试、日志和实际运行输出审核。

## 逐条核验

| 编号 | 需求 | 判定 | 证据 |
|---|---|---|---|
| D1 | 固定 ±0.5%，以重组时池价为中心，tick 对齐向外取整 | **FAIL** | 固定宽度与 floor/ceiling 存在：`src/okxlp/strategy/machine_types.py:12,48-57`、`src/okxlp/uniswap/tickmath.py:57-73`。但状态机只把整数 `sample.tick` 传入区间构造，忽略同一样本的精确 `sample.price`：`src/okxlp/strategy/machine.py:88,139-140`、`src/okxlp/strategy/machine_stages.py:25`。独立反例：精确价 `1.00009999`、当前 tick `0`、spacing `10` 时，按精确池价外扩应为 `(-50,60)`，代码给 `(-60,50)`，代码上沿仅 `+0.491178849%`，属于向内；见运行记录 3。锁定快照恰好得到正确 `(-201580,-201470)`，不能排除该反例。 |
| D2 | 跌破下沿、满仓币时卖一半，按现价 50/50 重组 | **PASS** | `calculate_50_50_swap` 按两腿现值差额统一求兑换方向与数量：`src/okxlp/strategy/allocation.py:59-85`；编排在 collect 后只调用该函数一次：`src/okxlp/strategy/rebalance.py:109-122`。独立输入 `2 wASMLx + 0 USDC`、价格 2000，手算应卖 `1 wASMLx`，代码完全一致；见运行记录 2。生产接线缺失归入 D5/D8。 |
| D3 | 涨破上沿、满仓 U 时立刻买回一半，按现价 50/50 重组；与 D2 同代码 | **PASS** | 同一 `calculate_50_50_swap` 无方向参数，只按 `delta` 正负在函数内部选 token_in/out：`src/okxlp/strategy/allocation.py:70-85`；`MachineStages._rebalance_stage` 不按出界方向分支：`src/okxlp/strategy/machine_stages.py:23-31`。独立输入 `0 wASMLx + 4000 USDC`，手算应使用 `2000 USDC` 买币，代码一致；已平衡与偏斜输入也一致。 |
| D4 | 标的交易时段撤出，不做市 | **FAIL** | 任一上市地开市会返回不做市：`src/okxlp/market/sessions.py:66-71`；但持仓状态首次只转为 `EXITING`：`src/okxlp/strategy/machine.py:70-86`，该轮不调用退出。循环随后因 `should_make_market=False` 睡 60 秒：`src/okxlp/strategy/machine_loop.py:56-60`，下一轮才执行 `actions.exit`：`src/okxlp/strategy/machine_stages.py:36-42`。内存实测第一轮 `state=EXITING; exit_calls=0; sleeps=[60]`，第二轮才 `exit_calls=1`；见运行记录 5。 |
| D5 | 撤出时全部清成 U，零敞口 | **FAIL** | 正式源码只有 `MachineActions.exit` 协议声明：`src/okxlp/strategy/machine_types.py:74-86`；状态机只委托该动作：`src/okxlp/strategy/machine_stages.py:36-42`。仓库唯一具体主状态机动作类是 `NoBroadcastActions`，只打印并拒绝广播：`tools/dry_run_m7.py:58-87`。定向搜索未找到生产 enter/rebalance/exit 适配器，无法证明真实执行 burn→collect→全量换 U 或事后零余额。 |
| D6 | 不做 Delta 对冲，不开发对冲模块 | **PASS** | `config/risk.yaml:39-43` 明确 `hedge.enabled: false`；定向搜索 `rg -n -i 'hedge' src tools` 无源码命中。 |
| D7 | 出界判定仅使用链上池价和连续时间，不使用外部行情参考价 | **PASS** | `OutrangeDetector.evaluate` 只接收池价、上下沿和观测时间：`src/okxlp/strategy/outrange.py:85-126`；180 秒确认、600 秒上限保护及回归重置分别见 `src/okxlp/strategy/outrange.py:99-137`，重启恢复首次时间与方向见 `src/okxlp/strategy/outrange.py:67-77`、`src/okxlp/strategy/machine.py:174-180`。实际接线的市场样本来自 `UniswapV3Pool.snapshot()`：`tools/dry_run_m7.py:103-105`，未使用外部行情。 |
| D8 | 全自动无人值守 + 熔断 + Telegram 告警 | **FAIL** | 唯一主状态机运行接线是永久拒绝广播的 dry-run：`tools/dry_run_m7.py:58-87,111-128`；默认告警仅写 logger：`src/okxlp/strategy/machine.py:40-52`。现有 `DryRunRiskGate` 只检查 HALT 与事实闸门：`tools/dry_run_m7.py:37-55`，未消费 `daily_loss_pct` 和 `max_rebalances_per_day`。定向搜索 Telegram、回撤熔断和每日次数控制在 `src tools tests` 中无实现命中；M9/M10 仍未完成：`openspec/changes/add-xlayer-lp-automation/tasks.md:64-75`。 |
| D9 | 单笔 swap ≥ $500 才拆，否则直接成交 | **PASS** | `src/okxlp/uniswap/swap.py:152-179` 明确 `usd < threshold` 为 1 笔，否则随机 3–5 笔，因此恰好 500 会拆；配置为 500、3–5 笔、20–30 秒：`config/risk.yaml:17-23`；边界测试见 `tests/test_swap.py:104-115,128-141`。 |
| D10 | 自有资金、不做份额账本；NAV 快照从第一天存 | **FAIL** | 未发现份额账本，符合前半项；但定向搜索 `NAV/nav` 在 `src tools tests` 中无实现命中，任务表反而把 NAV 快照列为后置：`openspec/changes/add-xlayer-lp-automation/tasks.md:77-79`，与权威 D10 的“从第一天存”不符。现有 observer/position 日志不是包含资产、负债、闲置资金和已实现损益的 NAV 快照。 |
| D11 | Mac 本机 + launchd，且启动/唤醒先对账、心跳、失联告警、睡眠容忍 | **FAIL** | M8.1–M8.4 全未完成：`openspec/changes/add-xlayer-lp-automation/tasks.md:57-62`。定向搜索未发现 heartbeat、launchd plist、caffeinate、pmset、唤醒头寸 reconciliation；可运行入口也只有 verifier、observer 和 dry-run（运行记录 6）。现有 Intent 回执恢复不是读取 NPM 实际头寸后再放行决策。 |
| D12 | 首笔测试仓 < $50 | **PASS** | `RESUME.md:70-74` 记录用于校准的人工链上自持 tokenId 15857，价值约 `$39.56`；首条头寸日志为 `log/position_15857_2026-08-25.jsonl:1`。按该条 price、tickLower/tickUpper、liquidity 用 Uniswap v3 公式独立估值 `$39.5639786605 < $50`；见运行记录 7。该证据只证明测试仓规模，不证明自动化执行。 |
| D13 | MVP 先上，边跑边补 | **PASS** | README 记录 M1–M7 已实现：`README.md:18-23`；任务表 M1–M7 已勾选而 M8–M10 待补：`openspec/changes/add-xlayer-lp-automation/tasks.md:7-75`；实际 observer 日志共有 576 条（`log/observer_2026-08-25.jsonl` 431 条、`log/observer_2026-08-26.jsonl` 145 条），构成先运行只读 MVP、继续补后续模块的证据。此过程性 PASS 不改变 D8/D11 功能未完成。 |
| D14 | 撤出窗口为该标的所有上市地交易时段的并集 | **PASS** | 池配置声明 Amsterdam 与 NASDAQ 两地：`config/pools.yaml:29-36`；调度器逐个 listing 用各自时区换算，只要任一开市就返回撤出：`src/okxlp/market/sessions.py:66-71`。独立验证在 2026-10-28 错位周覆盖并集开始、Amsterdam 收盘但 NASDAQ 仍开、NASDAQ 收盘，全部与期望一致；见运行记录 4。D4 的动作延迟不影响 D14 的窗口计算判定。 |
| T1 | 夏令时使用 `zoneinfo`，Amsterdam/New York 各自换算 | **PASS** | `src/okxlp/market/sessions.py:9,66-70,87-96` 使用 `ZoneInfo`；时区名来自 `config/pools.yaml:31-36,45-50`。2026-10-28 实测 Amsterdam 为 `+01:00`、New York 为 `-04:00`，边界全部匹配。 |
| T2 | 事件文件读取失败必须 fail-safe 撤出 | **PASS** | `_load_events` 对文件、YAML、字段错误返回错误状态：`src/okxlp/market/sessions.py:127-138`；`should_make_market` 遇错误立即返回 False：`src/okxlp/market/sessions.py:55-60`。不存在事件文件实测 `expected=False actual=False`。实际退出仍受 D4 的一轮延迟影响。 |
| T3 | 外汇周日 17:00 ET 前后各 30 分钟保护窗口 | **PASS** | 配置见 `config/pools.yaml:45-50`；实现用 `America/New_York` 的周日当地时间并包含两端点：`src/okxlp/market/sessions.py:87-97`。实测 16:29 ET 做市、16:30–17:30 撤出、17:31 恢复做市，全部匹配。 |

## 发现的问题

### 1. D1 区间以整数 tick 而非重组时精确池价为中心

- 问题：`MarketSample` 同时有精确 price 和 tick，但 `_target_band` 只使用 tick；整数 tick 是精确价格所在格子的离散值，不等于精确池价。
- 影响：某些格内价格会使一侧边界窄于 0.5%，提前出界。本次反例的上沿只有 `+0.491178849%`。
- 复现方式：输入 `exact_price=1.00009999,current_tick=0,tickSpacing=10`；独立外扩为 `(-50,60)`，代码为 `(-60,50)`。
- 建议修法：区间构造接收精确 pool price，分别对 `price×0.995` 与 `price×1.005` 求目标 tick，再对下沿 floor、上沿 ceiling；把本反例加入测试。

### 2. D4 撤出窗口开始后多等待一个 60 秒循环

- 问题：第一轮只转移到 `EXITING`，主循环根据本轮 `should_make_market=False` 立即睡 60 秒，第二轮才执行退出动作。
- 影响：上市地开盘、事件 fail-safe 或 FX 保护窗口开始后，LP 最长仍可能留在池内约 60 秒（另加边界检测延迟），违反“只在静默期做市”。
- 复现方式：从 `IN_RANGE` 喂入 `should_make_market=False`，运行一轮；输出 `state=EXITING; exit_calls=0; sleeps=[60]`。
- 建议修法：进入 `EXITING` 后同轮执行防御性退出，或至少在尚未退出时保持快速循环；补“窗口边界首次 step 已触发 exit”的回归测试。

### 3. D5 缺少生产版退出动作和零敞口校验

- 问题：正式代码只有协议，唯一具体动作类是拒绝广播的预览桩。
- 影响：无法证明真实 burn、collect、全量 token0→USDC swap，也无法证明撤出后 wASMLx 余额为零。
- 复现方式：`rg -n "class .*Actions|def enter\\(|def rebalance_actions\\(|def exit\\(" src tools` 只找到 Protocol、`RebalanceActions` 数据容器与 `NoBroadcastActions`。
- 建议修法：实现生产 `MachineActions` 适配器，collect 后读取真实余额，全量换 U，并在交易确认后重新读取钱包/NPM 证明零敞口。

### 4. D8 仍是 dry-run 骨架，熔断和 Telegram 未闭环

- 问题：唯一主状态机接线永久拒绝广播；仅有 HALT/事实检查，未实现 3% 日回撤、每日再平衡上限和 Telegram 发送。
- 影响：无法全自动无人值守运行；关键资金风险与故障不会按需求熔断和告警。
- 复现方式：运行记录 6 的入口/关键词搜索；并检查 `tools/dry_run_m7.py:58-87,111-128`。
- 建议修法：增加生产入口，接入真实 actions、完整风险闸门和 Telegram adapter；每个写链 Intent 前重新检查 HALT/回撤/频次，并用受控小额完整周期验收。

### 5. D10 缺少从第一天开始的 NAV 快照

- 问题：仓库没有 NAV 模块或 NAV 日志，任务表将其后置。
- 影响：无法重建每日净值和 3% 回撤，D8 的净值熔断也缺少数据基础。
- 复现方式：`rg -n -i "nav" src tools tests` 无输出；`openspec/changes/add-xlayer-lp-automation/tasks.md:77-79` 明确后置。
- 建议修法：立即补最小 NAV 快照（LP 估值、闲置两腿、已实现损益、时间和区块）；仍不需要开发合伙份额账本。

### 6. D11 睡眠容忍与 Mac 部署未实现

- 问题：没有启动/唤醒 NPM 头寸对账、heartbeat、失联 Telegram 告警及 launchd/caffeinate/pmset 文件。
- 影响：Mac 睡眠或重启后可能依据过期本地状态决策，也不能报告失联时长。
- 复现方式：M8.1–M8.4 均未勾选；运行记录 6 的定向搜索无实现结果。
- 建议修法：把链上头寸 reconciliation 作为任何决策前的硬闸门；实现 heartbeat/失联告警和 launchd 部署脚本，并执行真实睡眠唤醒验收。

## 实际运行记录

以下记录结论性验证命令与真实输出；逐文件 `nl -ba` 阅读输出不重复粘贴，行号证据已列在表中。为避免审核过程生成 `.pyc`，Python 命令设置了 `PYTHONDONTWRITEBYTECODE=1`。

### 1. 全量单元测试

命令：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

退出码 `0`，真实输出：

```text
.................Intent 39f2c864b7e0449b8ff583889ed4dbf2 模拟回滚，中止执行：execution reverted: 价格保护
..................................................................................................
----------------------------------------------------------------------
Ran 115 tests in 4.788s

OK
```

### 2. 50/50 四组独立验算

命令外壳：

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -c '<四组 BalanceSnapshot 与独立期望计算脚本>'
```

输入统一采用 `price=2000 USDC/wASMLx`。手算：2 币值 4000，卖 1 币；4000 U 用 2000 U 买币；1 币+2000 U 已平衡；1.5 币+1000 U 卖 0.5 币。真实输出：

```text
独立期望：价格=2000 USDC/wASMLx
满仓币：2币=$4000，目标每腿$2000，卖1币
满仓U：4000U=$4000，目标每腿$2000，用2000U买币
已平衡：1币=$2000 + 2000U，无需swap
偏斜：1.5币=$3000 + 1000U，目标每腿$2000，卖0.5币
满仓 wASMLx: expected=('wASMLx', 'USDC', 1000000000000000000, Decimal('2000')); actual=('wASMLx', 'USDC', 1000000000000000000, Decimal('2000')); match=True
满仓 USDC: expected=('USDC', 'wASMLx', 2000000000, Decimal('2000')); actual=('USDC', 'wASMLx', 2000000000, Decimal('2000')); match=True
已平衡: expected=None; actual=None; match=True
偏斜: expected=('wASMLx', 'USDC', 500000000000000000, Decimal('1000')); actual=('wASMLx', 'USDC', 500000000000000000, Decimal('1000.0')); match=True
```

首次执行同类脚本时未设置 `PYTHONPATH=src`，三个脚本均真实退出 `1`：

```text
Traceback (most recent call last):
  File "<string>", line 3, in <module>
ModuleNotFoundError: No module named 'okxlp'
```

补上 `PYTHONPATH=src` 后得到以上及下述成功输出；失败没有被隐去。

### 3. ±0.5% tick 区间独立验算

命令外壳：

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -c '<用 ln(price×0.995)/ln(1.0001) 与 ln(price×1.005)/ln(1.0001) 独立求 tick，并比较 aligned_tick_range>'
```

退出码 `0`，真实输出：

```text
锁定快照
  exact_price=1770.77; current_tick=-201526
  raw_target_ticks=(-201578.71724931364491503036577905467882704360349481588032559940196925074564563683, -201478.71141600947392815125632690181415273346771151992099562931583756363175315543)
  independent_expected=(-201580, -201470); code=(-201580, -201470); match=True
  code_bounds_vs_exact_center=(-0.512761913%, 0.587583495%)
同一tick内接近上界
  exact_price=1.00009999; current_tick=0
  raw_target_ticks=(-49.128024459586415690100118582288498192715452707348286197867909834432570331907136, 50.877808844584571189009333570576176117420330588611043772218221852681322149500729)
  independent_expected=(-50, 60); code=(-60, 50); match=False
  code_bounds_vs_exact_center=(-0.608111971%, 0.491178849%)
```

### 4. 时段边界、周末、DST 错位周、FX 与事件失败

命令外壳：

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -c '<MarketSessions 边界矩阵脚本>'
```

退出码 `0`。以下为真实输出（`actual=True` 表示做市，`False` 表示撤出）：

```text
2026-10-28：欧洲已回标准时、美国仍为夏令时
Amsterdam开盘前1秒: UTC=2026-10-28T07:59:59+00:00 AMS=2026-10-28T08:59:59+01:00 NY=2026-10-28T03:59:59-04:00 expected=True actual=True match=True
Amsterdam开盘: UTC=2026-10-28T08:00:00+00:00 AMS=2026-10-28T09:00:00+01:00 NY=2026-10-28T04:00:00-04:00 expected=False actual=False match=True
Amsterdam收盘前1秒: UTC=2026-10-28T16:39:59+00:00 AMS=2026-10-28T17:39:59+01:00 NY=2026-10-28T12:39:59-04:00 expected=False actual=False match=True
Amsterdam收盘: UTC=2026-10-28T16:40:00+00:00 AMS=2026-10-28T17:40:00+01:00 NY=2026-10-28T12:40:00-04:00 expected=True actual=True match=True
NASDAQ收盘前1秒: UTC=2026-10-28T19:59:59+00:00 AMS=2026-10-28T20:59:59+01:00 NY=2026-10-28T15:59:59-04:00 expected=False actual=False match=True
NASDAQ收盘: UTC=2026-10-28T20:00:00+00:00 AMS=2026-10-28T21:00:00+01:00 NY=2026-10-28T16:00:00-04:00 expected=True actual=True match=True
并集开始前1秒: UTC=2026-10-28T07:59:59+00:00 expected=True actual=True match=True
并集开始: UTC=2026-10-28T08:00:00+00:00 expected=False actual=False match=True
Amsterdam已收、NASDAQ仍开: UTC=2026-10-28T16:40:00+00:00 expected=False actual=False match=True
并集结束: UTC=2026-10-28T20:00:00+00:00 expected=True actual=True match=True
周六: UTC=2026-10-31T12:00:00+00:00 expected=True actual=True match=True
外汇周日17:00 ET ±30分钟（EDT）
窗口前1分钟: UTC=2026-08-30T20:29:00+00:00 NY=2026-08-30T16:29:00-04:00 expected=True actual=True match=True
窗口起点: UTC=2026-08-30T20:30:00+00:00 NY=2026-08-30T16:30:00-04:00 expected=False actual=False match=True
窗口终点: UTC=2026-08-30T21:30:00+00:00 NY=2026-08-30T17:30:00-04:00 expected=False actual=False match=True
窗口后1分钟: UTC=2026-08-30T21:31:00+00:00 NY=2026-08-30T17:31:00-04:00 expected=True actual=True match=True
事件文件缺失fail-safe: UTC=2026-10-31T12:00:00+00:00 expected=False actual=False match=True reason=事件文件不可用（无法读取事件文件），按有事件处理并撤出
```

说明：Amsterdam 开/收四项使用仅保留 Amsterdam listing 的调度器，NASDAQ 收盘两项使用仅保留 NASDAQ listing 的调度器；“并集”各项使用完整两地配置，因此 Amsterdam 收盘时完整调度器仍因 NASDAQ 开市而撤出。

### 5. D4 撤出延迟复现

命令外壳：

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -c '<内存 Store/Journal/Actions，从 IN_RANGE 喂入 should_make_market=False，运行两轮>'
```

退出码 `0`，真实输出：

```text
第一轮后: state=EXITING; exit_calls=0; sleeps=[60]
第二轮后: state=IDLE; exit_calls=1; reason=撤出完成：burn → collect → 全部换成 USDC
```

### 6. 生产接线、监控和部署产物搜索

命令：

```bash
rg -n "if __name__ == .__main__.|argparse|MainStateMachine\\(|TransactionExecutor\\(|PositionManager\\(|SwapRouter\\(" src tools
rg -n -i "telegram|heartbeat|launchd|caffeinate|pmset|daily_loss|max_rebalances|nav" src tools tests
rg -n "class .*Actions|def enter\\(|def rebalance_actions\\(|def exit\\(" src tools
```

真实结果：第一条仅找到 verifier、observer、若干只读工具与 `tools/dry_run_m7.py` 的 `MainStateMachine` 接线；第二条退出码 `1`、无输出；第三条输出仅包含 `MachineActions(Protocol)`、`RebalanceActions` 数据容器和 `NoBroadcastActions`。部署产物搜索 `rg --files -g '*.plist' -g '*launch*' -g '*deploy*' -g '*heartbeat*' -g '*telegram*' -g '*nav*' .` 退出码 `1`、无输出。

### 7. < $50 测试仓独立估值

命令外壳：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -c '<读取 position_15857 首条日志，按 Uniswap v3 amount0/amount1 公式估值>'
```

退出码 `0`，真实输出：

```text
记录={'ts': '2026-08-25T12:42:41Z', 'block': 68892725, 'token_id': 15857, 'price': 1771.4431701141646, 'tick': -201525, 'tick_lower': -201970, 'tick_upper': -201070, 'in_range': True, 'my_liquidity': 21126254269852, 'active_liquidity': 19021251539791270, 'share': 0.0011106658374007198, 'tokens_owed0': 0.0, 'tokens_owed1': 0.0, 'fee_growth_inside0': 0, 'fee_growth_inside1': 0}
独立Uniswap-v3估值: amount_wASMLx=0.011284654228689677236790063319126705439884116384057205085365910511701111206283847; amount_USDC=19.573854999999593679270897660693727277607198498490185246916121340397522700559536; value_USDC=39.563978660511848506043793577095502856494534734375612278016167662870426255545671; less_than_50=True
```
