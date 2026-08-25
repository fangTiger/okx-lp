# M7 Main State Machine Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 实现可注入、可持久化、默认不广播的六状态主状态机，并修复 50/50 粉尘兑换缺口。

**Architecture:** 状态数据与原子存储拆到 `machine_state.py`，`machine.py` 保留检查顺序和转移逻辑。建仓与撤出由可注入动作协议承接，再平衡直接调用 M6 编排器；M4 负责纯链上持续出界计时、回归重置和超时保护。

**Tech Stack:** Python 3.11、`dataclasses`、`Enum`、`Decimal`、JSONL、PyYAML、`unittest`。

---

### Task 1: 粉尘兑换阈值

**Files:**
- Modify: `tests/test_rebalance.py`
- Modify: `tests/test_config.py`
- Create: `src/okxlp/strategy/allocation.py`
- Modify: `src/okxlp/strategy/rebalance.py`
- Modify: `config/risk.yaml`
- Modify: `config/risk.example.yaml`

1. 添加 20.00/20.00 两腿仅有 0.0008 USD 差额时返回 `None` 的测试，并断言实际配置为 1 USD。
2. 运行 `PYTHONPATH=src .venv/bin/python -m unittest tests.test_rebalance tests.test_config -v`，确认因缺少阈值失败。
3. 把余额类型、差额计算和阈值加载移入 `allocation.py`，由 M6 编排器使用配置值。
4. 重跑定向测试，确认边界与既有双向兑换测试通过。

### Task 2: 持久化状态与结构化日志

**Files:**
- Create: `tests/test_machine_state.py`
- Create: `src/okxlp/strategy/machine_state.py`
- Create: `src/okxlp/strategy/machine_journal.py`

1. 测试首次加载为 `IDLE`、原子保存后可读回、非法状态拒绝加载，以及转移日志字段完整。
2. 运行测试确认模块缺失的 RED。
3. 实现六状态枚举、价格区间、状态快照、原子 JSON 存储与逐行 JSON 转移日志。
4. 重跑测试确认 GREEN。

### Task 3: 主状态机生命周期

**Files:**
- Create: `tests/test_machine.py`
- Create: `tests/test_machine_safety.py`
- Create: `tests/test_machine_recovery.py`
- Create: `src/okxlp/strategy/machine.py`
- Create: `src/okxlp/strategy/machine_loop.py`
- Create: `src/okxlp/strategy/machine_stages.py`
- Create: `src/okxlp/strategy/machine_types.py`

1. 用假时钟、假时段/风控、假池价和假动作回放完整生命周期，断言所有转移及原因。
2. 补非做市不入场、持续出界确认、检查顺序、广播默认关闭、失败停留并告警测试。
3. 运行 `PYTHONPATH=src .venv/bin/python -m unittest tests.test_machine -v`，确认 RED。
4. 实现固定 ±0.5% 区间、状态优先级、M4/M6 组合、5/60 秒循环与安全失败处理。
5. 重跑状态机定向测试，确认 GREEN。

### Task 4: 真实链 dry-run 与最终验证

**Files:**
- Create: `tools/dry_run_m7.py`
- Modify: `openspec/changes/add-xlayer-lp-automation/tasks.md`

1. 实现只读脚本：读取真实 X Layer 池快照、时段、事实闸门与 HALT 状态，打印状态机当前决策和理由；动作仅打印且永不提供广播入口。
2. 运行 `PYTHONPATH=src .venv/bin/python tools/dry_run_m7.py`，保存实际输出。
3. 运行 `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`，确认全量通过。
4. 检查所有新增/修改 Python 文件不超过 200 行，再把 M7 任务标记完成。

当前目录没有可用 Git 元数据，因此计划不包含 worktree 或 commit 步骤；用户已明确要求在当前目录直接完成。
