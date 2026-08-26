# 交接文档 — 换机继续开发从这里开始

> 最后更新：2026-08-26
> 提交基线：`374fd75`　测试：115 项全绿　进度：MVP 27/38 项（M1–M7 完成，M8–M10 未开始）

---

## 0. 这个项目是什么

自动化参与 **X Layer RWA 流动性激励活动 Round 1**（2026-08-24 ~ 09-07，$300K 奖池）。
在 X Layer 的 Uniswap V3 池上做集中流动性做市，赚取活动激励与手续费。

**活动的奖励口径是「LP 按其在池内产生的手续费占比、按小时结算」**，不是常见的 TVL 时间加权。
这决定了收益由 **in-range 有效流动性份额** 决定，而份额 ≈ `本金 / 区间宽度`。
出界即当小时归零，所以 **uptime 是第一优先级指标**。

当前只做一个池：**wASMLx / USDC**（标的 ASML，阿姆斯特丹主上市 + NASDAQ ADR 双重上市）。

---

## 1. 新机器环境搭建

```bash
git clone git@github.com:fangTiger/okx-lp.git
cd okx-lp

python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 验证：应输出 115 tests OK
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'

# 验证链上连通与配置一致性：应输出「链上校验通过」，退出码 0
PYTHONPATH=src .venv/bin/python -m okxlp.campaign.verifier

# 看当前时刻系统会做什么决策（只读，0 广播）
PYTHONPATH=src .venv/bin/python tools/dry_run_m7.py
```

需要的外部条件：
- 无需 API key（系统已改为纯链上，不依赖任何外部行情源）
- 实盘才需要 keystore 文件 + 环境变量 `OKXLP_KEYSTORE_PASSWORD`（当前阶段不需要）

### 恢复数据采集（可选但建议）

```bash
nohup env PYTHONPATH=src .venv/bin/python -m okxlp.observer --pool-id wASMLx_USDC \
  > log/observer_stdout.log 2>&1 &
nohup .venv/bin/python tools/track_position.py > log/track_position.log 2>&1 &
```

历史数据已快照在 `data/snapshots/`（观测器 1397 条、仓位 579 条），新采集的会写到 `log/`（不入库）。

---

## 2. 必读文件（按顺序）

| 文件 | 内容 |
|---|---|
| `docs/plans/2026-08-25-requirements-locked.md` | **需求定稿 v2，唯一权威**。D1–D14 决策表、时段规则、状态机、风控闸门 |
| `docs/plans/2026-08-26-decision-log.md` | 决策演进过程与理由，含被修正过的错误判断 |
| `docs/plans/2026-08-25-campaign-facts.md` | 活动事实基线与未核实清单 F1–F10 |
| `docs/reviews/review-A-requirements.md` | 独立审核 A：需求符合性（判定 FAIL） |
| `docs/reviews/review-B-security.md` | 独立审核 B：安全与正确性（判定 FAIL） |
| `openspec/changes/add-xlayer-lp-automation/tasks.md` | 任务清单与勾选状态 |

---

## 3. 当前进度

| 模块 | 状态 |
|---|---|
| M1 只读观测器 | ✅ 已验收 |
| M2 配置与事实闸门 | ✅ 已验收 |
| M3 时段状态机 | ✅ 已验收 |
| M4 出界判定（纯链上时间确认） | ✅ 已验收 |
| M5 签名与执行层 | ⚠️ 已交付，**审核发现安全缺陷，见 §4** |
| M6 头寸操作 | ⚠️ 已交付，**审核发现安全缺陷，见 §4** |
| M7 主状态机 | ⚠️ 已交付，**审核发现两个 bug，见 §4** |
| M8 睡眠容忍与对账 | ⬜ 未开始 |
| M9 风控闸门 | ⬜ 未开始 |
| M10 监控与记录 | ⬜ 未开始 |
| 生产 MachineActions 适配器 | ⬜ 未开始（当前唯一实现是 dry-run 桩） |

**系统当前完全不能上实盘**：没有生产动作适配器，且存在下列未修复的安全缺陷。

---

## 4. 下一步：修复清单（两份独立审核的结论）

两份审核均判 **FAIL**。按优先级修复，修完让两个审核**复审**至全 PASS 再继续 M8–M10。

### P0 安全（实盘前必须清零）

1. **广播门控可绕过**：编排器、executor、rpc 三层用 `if not allow_broadcast`，传 `1`、`"true"`、
   非空对象均可到达 `eth_sendRawTransaction`。只有状态机层严格校验 bool。
   → 全层改为严格 `is True`。
2. **白名单只校验前 4 字节**：`collect` 的 recipient 可为任意地址，swap 的 recipient/token 亦然。
   → 增加参数级校验，recipient 锁死为自有地址，token/fee/tokenId 校验。
3. **篡改 SIGNED 记录可绕过白名单**：Intent 身份不含 `transaction`，恢复时直接重签持久化交易。
   → 身份纳入 transaction，恢复时重新过白名单与模拟。
4. **模拟不是 fail-closed**：`eth_call` 返回畸形数据（如 dict）仍继续广播。
   → 校验 result 必须是合法 hex 字符串，否则中止。
5. **崩溃后重复执行**：再平衡进度每次用空 progress 覆盖同一 ID 且从不 resume。
   → 支持 load/resume，已完成阶段不重跑。
6. **不校验 tx hash**：本地计算的 hash 与节点返回值不一致时仍标记 confirmed。
   → 不一致即中止告警。
