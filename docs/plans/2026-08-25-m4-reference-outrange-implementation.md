# M4 参考价与出界判定实施计划

> **执行要求：** 使用 `executing-plans` 按任务逐项实施；用户已明确要求在当前目录完成，因此不另建 worktree。

**目标：** 以 Yahoo 的 ASML.AS 欧元价乘 EURUSD 即期构造美元公允价，并用基差相对 EWMA 的突变识别插针，在参考价不可用时安全退化为时间确认。

**架构：** `market.reference` 用只读 Protocol 隔离参考价提供方，Yahoo 实现负责 User-Agent、数据新鲜度、Decimal 解析与 TTL 缓存。`strategy.basis` 用三个间隔至少 60 秒的相近样本建立或重建 EWMA，`strategy.outrange` 是每池独立的有状态判定器；每次出界返回包含触发、价格、基差、依据和挂起时长的不可变事件，供后续 M7/M10 消费。

**技术栈：** Python 3.11、dataclasses、Decimal、datetime、enum、urllib、certifi、PyYAML、unittest；不新增依赖，不包含签名、私钥或交易发送。

---

### 任务 1：参考价接口与 Yahoo 实现（RED→GREEN）

**文件：**
- 创建：`tests/test_reference.py`
- 创建：`src/okxlp/market/reference.py`

**步骤：**
1. 用录制的 Yahoo JSON 测试 `regularMarketPrice` 相乘得到精确 `Decimal` 公允价，并断言两个请求均带 User-Agent。
2. 测试在 TTL 内只发起一轮双行情请求，TTL 后重新拉取。
3. 分别测试过期时间戳、网络异常、坏 JSON、字段缺失和非法价格均返回 `None`，不向主流程抛异常。
4. 测试 `NullReference.get_price()` 恒为 `None`。
5. 运行 `tests/test_reference.py`，确认因模块缺失而 RED。
6. 实现 `ReferencePrice` Protocol、`YahooFxAdrReference` 和 `NullReference`，使用 `json.loads(..., parse_float=Decimal)` 避免浮点误差。
7. 重跑定向测试确认 GREEN。

### 任务 2：基差 EWMA 与出界状态机（RED→GREEN）

**文件：**
- 创建：`tests/test_outrange.py`
- 创建：`src/okxlp/strategy/__init__.py`
- 创建：`src/okxlp/strategy/basis.py`
- 创建：`src/okxlp/strategy/outrange.py`

**步骤：**
1. 先用多个区间内样本预热 EWMA，再测试池价与公允价同步越界时立即进入 `CONFIRMED`。
2. 测试基差长期稳定在 `+0.32%` 时，即使价格真实移动也不会被判为插针。
3. 测试池价单独越界造成基差突变时进入 `OUT_PENDING`，回到区间后判为插针回归并恢复 `IN_RANGE`。
4. 测试参考价不可用时，价格连续界外满 `confirm_seconds=180` 才确认；中途回区间会清零计时。
5. 测试插针持续到 `pin_timeout=600` 时强制确认。
6. 对所有最终事件断言触发时间、方向、池价、公允价、基差、EWMA、结果、依据和挂起秒数完整存在。
7. 运行 `tests/test_outrange.py`，确认因模块缺失而 RED。
8. 实现 `BasisEwma`、`OutrangeState`、`OutrangeDirection`、`OutrangeResult`、`OutrangeEvent` 与 `OutrangeDetector`；异常基差在挂起期间不得更新 EWMA，错误基线可由持续一致的新样本重建。
9. 重跑定向测试确认 GREEN，并在绿色状态下整理重复分支。

### 任务 3：配置扩展（RED→GREEN）

**文件：**
- 修改：`tests/test_config.py`
- 修改：`src/okxlp/config.py`
- 修改：`config/pools.yaml`
- 修改：`config/pools.example.yaml`
- 创建：`config/risk.yaml`
- 修改：`config/risk.example.yaml`

**步骤：**
1. 测试实际池配置加载后包含 Yahoo provider、`ASML.AS`、`EURUSD=X`、60 秒缓存和 1800 秒新鲜度阈值。
2. 测试 `config/risk.yaml` 精确包含 `basis_jump_threshold=0.004`、`confirm_seconds=180`、`pin_timeout=600`。
3. 运行配置测试并确认因字段缺失而 RED。
4. 为 `PoolConfig` 增加不可变 `ReferenceConfig`，复用严格路径校验；更新正式配置与示例配置。
5. 创建正式风控文件，并同步示例中的 M4 参数。
6. 重跑配置测试确认 GREEN；检查所有 Python 生产文件均少于 200 行。

### 任务 4：验收与只读安全复核

**文件：**
- 不新增生产能力，仅执行验证。

**步骤：**
1. 运行 `tests/test_reference.py`、`tests/test_outrange.py`、`tests/test_config.py` 定向测试。
2. 用正式配置实例化 Yahoo 参考价，真实拉取一次行情，并结合当前只读池价记录打印公允价和基差；若外网或 RPC 不可达，保留原始错误并明确说明。
3. 运行 `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`。
4. 搜索 `private key`、`eth_send`、签名和交易发送入口，确认 M4 仍为纯只读。
5. 统计本次生产 Python 文件行数，确认均少于 200 行。
6. 记录 RED、GREEN、真实行情与全量测试的实际输出。

> 当前目录没有可识别的 `.git`，无法执行计划模板要求的逐任务 commit；交付以文件内容、RED/GREEN 输出和最终验证命令为证据。
