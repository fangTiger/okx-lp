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

### Requirement: 确定性合约回滚立即中止
系统 MUST 把合约执行回滚与暂时性网络错误区分处理；确定性回滚不得重试或跨节点故障转移，且错误消息必须保留合约给出的原因。

#### Scenario: 首个节点返回合约回滚
- **WHEN** 任一 RPC 调用返回 code 3、`execution reverted` 消息或标准 `Error(string)` 数据
- **THEN** 客户端抛出确定性合约回滚异常并立即中止，不再调用当前或其他节点

#### Scenario: 标准回滚数据包含字符串
- **WHEN** RPC 错误数据以 `0x08c379a0` 开头并包含 ABI 编码的回滚字符串
- **THEN** 客户端解码该字符串并附在异常消息中

#### Scenario: 暂时性网络错误
- **WHEN** RPC 节点超时或发生普通网络错误
- **THEN** 客户端继续按既有次数在节点间重试和故障转移

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

### Requirement: ERC20 授权参数级白名单
系统 MUST 仅允许本池两腿代币调用 `approve(address,uint256)`，spender 只能是 NPM 或 SwapRouter02，且额度不得超过该代币显式配置的正整数上限。

#### Scenario: 合法授权
- **WHEN** 本池代币向 NPM 或 SwapRouter02 授权，且额度在零到配置上限之间
- **THEN** 参数策略允许该 Intent 继续流转

#### Scenario: 无限授权或未知 spender
- **WHEN** 授权额度超过配置上限，或 spender 不是 NPM 与 SwapRouter02
- **THEN** 主进程和签名子进程均拒绝该 Intent

#### Scenario: 授权配置不完整
- **WHEN** 授权上限映射缺少任一池代币、包含多余代币或额度不是正整数
- **THEN** 参数策略构造失败，不使用默认值

### Requirement: 自动授权补足计划
系统 MUST 在一个固定区块读取全部所需 allowance；额度不足时构造补到对应配置上限的 approve Intent，并在返回前通过参数策略自检。

#### Scenario: 当前额度不足
- **WHEN** 当前 allowance 小于需求且需求不超过配置上限
- **THEN** 系统生成一笔额度等于配置上限的 approve Intent

#### Scenario: 需求超过上限
- **WHEN** 所需额度大于该代币配置上限
- **THEN** 系统以中文错误要求人工提高上限，不自动放宽配置

### Requirement: 授权只读 dry-run 工具
系统 MUST 提供要求 `--owner` 的授权检查 CLI，打印每个代币与允许 spender 的当前额度、充足状态及待执行 approve Intent，但不得签名或广播。

#### Scenario: 请求广播
- **WHEN** 操作者向授权检查工具传入 `--broadcast`
- **THEN** 工具以非零状态退出并说明广播需在生产入口接线完成后启用

### Requirement: 只读验收工具
系统 MUST 提供要求 `--owner` 参数的只读 CLI，输出区块、owner、本池头寸区间与流动性、是否处于区间内、两腿余额及 NPM 和 SwapRouter02 授权额度。

#### Scenario: 运行账户读取工具
- **WHEN** 操作者提供合法 owner 地址运行 CLI
- **THEN** 工具只使用只读 RPC 并打印账户快照，不构造或发送任何交易

### Requirement: 启动链上对账
系统 MUST 以同区块账户快照作为启动事实，并将其中全部本池 tokenId 作为 `allowed_token_ids` 的唯一来源。

#### Scenario: 多个历史头寸但仅一个有流动性
- **WHEN** owner 有多个本池头寸，但只有一个头寸的 liquidity 大于零
- **THEN** 系统记录 warning，并选择流动性最大的头寸作为 active_position

#### Scenario: 多个有效头寸
- **WHEN** owner 有两个或更多 liquidity 大于零的本池头寸
- **THEN** 系统抛出对账错误，要求人工处理

### Requirement: 生产状态机动作接线
系统 MUST 以严格布尔广播门控执行 enter、rebalance 与 exit；所有 Intent 按规定顺序逐笔交给执行器，任一失败立即停止后续阶段。

#### Scenario: 建仓本金未配置
- **WHEN** USDC 余额与事实闸门上限的最小值不大于零
- **THEN** enter 在构造任何 Intent 前失败关闭，并要求设置 `limits.total_capital_usd`

#### Scenario: 建仓完整顺序与参数保护
- **WHEN** enter 具有正数可用本金
- **THEN** 必要 approve 全部先于 swap 与 mint，mint 使用状态机传入的 ticks、按当前价与区间配比后的 desired、基于 desired 的滑点下限、owner recipient 和受限 deadline

