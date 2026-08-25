# M2/M3 配置闸门与时段状态机实施计划

> **执行要求：** 使用 `executing-plans` 按任务逐项实施；本会话已由用户明确要求在当前目录直接继续，因此不另建 worktree。

**目标：** 用纯只读组件完成池配置校验、事实 dry-run 闸门、链上事实交叉验证，以及按上市地时段并集和事件窗口判定是否做市。

**架构：** `okxlp.config` 是 YAML 到不可变 dataclass 的唯一入口，负责字段、类型、地址、时区和时刻的显式校验。`campaign.gate` 只负责事实状态与写链否决，`campaign.verifier` 只做只读 RPC 交叉验证；`market.sessions` 组合上市地、财报和外汇窗口并返回中文原因。所有边界先由 unittest 固化，再写最小实现。

**技术栈：** Python 3.11、dataclasses、Decimal、zoneinfo、PyYAML、unittest；不新增依赖，不包含签名或交易发送。

---

### 任务 1：配置模型与显式校验（RED→GREEN）

**文件：**
- 创建：`tests/test_config.py`
- 创建：`src/okxlp/config.py`
- 修改：`config/pools.yaml`
- 修改：`config/pools.example.yaml`

**步骤：**
1. 测试实际 `pools.yaml` 可加载为不可变 dataclass，并保留 `fee_bps` 的 `Decimal` 精度。
2. 分别测试缺字段、布尔值冒充整数、错误地址和无效时区产生包含字段路径的中文错误。
3. 运行定向测试，确认因 `okxlp.config` 尚不存在而 RED。
4. 实现 `ConfigError`、`ChainConfig`、`TokenConfig`、`ListingConfig`、`PoolConfig`、`FxWindowConfig` 与 `AppConfig`。
5. 用严格辅助函数验证 mapping/list/string/bool/int/Decimal、EVM 地址、`HH:MM-HH:MM` 和 IANA 时区。
6. 把上市地改为显式 `timezone + hours_local`，把 FX 周日窗口写入 `session.fx_sunday_open`。
7. 重跑定向测试确认 GREEN。

### 任务 2：事实清单与强制 dry-run 闸门（RED→GREEN）

**文件：**
- 创建：`tests/test_campaign_gate.py`
- 创建：`src/okxlp/campaign/__init__.py`
- 创建：`src/okxlp/campaign/gate.py`
- 创建：`config/facts.yaml`

**步骤：**
1. 测试 F1/F6/F7 为 `true`，F2/F3/F4/F5/F8/F9 为 `false`，F10 为 `n/a`。
2. 测试存在 `verified: false` 时 `forced_dry_run` 为真，日志逐项列出未核实事实，`ensure_write_allowed()` 抛出中文 `PermissionError`。
3. 运行定向测试确认 RED。
4. 实现事实文件显式解析，状态仅允许布尔值或 `n/a`；缺失、坏 YAML、重复 ID 均报中文错误。
5. 实现启动日志与写链否决接口；当前 M2/M3 不调用任何写链方法。
6. 重跑定向测试确认 GREEN。

### 任务 3：池与代币链上交叉校验（RED→GREEN）

**文件：**
- 创建：`tests/test_campaign_verifier.py`
- 创建：`src/okxlp/campaign/verifier.py`

**步骤：**
1. 用录制快照测试 token0、token1、fee、tickSpacing、decimals 和 `eth_getCode` 全部匹配时通过。
2. 测试 fee 改错时抛出拒启异常，消息同时包含配置值与链上值。
3. 测试代币无代码或 decimals 不符时拒启，并列出全部差异而不是遇到第一项就停止。
4. 运行定向测试确认 RED。
5. 实现 `VerificationError` 与 `verify_campaign()`；fee 原始值按 `fee / 100` 转换为 bps 后比较。
6. 实现只读 CLI：加载配置、打印事实闸门、验证 chainId 和全部池；任何差异返回非零退出码。
7. 重跑定向测试确认 GREEN。

### 任务 4：上市地并集与 DST（RED→GREEN）

**文件：**
- 创建：`tests/test_sessions.py`
- 创建：`src/okxlp/market/__init__.py`
- 创建：`src/okxlp/market/sessions.py`

**步骤：**
1. 测试 2026 年美欧不同步切换 DST 的三段 UTC 边界，证明分别使用两个 IANA 时区。
2. 测试工作日任一上市地开盘即撤出、两地均收盘才做市、周六做市。
3. 加入指定北京时间断言：`2026-08-25 20:00` 撤出，`2026-08-26 06:00` 做市。
4. 运行定向测试确认 RED。
5. 实现 `MarketSessions.from_files()` 与 `should_make_market(now) -> (bool, reason)`；上市地按当地周一至周五和本地时刻判断并集。
6. 重跑定向测试确认 GREEN。

### 任务 5：财报与 FX 周日开盘 fail-safe（RED→GREEN）

**文件：**
- 修改：`tests/test_sessions.py`
- 修改：`src/okxlp/market/sessions.py`
- 创建：`config/events.yaml`

**步骤：**
1. 测试匹配标的在发布前 4 小时、发布时、发布后 18 小时的边界均撤出，窗口外恢复做市。
2. 测试事件文件缺失、坏 YAML、字段错误时全部 fail-safe 撤出并返回中文原因。
3. 测试周日 17:00 ET 前后配置窗口撤出，窗口外做市。
4. 运行定向测试确认 RED。
5. 创建空事件列表与中文格式注释；实现解析、标的过滤、事件优先级和 FX 窗口。
6. 重跑定向测试确认 GREEN。

### 任务 6：验收与只读安全复核

**文件：**
- 可能修改：`README.md`

**步骤：**
1. 运行全部定向测试与用户指定的 unittest discover 命令。
2. 暂时把实际 `config/pools.yaml` 的 `fee_bps` 改错，运行 verifier，保存非零退出与“配置值 vs 链上值”输出，然后立即恢复为 5。
3. 再运行 verifier 验证正确配置可通过；若外部 RPC 不可达，明确区分网络阻塞与单元测试证据。
4. 检查所有本次 Python 文件不超过 200 行。
5. 搜索签名、私钥、`eth_send*` 和交易发送代码，确认 M2/M3 纯只读。
6. 重跑完整测试并记录原始输出。

> 当前目录没有 `.git`，因此无法执行计划模板要求的逐任务 commit；所有修改以文件 diff、RED/GREEN 输出和最终验证命令为证据。
