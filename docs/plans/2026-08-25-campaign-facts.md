# X Layer RWA 流动性激励活动 — 事实基线与待核实清单

> 采集日期：2026-08-25
> 用途：本文件是策略与系统设计的**唯一事实来源**。凡标注 `[待核实]` 的条目，
> 在实盘投入资金前必须由人工在官方渠道确认，禁止基于假设下单。

## 1. 已确认事实（有公开信源）

| 项 | 内容 | 信源 |
|---|---|---|
| 计划总规模 | 500 万美元 RWA 生态激励，分多轮发放 | Bitcoin World / mpost |
| Round 1 总额 | 30 万美元 | 同上 |
| Round 1 拆分 | RWA-稳定币对 20 万美元；RWA-生态代币对 10 万美元 | 同上 |
| RWA-稳定币档期 | 合格资产于 2026-08-24 公布，激励期为期两周（即约 08-24 ~ 09-07） | mpost |
| RWA-生态代币档期 | 2026-08-26 ~ 2026-09-02 | mpost |
| 协议范围 | 池必须部署在 Uniswap v2 / v3 / v4 上 | mpost |
| 池数量限制 | 每个合格代币**仅一个池**参与激励 | mpost |
| **奖励计算口径** | LP 按其**在池内产生的交易手续费占比**分配奖励 | mpost |
| **结算频率** | **按小时**计算 | mpost |
| 发放币种 | 稳定币 | mpost |
| 领取入口 | 平台「投资详情 / Investment Details」界面 | mpost |
| 参与门槛（生态代币） | 市值 ≥ 100 万美元；RWA 相关流动性 ≥ 20 万美元；活跃持币地址 ≥ 2000；前十地址持仓 ≤ 15% | mpost |
| 门槛非充分条件 | 满足门槛不保证入选，官方另行评估项目真实性、流动性质量、交易活跃度、生态贡献 | mpost |
| xPoints 叠加 | 持有或为合格 xStocks 资产提供流动性，可额外获得最高 45% 的 xPoints | OKX Wallet / xStocks |
| 链参数 | X Layer 主网 chainId = 196，原生 gas 代币 = OKB，公共 RPC = `https://rpc.xlayer.tech`，浏览器 = xlayerscan.com | chainid.network / XLayerScan |
| 链定位 | OKX 基于 Polygon CDK 构建的 zkEVM L2 | 官方 |
| Uniswap 状态 | Uniswap 协议、Web App、Wallet、Trading API 已在 X Layer 上线 | Uniswap 官方博客 |

## 2. `[待核实]` — 投入资金前必须逐项确认

编号用于 `config/pools.yaml` 与代码中的引用。

- **F1｜合格池清单**：8/24 公布的 RWA-稳定币合格资产与**具体 pool 地址**（含 Uniswap 版本与费率档）。
  官方公告页 / X Layer 官方推特 / OKX 公告中心。
- **F2｜每小时预算的池间分配规则**（**本设计中影响最大的未知项**）：
  - 假设 A：每小时总预算在各合格池之间**平均分配**；
  - 假设 B：按各池当小时**手续费绝对值**加权分配；
  - 假设 C：按各池 TVL 加权分配。
  三者导致**完全相反**的选池结论（见 `2026-08-25-strategy.md` §4）。必须以官方文案或首日实测发放数据反推确认。
- **F3｜"手续费占比"的精确定义**：是按已实现（collect）的手续费，还是按 accrued（未提取）手续费；是否只统计 in-range 时段。
- **F4｜是否需要报名/绑定地址**：奖励是自动归属钱包地址，还是需在 OKX Wallet 内主动注册活动。
- **F5｜领取机制**：链上 claim 合约地址 + ABI，还是 OKX 中心化侧发放；是否有 claim 截止时间。
- **F6｜Uniswap 在 X Layer 的合约地址**：v3 Factory / NonfungiblePositionManager / SwapRouter；v4 PoolManager / PositionManager / StateView / Quoter；Permit2。
  以 `https://developers.uniswap.org/docs/protocols/v3/deployments` 与官方 deployments.json 为准。
  **本文件不提供地址，禁止硬编码未经校验的地址。**
- **F7｜xStocks 在 X Layer 的 ERC-20 代币地址**：公开资料中广泛流传的是 Solana 地址（如 TSLAx `XsDoVfq...`），**不可用于 EVM**。X Layer 上的 EVM 地址需从 xlayerscan / OKX DEX 官方代币列表核对，并校验 decimals。
- **F8｜地域与合规限制**：xStocks 对部分司法辖区（含美国）不开放；确认参与方主体与 KYC 状态是否合规。
- **F9｜是否存在最低持仓时长 / 反女巫规则**：部分活动会剔除频繁进出的地址或多地址拆分。
- **F10｜xStocks 永续合约可用性**：用于 24/7 delta 对冲。已知 Kraken、Gate 提供 xStocks 永续，需确认具体标的覆盖、保证金币种、资金费率与本方主体可否开户。

## 3. 核实方式（自动化）

系统的 `campaign/verifier.py` 在每次启动时执行：

1. 拉取 F1 的官方池清单（人工录入 `config/pools.yaml` 后由程序做链上交叉校验：
   池地址的 token0/token1/fee 必须与配置一致，否则拒绝启动）。
2. 对 F6/F7 的每个地址做链上探针（`factory()`、`symbol()`、`decimals()`、代码字节长度非零）。
3. 记录首日实际发放数据到 `log/`，用于反推 F2/F3，形成《规则校准报告》后才允许放大仓位。

## 4. 信源

- [X Layer Launches $5M RWA Incentive Program, Opening $300K First Round For Uniswap Liquidity Providers — Metaverse Post](https://mpost.io/x-layer-launches-5m-rwa-incentive-program-opening-300k-first-round-for-uniswap-liquidity-providers/)
- [X Layer Launches $5M Incentive Program To Boost RWA Liquidity And Trading — Bitcoin World](https://bitcoinworld.co.in/x-layer-5m-rwa-incentive-program/)
- [Uniswap is Now Live on X Layer — Uniswap Blog](https://blog.uniswap.org/uniswap-is-now-live-on-x-layer)
- [X Layer Mainnet (chainId 196) — chainid.network](https://chainid.network/chain/196/)
- [XLayerScan 区块浏览器](https://www.xlayerscan.com/)
- [What is X Layer? Upgrades, Tokenomics, Ecosystem — OKX Wallet](https://web3.okx.com/learn/what-is-x-layer-upgrades-tokenomics-ecosystem)
- [Introducing xPoints — xStocks](https://xstocks.fi/us/news/introducing-xpoints)
- [OKX Launches 40+ Tokenized Stocks Powered by xStocks](https://xstocks.fi/us/news/okx-launches-tokenized-stocks-xstocks)
