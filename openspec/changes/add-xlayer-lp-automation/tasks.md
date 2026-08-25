# MVP 任务拆分（交付 codex 逐条执行）

规格来源：`docs/plans/2026-08-25-requirements-locked.md`（v2）
每条任务自包含、可独立验收。约定：Python 3.11、`.venv`、中文注释与日志、
日志落 `log/{功能名}_{日期}.log`、单文件超 200 行分段写。

## M1 只读观测器（最优先，无风险，立即开跑）
- [x] 1.1 `chain/rpc.py`：X Layer JSON-RPC 客户端，多节点故障转移、重试、超时
- [x] 1.2 `uniswap/tickmath.py`：价格↔tick 换算、tickSpacing 对齐（向外取整）、
      流动性↔金额换算；**含单元测试**，用已知快照校验（tick=-201526 ↔ 1770.77 USDC）
- [x] 1.3 `uniswap/pool.py`：读 slot0 / liquidity / token 元数据 / 池内余额
- [x] 1.4 `observer.py`：30s 轮询，落 `log/observer_{日期}.jsonl`，
      字段含 block、price、tick、active_liquidity、±0.5% 区间、各档本金对应份额
- [x] 1.5 优雅退出（SIGINT/SIGTERM）、网络失败不退出只告警
- **验收**：连续跑 10 分钟无异常，jsonl 每 30s 一条，字段与 `tools/probe_pool.py` 输出一致

## M2 配置与事实闸门
- [x] 2.1 `config/` 的 pydantic 模型与加载校验
- [x] 2.2 启动时链上交叉校验池配置（token0/token1/fee/tickSpacing 不符则拒绝启动）
- [x] 2.3 事实清单闸门：`facts.yaml` 存在 `verified: false` → 强制 dry-run
- **验收**：故意改错配置中的 fee，程序拒绝启动并打印差异

## M3 时段状态机
- [x] 3.1 `market/sessions.py`：按 `listings` 取各上市地时段并集，用 `zoneinfo` 处理夏令时
- [x] 3.2 `config/events.yaml` 读取；读取失败按「有事件」处理
- [x] 3.3 外汇周日开盘窗口（周日 17:00 ET ± 30min）
- [x] 3.4 输出 `should_make_market(now) -> bool` 与原因说明
- **验收**：单元测试覆盖夏令时切换日、周末、财报窗口、FX 开盘四类边界

## M4 纯链上时间出界判定
- [x] 4.1 池价持续位于界外达到 `confirm_seconds` 后确认
- [x] 4.2 确认前回到区间时清除计时并恢复 `IN_RANGE`
- [x] 4.3 `pin_timeout` 达到后仍在界外则作为上限保护确认
- **验收**：单元测试覆盖确认前、确认点、回归重置与保护上限四类边界

## M5 签名与执行层
- [x] 5.1 `chain/signer.py`：keystore + 环境变量口令，独立模块，策略层不接触私钥
- [x] 5.2 nonce 管理、EIP-1559 gas 估算
- [x] 5.3 交易前 `eth_call` 模拟，回滚即中止并记录 revert 原因
- [x] 5.4 地址与方法选择器白名单，非白名单拒签
- [x] 5.5 Intent 幂等：唯一 ID、落盘后再发送、重启可恢复
- **验收**：dry-run 下完整走通一次 mint 的构造与模拟，不发送

## M6 头寸操作
- [x] 6.1 NPM mint / decreaseLiquidity / collect / burn 封装
- [x] 6.2 SwapRouter swap 封装，滑点上限 30bps
- [x] 6.3 拆单：单笔 ≥ $500 才拆（3–5 笔、间隔 20–30s），否则直接成交
- [x] 6.4 **强制顺序 `burn → collect → swap → mint`**
- **验收**：X Layer 上用 < $50 真实资金完整走通建仓→撤出一轮

## M7 主状态机
- [x] 7.1 IDLE / ENTERING / IN_RANGE / OUT_PENDING / REBALANCING / EXITING 实现
- [x] 7.2 REBALANCING 统一处理：读两腿余额 → 算目标 50/50 → 差额一次 swap（不分方向）
- [x] 7.3 快循环 5s（做市时段）/ 60s（非做市时段）
- **验收**：dry-run 模式下用回放数据跑完整周期，状态转移日志正确

## M8 睡眠容忍与对账
- [ ] 8.1 启动与唤醒先读 NPM 链上头寸对账，以链上为准
- [ ] 8.2 `log/heartbeat` 心跳；失联超 15 分钟，唤醒后首条 TG 告警说明时长
- [ ] 8.3 未完成对账前禁止任何决策
- [ ] 8.4 部署脚本：`caffeinate -dimsu` 包裹 + `pmset repeat wakeorpoweron` + launchd plist
- **验收**：手动令 Mac 睡眠 10 分钟，唤醒后日志显示对账过程与失联告警

## M9 风控闸门
- [ ] 9.1 `log/HALT` 文件检查（每次写链前）
- [ ] 9.2 每池每日再平衡次数上限（默认 30），触顶转 IDLE
- [ ] 9.3 单日净值回撤 3% 熔断，全部撤出
- [ ] 9.4 所有 Intent 必经闸门，被否决的意图也要落日志
- **验收**：注入模拟条件逐条触发，每条都能拦住并告警

## M10 监控与记录
- [ ] 10.1 Telegram 告警（建仓、出界等待、重组、撤出、熔断、心跳失联）
- [ ] 10.2 逐笔再平衡记录：触发原因、出界方向、空窗时长、swap 成交价与滑点、已实现盈亏
- [ ] 10.3 每日摘要落 `log/`
- **验收**：一次完整再平衡在 TG 收到消息，且 jsonl 中有对应完整记录

## 后置（Round 2 前补，MVP 不做）
- 奖励归因与 NAV 快照 / 奖励密度监控 / 跨池迁移 / 退出决策
- 最优仓位求解器 / 合伙份额账本 / 财报日历 API
