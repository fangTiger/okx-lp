# M6 头寸操作设计

## 目标与边界

M6 只负责把头寸动作转换为可审计、可模拟的 `Intent`，发送、签名、
nonce、gas 与广播授权继续由 M5 的 `TransactionExecutor` 统一处理。任何新入口都不
直接调用 `eth_sendRawTransaction`，执行器仍以 `allow_broadcast=False` 为默认值。
合约地址只从 `config/execution.yaml` 读取；源码只保留由官方 ABI 推导的方法签名。

事实闸门把未核实项分为 `live` 与 `size`。`live` 是写链否决项；`size` 不禁止交易，
但 `max_position_usd(configured, probe_capital_usd)` 返回配置仓位与探针仓位的较小值；
`n/a` 完全不参与判断。未核实项必须显式声明 `blocks`，避免配置遗漏时静默放行。

## 组件与数据流

`uniswap/position.py` 用 `eth_abi` 编码 NPM 的 mint、increaseLiquidity、
decreaseLiquidity、collect、burn。mint 接收当前 tick、宽度与 tick spacing，并且必须
调用 M1 的 `aligned_tick_range`，从而保证下沿向下、上沿向上取整。每个方法只返回
一个 `Intent`。

`uniswap/swap.py` 先构造 QuoterV2 的 `quoteExactInputSingle` 只读调用，解码真实报价，
再以 `Decimal` 和向下取整计算 `amountOutMinimum`。调用方请求的滑点不能超过
`risk.yaml` 的上限；缺省上限为 30 bps。金额低于 500 USD 时生成一笔 swap；达到
阈值时生成 3–5 笔，总原始金额严格守恒，相邻交易延迟 20–30 秒，每笔分别报价。

`strategy/rebalance.py` 提供 50/50 差额计算与固定四阶段执行器。阶段 `burn` 指
`decreaseLiquidity`（销毁流动性），随后依次 collect、swap、mint；NPM 的
`burn(tokenId)` 仍独立暴露，用于 collect 后销毁空 NFT。编排器逐阶段调用 M5，
每完成一阶段就原子写入 JSON 进度。异常、非预期 Intent 状态或中断都会停止后续阶段，
记录失败阶段和已经完成的阶段。

## 验证策略

单元测试解码 calldata，证明 tick 外扩、全部选择器、报价最小到账量、滑点拒绝、拆单
边界、金额守恒、50/50 双向差额、固定顺序与失败中止。集成验证只用 dry-run 和
`eth_call`：真实 QuoterV2 报价 20 USDC→wASMLx；四阶段打印完整 Intent、交易内容与
模拟结果，任何情况下都不传 `allow_broadcast=True`。
