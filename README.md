# okx-lp — X Layer xStocks LP 激励自动化

自动化参与 X Layer RWA 流动性激励（Round 1，2026-08）。

## 先读这三份

1. `docs/plans/2026-08-25-campaign-facts.md` — 活动事实基线与**待核实清单 F1–F10**
2. `docs/plans/2026-08-25-strategy.md` — 策略推导与收益模型
3. `openspec/changes/add-xlayer-lp-automation/design.md` — 技术设计

## 一句话策略

活动奖励按「LP 在池内产生的手续费占比、按小时」分配，
所以收益由 **in-range 流动性份额 ≈ 本金 / 区间宽度** 决定，
系统的工作是**在不出界的前提下把区间压到最窄**，
并在美股开盘与事件窗口前主动收敛风险。

## 状态

M1–M7 已实现：只读观测、配置与事实闸门、时段状态机、参考价与出界判定、
默认不广播的执行层、头寸/兑换 Intent 与固定顺序再平衡，以及可恢复的主状态机。
池参数以 `docs/plans/2026-08-25-requirements-locked.md` 和
`config/pools.yaml` 为准。

## 运行启动闸门

```bash
PYTHONPATH=src .venv/bin/python -m okxlp.campaign.verifier
```

该命令只读校验 chainId、池参数、代币合约代码和 decimals。事实清单中未核实项的
`blocks: live` 会强制 dry-run 并拒绝写链；`blocks: size` 允许写链，但单池仓位由
`max_position_usd` 压到 `probe_capital_usd`；`verified: n/a` 不参与判断。所有写链
入口都必须先调用事实闸门。

`config/events.yaml` 手工维护财报事件。缺失、YAML 解析失败或字段错误都会按
“有事件”处理并撤出。`okxlp.market.sessions.should_make_market(now)` 返回
`(是否做市, 中文原因)`。

## 运行只读观测器

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python -m okxlp.observer --pool-id wASMLx_USDC
```

程序每 30 秒追加一行到 `log/observer_YYYY-MM-DD.jsonl`，每 5 分钟在控制台
打印一次摘要。`share_at` 的值为 0–1 之间的 in-range 份额比例；SIGINT 或
SIGTERM 会触发优雅退出。网络调用失败只告警，下一轮继续重试。

运行测试：

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```