#### Scenario: 建仓配比受两腿预算约束
- **WHEN** swap 完成或跳过后得到两腿可投入预算
- **THEN** 系统按当前 `sqrtPriceX96` 与目标 tick 区间求预算允许的最大流动性，并把该流动性的实际两腿数量作为 mint desired，任何一腿不得超过对应预算

#### Scenario: 建仓价格位于区间外
- **WHEN** 当前价格位于 mint 目标区间下方或上方
- **THEN** 数学上不需要的那一腿 desired 与 minimum 均允许为零，另一腿 minimum 按 desired 扣除最大滑点后向下取整

#### Scenario: 撤出清成 USDC
- **WHEN** exit 处理唯一有效本池头寸
- **THEN** 系统依次 decreaseLiquidity、collect、按实际余额把全部标的换成 USDC、burn NFT，并在实盘后检查剩余敞口

### Requirement: 签名子进程 tokenId 刷新
系统 MUST 接受最多 50 个非负整数 tokenId，并只替换子进程策略的 `allowed_token_ids`；其他安全字段保持不可变且无刷新入口。

#### Scenario: mint 后刷新新 tokenId
- **WHEN** 主进程把新 tokenId 集合发送给签名子进程
- **THEN** 新 tokenId 的合法 collect 可签出，但攻击者 recipient 仍被拒绝

#### Scenario: 非法刷新参数
- **WHEN** 刷新列表含负数、非整数或超过 50 项
- **THEN** 子进程拒绝刷新并继续使用原策略

### Requirement: 全量动作 dry-run 预览
系统 MUST 提供要求 `--owner` 和 `--action {enter,exit}` 的只读工具，在启动对账后按顺序打印完整交易内容、总笔数与 gas 预估，且不提供签署或发送路径。

#### Scenario: 请求预览工具广播
- **WHEN** 操作者传入 `--broadcast`
- **THEN** 工具以非零状态退出并说明生产入口在批次 8

### Requirement: 简版生产风控闸门
系统 MUST 按 HALT、live 事实、UTC 当日再平衡次数的顺序检查写链权限，并为撤出单独返回布尔权限。

#### Scenario: 人工急停完全冻结
- **WHEN** HALT 文件存在
- **THEN** 闸门返回 `allowed=false` 且 `allow_exit=false`，每轮重新读取文件

#### Scenario: 事实或次数只阻止新仓
- **WHEN** live 事实未核实或 UTC 当日再平衡次数达到配置上限
- **THEN** 闸门返回 `allowed=false` 且 `allow_exit=true`

#### Scenario: 完成一次再平衡
- **WHEN** 生产循环观察到 `REBALANCING` 转移至 `IN_RANGE`
- **THEN** 系统以原子 JSON 文件把当前 UTC 日期计数加一

### Requirement: NAV 基础快照
系统 MUST 使用字符串 Decimal 与整数 raw 数量记录基础 NAV，不得让 float 进入快照。

#### Scenario: 记录同区块头寸估值
- **WHEN** 生产循环完成一轮
- **THEN** 系统按标准 V3 三段式公式计算 LP 两腿数量，并把时间、区块、价格、LP 估值、闲置两腿与总值追加到 UTC 日期 JSONL

#### Scenario: NAV 写入节流
- **WHEN** 同一 UTC 日期距上次成功记录不足 300 秒
- **THEN** 记录器返回 false 且不追加新行；跨日时写入新的日期文件

### Requirement: decreaseLiquidity 滑点保护
系统 MUST 使用状态机决策轮同一区块的 `sqrtPriceX96` 计算撤流动性的预期两腿数量，并按配置滑点向下计算最小数量。

#### Scenario: 价格在区间内
- **WHEN** 头寸当前价格位于 tickLower 与 tickUpper 之间
- **THEN** exit 与 rebalance 的 decreaseLiquidity 两个最小数量均等于预期数量扣除最大滑点后的向下取整值，且均大于零

#### Scenario: 价格在区间外
- **WHEN** 当前价格位于区间下方或上方
- **THEN** 数学上无法取出的那一腿最小数量允许为零，另一腿必须具有滑点下限

#### Scenario: 签名边界拒绝双零下限
- **WHEN** 非零流动性的 decreaseLiquidity calldata 中两个最小数量同时为零
- **THEN** calldata 参数策略拒绝签名

### Requirement: 生产入口三重广播门
系统 MUST 只在配置 `mode=live`、显式 `--broadcast` 与精确交互确认同时成立时允许广播；用户显式传入 `--yes` 时可以代替第三重交互确认，但它不得代替前两重门。系统并 MUST 在所有退出路径关闭签名子进程。

