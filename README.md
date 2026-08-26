# okx-lp — X Layer xStocks LP 激励自动化

在 X Layer 的 Uniswap V3 池上做集中流动性做市，参与 X Layer RWA 流动性激励活动。

## 换机器 / 新接手，从这里开始

**→ [`HANDOFF.md`](HANDOFF.md)** — 环境搭建、当前进度、下一步修复清单、踩坑记录

## 一句话策略

活动奖励按「LP 在池内产生的手续费占比、按小时」分配，
所以收益由 **in-range 流动性份额 ≈ 本金 / 区间宽度** 决定。
系统固定 ±0.5% 区间、出界即按现价 50/50 重组，
且只在标的所有上市地都收盘的**静默期**做市。

## 文档索引

| 文件 | 内容 |
|---|---|
| [`HANDOFF.md`](HANDOFF.md) | 交接文档，换机后的入口 |
| [`docs/plans/2026-08-25-requirements-locked.md`](docs/plans/2026-08-25-requirements-locked.md) | **需求定稿 v2，唯一权威** |
| [`docs/plans/2026-08-26-decision-log.md`](docs/plans/2026-08-26-decision-log.md) | 决策演进过程与理由，含被修正的错误判断 |
| [`docs/plans/2026-08-25-campaign-facts.md`](docs/plans/2026-08-25-campaign-facts.md) | 活动事实基线与未核实清单 F1–F10 |
| [`docs/plans/2026-08-25-strategy.md`](docs/plans/2026-08-25-strategy.md) | 策略推导与收益模型 |
| [`docs/reviews/`](docs/reviews/) | 两份独立审核报告（当前均判 FAIL，修复清单见 HANDOFF §4） |

## 状态

MVP **27/38** 项，M1–M7 已交付，M8–M10 未开始。测试 115 项全绿。

**当前不能上实盘**：缺生产动作适配器，且两份独立审核发现多项安全缺陷待修复。
所有写链操作默认关闭，需显式授权才会发出交易。

## 快速验证

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'   # 115 OK
PYTHONPATH=src .venv/bin/python -m okxlp.campaign.verifier      # 链上校验
PYTHONPATH=src .venv/bin/python tools/dry_run_m7.py             # 当前决策（只读）
```