7. **私钥可从闭包读出**：`signer._signer.__closure__` 能取出私钥；定稿要求的独立签名进程未实现。
   → 至少让闭包不可读；理想是落实独立签名进程。
8. 删除白名单中无调用者的 `increaseLiquidity`，缩小签名面。
9. 滑点 `amountOutMinimum == 0` 时应拒绝。
10. `config/risk.yaml` 的 `mode: dry_run` **没有任何代码读取**，需接线或删除。

### P1 需求符合性

11. **D1 区间中心用错**：状态机只把整数 `sample.tick` 传入区间构造，忽略精确 `sample.price`。
    反例：精确价 `1.00009999`、tick `0`、spacing `10`，代码给 `(-60,50)`，上沿仅 `+0.4912%`，
    窄于 0.5%，违反「一律向外取整」。
    → 区间构造改为接收精确池价，对 `price×0.995` 与 `price×1.005` 分别求 tick 再向外对齐。
12. **D4 撤出延迟 60 秒**：进入 `EXITING` 后本轮不执行退出，主循环按非做市时段睡 60 秒，
    下一轮才真撤出。上市地开盘瞬间仍留在池内最长 60 秒。
    → 进入 EXITING 同轮执行退出。
13. 生产配置只有一个 RPC endpoint，失联时无法撤出。→ 配置多节点。
14. **文档自相矛盾**：定稿 D10 写「NAV 快照从第一天存」，但 tasks.md 把 NAV 列为后置。
    → 二选一并统一。

### P2 剩余功能

- M8 睡眠容忍与链上对账（Mac 会睡眠，醒来必须先对账再决策）
- M9 风控熔断（单日回撤 3%、每日再平衡上限、HALT 文件）
- M10 Telegram 告警与逐笔记录
- 生产 `MachineActions` 适配器（真实 enter / rebalance / exit）

---

## 5. 已确认的链上地址（全部链上核验 + 官方部署清单交叉验证）

| 用途 | 地址 |
|---|---|
| 池 wASMLx/USDC（fee 500, tickSpacing 10） | `0xc3d659028117f1ae5db9b9c68239b4a71f03ef37` |
| UniswapV3Factory | `0x4b2ab38dbf28d31d467aa8993f6c2585981d6804` |
| NonfungiblePositionManager | `0x315e413a11ab0df498ef83873012430ca36638ae` |
| SwapRouter02 | `0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca` |
| QuoterV2 | `0xd1b797d92d87b688193a2b976efc8d577d204343` |
| Permit2 | `0x000000000022D473030F116dDEE9F6B43aC78BA3` |
| WOKB (WETH9) | `0xe538905cf8410324e03a5a23c1c177a474d59b2b` |
| wASMLx (18 位) | `0x9147b03c16b18fc4f686f610f189f91ddf4347b4` |
| USDC (6 位) | `0xb6ceceab302e2e4948951ee7843fc24e92933061` |

链参数：chainId **196**，RPC `https://rpc.xlayer.tech`，浏览器 `xlayerscan.com`，gas 代币 OKB。
实测 gas 价 **0.02 gwei**，一次完整再平衡约 103 万 gas ≈ **$0.002–0.004**，gas 不构成约束。

---

## 6. 链上人工持仓（不依赖本系统）

**tokenId 15857**，区间 `[-201970, -201070]`（约 -4.35% / +4.65%），开仓价值 **$39.56**。

用途是**校准活动的奖励分配规则**（F2/F3）。它刻意用宽区间，以便 24 小时都在场、
横跨静默期与活跃期两种成交量régime。

份额可由 `我的流动性 / 活跃流动性(t)` 重建，我的流动性恒为 `21126254269852`（不动仓时）。
手续费真值必须用 `eth_call` 模拟 `NPM.collect`（见 §7 踩坑记录第 2 条）。

---

## 7. 踩过的坑（别再踩一遍）

1. **int24 符号扩展**：ABI 里 int24 按 256 位符号扩展存放，按 24 位解会得到天文数字。
2. **`positions()` 的手续费字段是陈旧快照**：`tokensOwed` / `feeGrowthInsideLast` 只在头寸被操作时更新，
   不碰头寸永远不变。实时值必须对 `NPM.collect` 做 `eth_call`（它内部先 `pool.burn(...,0)` poke）。
3. **tick 是对数刻度，上下不对称**：`-0.5%` 需要 `|ln(0.995)|/ln(1.0001) = 50.13` tick，
   `+0.5%` 只需 `49.88` tick。两侧必须分别计算，否则下沿偏窄。
4. **公共 RPC 的 `eth_getLogs` 跨度上限是 100 个区块**，超过报 400。
5. **python.org 版 Python 缺根证书**，`urllib` 会报 `CERTIFICATE_VERIFY_FAILED`，
   必须用 `certifi` 构造 SSLContext。
6. **稀疏采样会严重低估成交量**：该池交易极度突发，每小时采 5 分钟的方式把日手续费低估了约 4 倍。
7. **不要拿过期读数做对比**：我曾用一小时前的池价与实时报价比较，误判出 0.65% 的「异常价差」。

---

## 8. 最大的未决问题

**F2：每小时预算如何在各合格池之间分配。**

- 若**各池平均分配** → 这个池日手续费只有约 $25，却能分到远超此数的激励，是罕见的错定价机会。
- 若**按各池手续费加权** → 这个池分到的激励可能微不足道，整个策略需要重新评估。

只能靠 tokenId 15857 的实际到账数据反推。奖励领取由人工处理（用户已明确），
系统不做领取自动化。
