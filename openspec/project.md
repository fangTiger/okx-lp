# 项目约定：okx-lp

## 目标
自动化参与 X Layer RWA 流动性激励活动（xStocks 相关池），在可控风险下最大化激励与手续费收益。

## 技术栈
- Python 3.11+，虚拟环境 `.venv`（运行任何命令前先激活）
- web3.py（X Layer 为标准 EVM，chainId 196）、httpx、pydantic、apscheduler、pandas
- 沿用同目录 `bn-monitor` 的工程约定：`log/` 输出、launchd 保活、`tests/` 放测试

## 硬性约定
- 代码注释与文档一律中文；标识符用英文
- 日志文件命名 `log/{功能名}_{日期}.log`
- 涉及真实资金的写链操作默认关闭，需显式开启并通过风控闸门
- 私钥不落明文；`config/secrets.env` 不入版本库
- 单文件超过 200 行时分段写入

## 事实来源
- 活动规则事实：`docs/plans/2026-08-25-campaign-facts.md`
- 策略与收益模型：`docs/plans/2026-08-25-strategy.md`
- 技术设计：`openspec/changes/add-xlayer-lp-automation/design.md`
