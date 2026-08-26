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
- [ ] 8.1 NAV 快照从第一天存：记录时间、区块、LP 估值、闲置两腿与已实现损益
      （批次 8 已完成时间、区块、LP 估值与闲置两腿；已实现损益仍未接入）
- [ ] 8.2 启动与唤醒先读 NPM 链上头寸对账，以链上为准
- [ ] 8.3 `log/heartbeat` 心跳；失联超 15 分钟，唤醒后首条 TG 告警说明时长
- [ ] 8.4 未完成对账前禁止任何决策
- [ ] 8.5 部署脚本：`caffeinate -dimsu` 包裹 + `pmset repeat wakeorpoweron` + launchd plist
- **验收**：手动令 Mac 睡眠 10 分钟，唤醒后日志显示对账过程与失联告警

## M9 风控闸门
- [ ] 9.1 `log/HALT` 文件检查（每次写链前）
      （批次 8 已完成状态机每轮与启动授权检查；逐 Intent 阶段接线仍归 9.4）
- [x] 9.2 每池每日再平衡次数上限（默认 30），触顶禁止新仓但保留撤出权限
- [ ] 9.3 单日净值回撤 3% 熔断，全部撤出
- [ ] 9.4 所有 Intent 必经闸门，被否决的意图也要落日志
- **验收**：注入模拟条件逐条触发，每条都能拦住并告警

## M10 监控与记录
- [ ] 10.1 Telegram 告警（建仓、出界等待、重组、撤出、熔断、心跳失联）
- [ ] 10.2 逐笔再平衡记录：触发原因、出界方向、空窗时长、swap 成交价与滑点、已实现盈亏
- [ ] 10.3 每日摘要落 `log/`
- **验收**：一次完整再平衡在 TG 收到消息，且 jsonl 中有对应完整记录

## 后置（Round 2 前补，MVP 不做）
- 奖励归因 / 奖励密度监控 / 跨池迁移 / 退出决策
- 最优仓位求解器 / 合伙份额账本 / 财报日历 API

## M11 审核整改
- [x] 11.1 广播门控严格化
- [x] 11.2 RPC result 校验
- [x] 11.3 tx hash 校验
- [x] 11.4 滑点下限
- [x] 11.5 运行模式接线
- [x] 11.6 参数级白名单
- [x] 11.7 Intent 完整性与状态转移表
- [x] 11.8 再平衡幂等 resume
- [x] 11.9 独立签名进程
- [x] 11.10 D1 精确池价区间
- [x] 11.11 D4 同轮退出
- [x] 11.12 多 RPC 节点

## M12 链上头寸读取与授权检查（批次 5）
- [x] 12.1 用假 RPC 测试覆盖头寸解码、池过滤、零流动性、数量上限、同区块与授权边界
- [x] 12.2 实现 `uniswap/portfolio.py` 同区块只读账户快照
- [x] 12.3 实现 `tools/read_portfolio.py` 只读验收工具
- [x] 12.4 运行完整单测与真实链只读验收，并核对已锁定头寸字段

## M13 自动 ERC20 授权（批次 6，仅构造与校验）
- [x] 13.1 为两腿代币增加仅含 approve 的目标白名单与显式授权上限
- [x] 13.2 用参数级策略锁死 token、spender、额度、value 与 ABI 长度
- [x] 13.3 实现同区块 allowance 驱动的 `ApprovalManager` 并对 Intent 自检
- [x] 13.4 实现拒绝广播的 `tools/ensure_approvals.py` dry-run 工具
- [ ] 13.5 运行完整单测、真实链只读验收与 ERC20 多余方法自查

## M14 生产 MachineActions 接线与启动对账（批次 7，仅 dry-run）
- [x] 14.1 实现启动链上对账，以快照 tokenId 作为唯一白名单来源
- [x] 14.2 实现 enter、rebalance、exit 生产动作与失败即停顺序
- [x] 14.3 实现签名子进程 tokenId 集合刷新，保持资金安全锁不可变
- [x] 14.4 实现拒绝广播的 `tools/preview_actions.py` 全量预览工具
- [x] 14.5 完成 253 项单测、真实链 enter/exit 只读预览与 M7 dry-run 验收

## M15 简版 M9、NAV 与生产入口（批次 8）
- [x] 15.1 交互式明文私钥转 keystore 工具，敏感值不经 argv 或环境变量输入
- [x] 15.2 UTC 每日再平衡计数、HALT 与事实闸门组合，并区分撤出权限
- [x] 15.3 标准 V3 头寸估值与受 300 秒节流的按日 NAV JSONL
- [x] 15.4 decreaseLiquidity 在 exit 与 rebalance 中使用同区块价格和滑点下限
- [x] 15.5 生产入口完成三重广播门、对账、授权、签名地址校验与循环后处理
- [x] 15.6 完成 287 项单测、两条生产入口 CLI 验收，并保持 `mode: dry_run`

## M16 `.env` 明文私钥来源（批次 9）
- [x] 16.1 以 TDD 实现 `.env` 极简解析、格式校验、权限硬拒绝与 Git 跟踪检查
- [x] 16.2 扩展签名子进程为 keystore / dotenv 二选一，并保持主进程对象图不含私钥
- [x] 16.3 扩展生产入口密钥来源互斥参数、项目根 `.env` 默认选择与来源横幅
- [x] 16.4 增加 `.env` 忽略规则和可跟踪的占位 `.env.example`
- [x] 16.5 运行完整单测与敏感加载调用点自查，并保持 `mode: dry_run`

## M17 过渡阶段状态的链上对账复位（批次 11）
- [x] 17.1 以 TDD 覆盖清锁工具 ENTERING、EXITING、REBALANCING 与兼容模式
- [x] 17.2 扩展 `clear_stage_lock.py`，支持只读对账后的可选原子状态复位
- [x] 17.3 放宽生产入口 ENTERING 与 EXITING 启动闸门，并保留 REBALANCING 硬拒绝
- [x] 17.4 运行完整单测、OpenSpec 校验与 risk/log 红线自查

## M18 mint 配比滑点与确定性回滚短路（批次 12）
- [x] 18.1 以 TDD 实现两腿预算约束下的精确 mint 区间配比
- [x] 18.2 `enter` 基于配比后 desired 计算滑点下限，并允许区间外单腿为零
- [x] 18.3 RPC 识别并解码确定性合约回滚，立即中止跨节点重试
- [x] 18.4 完成 337 项单测、OpenSpec 校验与 risk/log 红线自查

## M19 时段闸门显式停用开关（批次 13）
- [x] 19.1 以 TDD 覆盖严格布尔、上市地跳过及事件、财报、外汇保护回归
- [x] 19.2 为 `MarketSessions` 增加 `ignore_listings` 并由 `from_files` 透传
- [x] 19.3 为生产入口增加 `--ignore-sessions`、醒目横幅与中文 warning 日志
- [x] 19.4 完成全量单测、OpenSpec 校验与 config/log 红线自查

## M20 REBALANCING 按再平衡日志判定复位（批次 15）
- [x] 20.1 以 TDD 覆盖进度与链上有效头寸联合判定矩阵
- [x] 20.2 为 `clear_stage_lock.py` 增加唯一未完成轮次定位与 `--rebalance-id`
- [x] 20.3 保持 swap 失败与生产入口 REBALANCING 无人值守路径硬拒绝
- [x] 20.4 完成全量单测、OpenSpec 校验与 config/log 红线自查
