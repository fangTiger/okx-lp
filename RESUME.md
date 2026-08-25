# 恢复点 — 2026-08-25 21:2x（断网前）

## 一句话状态
MVP 10 个任务完成 5 个（M1–M5），88 个单元测试全绿。
下一步是 M6（头寸操作 + 事实闸门分级修复），任务书已写好，路径见下。

## 进度

| 任务 | 状态 |
|---|---|
| M1 只读观测器 | ✅ 已独立验收（与探针交叉核对读数一致） |
| M2 配置与事实闸门 | ✅ 已独立验收（改错 fee_bps 能拒绝启动，退出码 2） |
| M3 时段状态机 | ✅ 已独立验收（含欧美夏令时错位周、fail-safe） |
| M4 参考价与出界判定 | ✅ 已独立验收（实跑基差 +0.393%） |
| M5 签名与执行层 | ✅ 已独立验收（私钥隔离 5 项、双重白名单 4 例、广播门控早退，均实跑通过） |
| M6 头寸操作 + 闸门分级 | ✅ 已独立验收（实链报价、滑点保护、50/50 计算四例、闸门放行） |
| M7 主状态机 | ⬜ 下一步 |
| M8–M10 | ⬜ 未开始 |

## 恢复后第一步

```bash
cd /Users/captain/python/Claude/okx-lp
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'   # 应为 88 OK
```

然后发 M6 给 codex（任务书在会话记录里，若丢失需按 tasks.md 的 M6 重写）：

```bash
codex exec -C /Users/captain/python/Claude/okx-lp -s workspace-write \
  --skip-git-repo-check --output-last-message <结果文件> - < <任务书文件>
```

## 已确认的链上地址（全部经链上核验 + 官方部署清单交叉验证）

| 用途 | 地址 |
|---|---|
| 池 wASMLx/USDC (fee 500, tickSpacing 10) | `0xc3d659028117f1ae5db9b9c68239b4a71f03ef37` |
| UniswapV3Factory | `0x4b2ab38dbf28d31d467aa8993f6c2585981d6804` |
| NonfungiblePositionManager | `0x315e413a11ab0df498ef83873012430ca36638ae` |
| SwapRouter02 | `0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca` |
| QuoterV2 | `0xd1b797d92d87b688193a2b976efc8d577d204343` |
| Permit2 | `0x000000000022D473030F116dDEE9F6B43aC78BA3` |
| WOKB (WETH9) | `0xe538905cf8410324e03a5a23c1c177a474d59b2b` |
| wASMLx (18 位) | `0x9147b03c16b18fc4f686f610f189f91ddf4347b4` |
| USDC (6 位) | `0xb6ceceab302e2e4948951ee7843fc24e92933061` |

## 事实清单最新状态

- **verified**：F1 合格池清单、F6 Uniswap 部署、F7 代币地址、F8 地域合规
- **n/a**：F4 报名、F5 领奖（人工处理）、F9 反女巫（单地址，确认不受影响）
- **blocks: size**：F2 预算池间分配规则、F3 手续费口径 —— 未校准前单池投入锁在 probe 上限

**闸门分级已落地**：无 live 级阻塞，写链放行；F2/F3 降级为仓位上限限制。

## 正在采集的数据（断网期间会报错，恢复后自动续上）

- `log/observer_{日期}.jsonl` —— 每 30 秒的池子快照
- `log/position_15857_{日期}.jsonl` —— 每 60 秒的头寸真值

重启命令：
```bash
nohup env PYTHONPATH=src .venv/bin/python -m okxlp.observer --pool-id wASMLx_USDC > log/observer_stdout.log 2>&1 &
nohup .venv/bin/python tools/track_position.py > log/track_position.log 2>&1 &
```

**已知小问题**：`tools/track_position.py` 的输出文件名在启动时算一次，跨零点不会滚动到新日期
（数据不丢，只是继续写在旧日期的文件里）。恢复后顺手修掉。

## 人工持仓（不依赖本系统，链上自持）

tokenId **15857**，区间 `[-201970, -201070]`（-4.35% / +4.65%），价值约 $39.56，
开仓时 100% 在区间内。用途是校准 F2/F3：
份额 = `21126254269852 / active_liquidity(t)`，配合 observer 的池子快照可重建任意时段的手续费份额。

## 关键实测数据（用于后续决策）

- 池子日手续费总额 ≈ **$8**（全池，不是我们的份额）
- 成交极度稀疏且突发：静默窗口约 2.2 笔/小时，撤出期约 24.9 笔/小时
- 活跃流动性 1 小时内 +27%，竞争者在涌入
- 已有竞争者在跑 `[-201550, -201510]` 的 ±0.2% 窄区间
- 基差（池价 / 公允价 − 1）稳定在 **+0.3%~0.4%**，所以插针判定必须用基差突变而非绝对偏离

## 最大的未决问题

**F2：每小时预算如何在各池之间分配。** 若按各池手续费加权，这个池日手续费只有 $8，
分到的激励可能微不足道，整个策略需要重新评估；若各池平均分配，则是罕见的错定价机会。
只能靠 tokenId 15857 的实际到账数据反推。
