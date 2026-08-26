## ADDED Requirements

### Requirement: 事实核实闸门
系统 SHALL 在事实清单存在未核实项时拒绝进入实盘模式。

#### Scenario: 未核实项存在时启动
- **WHEN** 运维启动系统且 `config/facts.yaml` 中存在 `verified: false` 的条目
- **THEN** 系统以 dry-run 模式启动，拒绝任何写链操作，并在日志与告警中列出未核实项

#### Scenario: 全部核实后启用实盘
- **WHEN** 所有事实条目标记为已核实且运维显式传入实盘参数
- **THEN** 系统允许执行写链操作

### Requirement: 合格池链上校验
系统 SHALL 在启动时对配置的每个池做链上交叉校验，不符即拒绝启动。

#### Scenario: 池配置与链上不一致
- **WHEN** 配置中池的 token0/token1/fee/tickSpacing 与链上读取结果不一致
- **THEN** 系统中止启动并输出差异明细

#### Scenario: 代币地址无效
- **WHEN** 配置的代币地址在 X Layer 上无合约代码，或 decimals 与配置不符
- **THEN** 系统中止启动并告警

### Requirement: 分时段区间带宽
系统 SHALL 依据美股交易时段、分时段已实现波动率与事件日历计算目标区间宽度。

#### Scenario: 休市时段收窄
- **WHEN** 当前处于美股完全休市时段且无事件窗口
- **THEN** 目标带宽收敛至该时段波动率对应的窄区间，且不小于 tickSpacing 决定的物理下界

#### Scenario: 开盘前拉宽
- **WHEN** 距美股开盘不足 30 分钟
- **THEN** 目标带宽不小于配置的开盘保护下限

#### Scenario: 事件日历不可用
- **WHEN** 财报或宏观事件日历拉取失败
- **THEN** 系统按存在事件处理，采用宽区间，并发出告警

### Requirement: 纯链上时间确认出界
系统 SHALL 只使用链上池价判断出界，不请求外部行情；确认后的再平衡不受收益成本比校验阻挡。

#### Scenario: 出界尚未达到确认时间
- **WHEN** 池价位于区间外但持续时间小于 `confirm_seconds`
- **THEN** 系统保持 `OUT_PENDING`，不产生再平衡意图

#### Scenario: 出界达到确认时间
- **WHEN** 池价持续位于区间外达到 `confirm_seconds`
- **THEN** 系统确认出界并进入 `REBALANCING`

#### Scenario: 确认前回到区间
- **WHEN** `OUT_PENDING` 中的池价在确认前回到区间内
- **THEN** 系统清除出界计时并回到 `IN_RANGE`

#### Scenario: 出界达到保护上限
- **WHEN** 池价从首次出界起达到 `pin_timeout` 后仍位于区间外
- **THEN** 系统作为上限保护确认出界并进入 `REBALANCING`

#### Scenario: 接近边界
- **WHEN** 当前价距区间边界小于带宽的 20%
- **THEN** 系统产生预防性再平衡意图，并按收益成本比校验决定是否执行

### Requirement: 风控闸门与熔断
系统 SHALL 使所有写链意图经过风控闸门，并在触发熔断条件时停止交易。

#### Scenario: 单池仓位超限
- **WHEN** 某意图会使单池本金超过总资金的配置上限
- **THEN** 闸门否决该意图并记录原因

#### Scenario: 单日亏损熔断
- **WHEN** 当日累计亏损（含无常损失）超过总资金的配置阈值
- **THEN** 系统撤出全部头寸、结清对冲腿并停止开新仓，同时告警

#### Scenario: 紧急停止文件
- **WHEN** 检测到 HALT 标记文件存在
- **THEN** 系统拒绝一切写链操作直至该文件被移除

#### Scenario: 交易模拟失败
- **WHEN** 交易在发送前的链上模拟中回滚
- **THEN** 系统中止该交易、记录 revert 原因并告警

### Requirement: LP Delta 对冲
系统 SHALL 计算 LP 头寸的实时 delta 并维持对冲腿在容忍区间内。

#### Scenario: 再平衡后同步对冲
- **WHEN** 任一 LP 头寸完成再平衡
- **THEN** 系统重算 delta 并同步调整对冲腿

#### Scenario: 对冲偏离超阈值
- **WHEN** 对冲腿与目标 delta 的偏离超过配置阈值
- **THEN** 系统产生再对冲意图

#### Scenario: 对冲通道不可用
- **WHEN** 对冲交易所不可用或下单持续失败
- **THEN** 系统停止开新仓、收窄现有敞口并告警