#### Scenario: dry_run 请求广播
- **WHEN** 配置为 `mode=dry_run` 且传入 `--broadcast`
- **THEN** 入口在创建 RPC 客户端前非零退出

#### Scenario: 实盘确认错误
- **WHEN** 已请求广播但输入不等于 `我确认实盘`
- **THEN** 入口非零退出，不执行 approve 或状态机循环，并关闭签名子进程

#### Scenario: 显式非交互确认
- **WHEN** 同时传入 `--broadcast --yes`
- **THEN** `--yes` 代替精确交互输入；若未传 `--broadcast`，`--yes` 不得使广播权限变为 true

#### Scenario: 签名地址不一致
- **WHEN** keystore 推导的 signer.address 与 owner 不一致
- **THEN** 入口立即退出且不进入循环

### Requirement: `.env` 明文私钥安全加载
系统 MUST 只在签名子进程内从 `.env` 读取指定变量，并在解析前拒绝不存在、group/other 权限非零或已被 Git 跟踪的文件；私钥值 MUST 是可选 `0x` 前缀加恰好 64 位十六进制，任何错误均不得回显敏感内容。

#### Scenario: 安全文件中的合法变量
- **WHEN** 权限为 600 或 400 且未被 Git 跟踪的 `.env` 包含合法私钥变量
- **THEN** 签名子进程得到 32 字节私钥并完成握手，主进程只持有路径和变量名

#### Scenario: 文件安全前置检查失败
- **WHEN** `.env` 不存在、group/other 权限非零或已被 Git 跟踪
- **THEN** 系统在解析内容前硬性拒绝，并给出不含敏感值的中文修复提示

#### Scenario: 私钥变量缺失或格式非法
- **WHEN** 指定变量不存在、为空或不是恰好 64 位十六进制
- **THEN** 系统拒绝加载且错误消息不包含实际值或其片段

### Requirement: 互斥签名密钥来源
系统 MUST 要求 keystore 路径与口令环境变量、或 dotenv 路径与变量名这两组来源恰好提供一组；dotenv 加载函数 MUST 只在签名子进程函数内部调用，既有交易二道门校验保持不变。

#### Scenario: dotenv 端到端签名
- **WHEN** 签名子进程从安全 `.env` 启动并收到合法 collect 交易
- **THEN** 签名可恢复到该临时账户地址；攻击者 recipient 仍被子进程拒绝

#### Scenario: 来源同时提供或均未提供
- **WHEN** 调用方同时提供 keystore 与 dotenv，或两者均不提供
- **THEN** 主进程拒绝构造 RemoteSigner，子进程边界也拒绝非法握手参数

### Requirement: 生产入口选择签名来源
系统 MUST 在 argparse 层要求 `--keystore` 与 `--dotenv` 互斥；未显式提供时仅当项目根存在 `.env` 才默认使用该文件，并在启动横幅中只打印来源路径。

#### Scenario: 同时指定两个来源
- **WHEN** 操作者同时传入 `--keystore` 与 `--dotenv`
- **THEN** argparse 以非零状态退出且不启动 RPC、签名子进程或状态机

#### Scenario: 默认项目根 dotenv
- **WHEN** 两个来源均未显式提供且项目根存在 `.env`
- **THEN** 入口选择 `.env` 路径；项目根不存在 `.env` 时要求显式指定来源

### Requirement: 过渡阶段状态链上对账复位
系统 MUST 仅依据本池流动性大于零的链上头寸消除 ENTERING 与 EXITING 的歧义，并 MUST 对 REBALANCING 继续失败关闭。

#### Scenario: 建仓阶段按链上事实复位
- **WHEN** 本地状态为 ENTERING 且链上没有本池有效头寸
- **THEN** 系统记录中文 warning，并复位为 IDLE，同时清空区间与出界挂起字段

#### Scenario: 建仓已上链时恢复真实区间
- **WHEN** 本地状态为 ENTERING 且链上存在本池有效头寸
- **THEN** 系统记录中文 warning，并复位为 IN_RANGE，区间 tick 与价格均由链上头寸重建

#### Scenario: 撤出阶段按链上事实恢复
- **WHEN** 本地状态为 EXITING 且链上仍有本池有效头寸
- **THEN** 系统保持 EXITING 供主循环继续撤出；链上已无有效头寸时复位为 IDLE

#### Scenario: 再平衡阶段拒绝自动推导
- **WHEN** 本地状态为 REBALANCING
- **THEN** 系统非零退出并要求人工核对 `log/rebalances/` 进度与链上交易，不修改状态文件

#### Scenario: 人工清锁兼容模式
- **WHEN** 操作者未向清锁工具传入 `--reset-state`
- **THEN** 工具保持既有行为，只原子清空 failure 与 failed_at
