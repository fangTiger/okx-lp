# M6 Position Operations Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 实现事实闸门分级、Uniswap V3 头寸/兑换 Intent 构造与安全可恢复的固定顺序再平衡。

**Architecture:** 纯构造层只编码 ABI 并返回 M5 `Intent`；报价层通过现有只读 RPC 调用 QuoterV2；编排层只按固定阶段调用 M5 执行器并原子记录进度。所有地址来自 YAML，所有金额计算使用 `Decimal`。

**Tech Stack:** Python 3.11、`dataclasses`、`Decimal`、`eth_abi`、`eth_utils`、PyYAML、`unittest`。

---

### Task 1: 事实闸门分级

**Files:**
- Modify: `config/facts.yaml`
- Modify: `src/okxlp/campaign/gate.py`
- Modify: `tests/test_campaign_gate.py`

**Step 1: Write the failing test**

新增当前事实清单允许 `ensure_write_allowed()`，并使 `max_position_usd(10000, 2000)`
返回 `Decimal("2000")` 的测试；另测 `blocks: live` 仍拒绝写链。

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_campaign_gate -v`
Expected: FAIL，旧闸门仍因 F2/F3 拒绝写链且没有仓位上限接口。

**Step 3: Write minimal implementation**

为 `Fact` 增加 `blocks`，提供 `live_blockers`、`size_blockers`、
`max_position_usd`；只让 `live` 触发 `forced_dry_run` 与写链拒绝。

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_campaign_gate -v`
Expected: PASS。

### Task 2: NPM Intent 构造

**Files:**
- Create: `src/okxlp/uniswap/position.py`
- Create: `tests/test_position.py`
- Modify: `config/execution.yaml`

**Step 1: Write the failing test**

对 mint calldata 做 ABI 解码，断言 `aligned_tick_range(-201526, 0.005, 10)` 的
外扩 tick；分别断言 increase、decrease、collect、burn 的选择器与目标地址。

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_position -v`
Expected: ERROR，模块尚不存在。

**Step 3: Write minimal implementation**

实现 `PositionManager` 五个纯构造方法，使用官方 tuple ABI 和 `Intent.create`。

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_position -v`
Expected: PASS。

### Task 3: 报价、滑点与拆单

**Files:**
- Create: `src/okxlp/uniswap/swap.py`
- Create: `tests/test_swap.py`
- Modify: `config/risk.yaml`
- Modify: `config/risk.example.yaml`

**Step 1: Write the failing test**

使用返回固定 ABI 数据的 RPC，断言 QuoterV2 calldata、30 bps 最小到账量、31 bps
拒绝、500 USD 边界拆为 3–5 笔、总量守恒与 20–30 秒间隔。

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_swap -v`
Expected: ERROR，模块尚不存在。

**Step 3: Write minimal implementation**

实现 `SwapPolicy`、`SwapQuote`、`ScheduledSwap` 与 `SwapRouter.plan_exact_input_single`。

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_swap -v`
Expected: PASS。

### Task 4: 固定顺序再平衡与进度记录

**Files:**
- Create: `src/okxlp/strategy/rebalance.py`
- Create: `tests/test_rebalance.py`

**Step 1: Write the failing test**

新增两腿余额到 50/50 差额测试；记录执行器事件并断言顺序严格为
`burn, collect, swap, mint`；在 swap 注入失败并断言 mint 未执行、日志只记录前两步。

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_rebalance -v`
Expected: ERROR，模块尚不存在。

**Step 3: Write minimal implementation**

实现差额计算、四阶段计划、终态校验以及原子 JSON 进度记录。

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_rebalance -v`
Expected: PASS。

### Task 5: 配置白名单与全量验证

**Files:**
- Modify: `config/execution.yaml`
- Create: `tools/simulate_m6.py`

**Step 1: Write the failing test**

扩展配置/白名单测试，断言四个官方合约地址和 NPM/Router 全部写方法选择器。

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_whitelist tests.test_config -v`
Expected: FAIL，配置尚未包含新地址和选择器。

**Step 3: Write minimal implementation**

更新配置并实现只读验收脚本：真实报价后，用 dry-run 执行器对四阶段逐项
`eth_call`，打印完整交易与结果，脚本不提供广播参数。

**Step 4: Run all verification**

Run: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py'`
Expected: 全部 PASS。

Run: `PYTHONPATH=src .venv/bin/python tools/simulate_m6.py`
Expected: 打印 20 USDC 报价、30 bps 最小到账量及四阶段 dry-run 模拟结果；广播数为零。

当前目录不是 Git 仓库，因此本计划不包含 commit 步骤；所有验证直接针对用户指定目录。