### Requirement: 奖励密度监控与退出
系统 SHALL 持续计算各池奖励密度，并在其低于阈值时退出。

#### Scenario: 奖励密度持续偏低
- **WHEN** 某池奖励密度连续低于阈值达到配置时长
- **THEN** 系统撤出该池头寸并评估迁移至奖励密度更高的合格池

#### Scenario: 活动临近结束
- **WHEN** 距活动结束时间不足配置的缓冲时长
- **THEN** 系统停止开新仓并按计划撤出

### Requirement: 收益归因与合伙记账
系统 SHALL 分项归因收益并按份额维护合伙账本。

#### Scenario: 每日净值快照
- **WHEN** 每日结算时点到达
- **THEN** 系统生成 NAV 快照，包含 LP 头寸估值、未领奖励、对冲腿权益与闲置资金，并落盘至日志目录

#### Scenario: 收益分项报表
- **WHEN** 生成日报
- **THEN** 报表分别列出手续费收入、激励收入、无常损失、对冲盈亏与 gas 成本，按池与时段拆分

### Requirement: 规则校准
系统 SHALL 采集实际到账奖励数据并反推奖励分配规则。

#### Scenario: 首日数据反推
- **WHEN** 累计到账数据达到可用样本量
- **THEN** 系统在多个候选分配规则假设下计算残差，输出校准报告并标注最优假设

#### Scenario: 校准前的仓位限制
- **WHEN** 规则校准尚未完成
- **THEN** 风控闸门将总投入限制在探针仓上限内

### Requirement: 同区块账户快照
系统 MUST 在确认 chainId 后读取一次区块高度，并将该高度的十六进制值传给本次账户快照中的每一次 `eth_call`。

#### Scenario: 所有账户读取固定在一个区块
- **WHEN** 系统读取指定 owner 的头寸、两腿余额和授权额度
- **THEN** 所有 `eth_call` 使用同一个已确定区块参数

### Requirement: 枚举并过滤本池 NPM 头寸
系统 MUST 枚举 owner 持有的全部 NPM tokenId，并且仅当 token0、token1 的顺序及 fee 均与目标池一致时才把头寸加入快照。

#### Scenario: 地址持有多个池的头寸
- **WHEN** owner 同时持有目标池头寸、不同 fee 头寸和不同币对头寸
- **THEN** 快照仅包含目标池头寸，并把其余头寸计入 `other_pool_position_count`

#### Scenario: 目标池头寸流动性为零
- **WHEN** 目标池头寸的 liquidity 等于零
- **THEN** 系统仍保留该头寸及 tokenId

### Requirement: 正确解码 NPM positions
系统 MUST 按 12 个 ABI 字的固定顺序解析 `positions(uint256)`，并把符号扩展存储的 tickLower 与 tickUpper 按 256 位有符号整数解码。

#### Scenario: 负数 tick 边界
- **WHEN** `positions` 返回符号扩展的负 tickLower 和 tickUpper
- **THEN** 快照中的两个 tick 均为正确负数

#### Scenario: 不暴露陈旧手续费字段
- **WHEN** 系统构造账户快照
- **THEN** 快照不暴露 feeGrowthInsideLast 或 tokensOwed 字段

### Requirement: 头寸数量失败关闭
系统 MUST 在 NPM `balanceOf(owner)` 超过 50 时以中文错误拒绝继续枚举。

#### Scenario: 地址持有过多 NPM NFT
- **WHEN** `balanceOf(owner)` 返回 51
- **THEN** 系统抛错且不读取任何 tokenId

#### Scenario: 地址没有 NPM NFT
- **WHEN** `balanceOf(owner)` 返回零
- **THEN** 系统返回空头寸元组和空 tokenId 集合

### Requirement: 读取两腿余额和授权额度
系统 MUST 读取 owner 的两腿 ERC20 原始余额，并读取两腿代币与每个指定 spender 的笛卡尔积授权额度。

#### Scenario: 授权额度边界
- **WHEN** 已记录授权额度恰好等于需要额度
- **THEN** `has_sufficient_allowance` 返回真；需要额度多一时返回假

### Requirement: 只读验收工具
系统 MUST 提供要求 `--owner` 参数的只读 CLI，输出区块、owner、本池头寸区间与流动性、是否处于区间内、两腿余额及 NPM 和 SwapRouter02 授权额度。

#### Scenario: 运行账户读取工具
- **WHEN** 操作者提供合法 owner 地址运行 CLI
- **THEN** 工具只使用只读 RPC 并打印账户快照，不构造或发送任何交易
