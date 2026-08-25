# X Layer Uniswap V3 只读观测器实施计划

> **执行要求：** 使用 `executing-plans` 按任务逐项实施；本会话已由用户明确要求直接实现，因此按同一计划继续执行。

**目标：** 在 X Layer 上持续读取配置中的 Uniswap V3 池，计算 ±0.5% 外扩对齐区间与不同本金的 in-range 份额，并写入每日 JSONL。

**架构：** 采用同步、分层的标准库实现。`JsonRpcClient` 统一处理 certifi TLS、节点轮换、超时和退避；`UniswapV3Pool` 只负责只读 ABI 调用及结构化解码；`tickmath` 保持纯函数；`observer` 负责配置、调度、落盘、摘要和信号生命周期。相比 web3.py 方案，此方案能直接复用已验证探针的调用和解码路径；相比 asyncio 方案，M1 单池 30 秒轮询无需额外并发复杂度。

**技术栈：** Python 3.11、标准库、certifi、PyYAML、unittest。

---

### 任务 1：建立运行与测试骨架

**文件：**
- 创建：`requirements.txt`
- 创建：`src/okxlp/__init__.py`
- 创建：`src/okxlp/chain/__init__.py`
- 创建：`src/okxlp/uniswap/__init__.py`

**步骤：**
1. 建立 Python 3.11 `.venv`。
2. 在虚拟环境安装 `requirements.txt`。
3. 使用 `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v` 作为测试入口。

### 任务 2：TickMath（RED→GREEN）

**文件：**
- 创建：`tests/test_tickmath.py`
- 创建：`src/okxlp/uniswap/tickmath.py`

**步骤：**
1. 先测试 `tick=-201526` 与快照 `sqrtPriceX96` 均折算到约 `1770.77` USDC。
2. 先测试价格到 tick 的反向换算允许一个 tick 的离散误差。
3. 先测试 ±0.5% 以 tick -201526 为中心向外对齐得到 `[-201580, -201470]`。
4. 先测试 `capital_to_liquidity` 与 `liquidity_to_capital` 可逆，份额公式为 `L_mine/(L_active+L_mine)`。
5. 运行测试确认因模块缺失而失败。
6. 实现价格/tick、sqrtPriceX96/价格、外扩对齐区间及流动性/金额纯函数。
7. 重跑定向测试确认通过。

### 任务 3：JSON-RPC 客户端（RED→GREEN）

**文件：**
- 创建：`tests/test_rpc.py`
- 创建：`src/okxlp/chain/rpc.py`

**步骤：**
1. 先测试请求体、RPC 错误、重试次数和节点轮换。
2. 先测试客户端默认链 ID 为 196、默认端点为 `https://rpc.xlayer.tech`，SSLContext 由 certifi CA 构造。
3. 运行测试确认失败。
4. 实现 `JsonRpcClient.call()` 与 `eth_call()`，只暴露只读通用 RPC 调用，不包含签名或交易发送。
5. 使用每轮指数退避，在每轮内依次尝试全部节点；达到上限后抛出中文错误。
6. 重跑定向测试确认通过。

### 任务 4：Uniswap V3 池读取器（RED→GREEN）

**文件：**
- 创建：`tests/test_pool.py`
- 创建：`src/okxlp/uniswap/pool.py`

**步骤：**
1. 用录制的十六进制返回值测试 slot0 的 int24 按 ABI 256 位符号扩展解码。
2. 测试 token 地址、symbol/name、decimals、余额、liquidity 与 block 组装为不可变 dataclass。
3. 运行测试确认失败。
4. 复用探针的 `word`、`as_int`、动态 string/bytes32 解码逻辑，实现只读调用。
5. 在快照 dataclass 中以 Decimal 暴露人类单位价格与余额。
6. 重跑定向测试确认通过。

### 任务 5：Observer（RED→GREEN）

**文件：**
- 创建：`tests/test_observer.py`
- 创建：`src/okxlp/observer.py`

**步骤：**
1. 先测试 YAML 配置加载、观测记录字段、份额档位与 JSONL 追加。
2. 先测试网络异常只记录告警并进入下一轮、停止事件可中断等待。
3. 运行测试确认失败。
4. 实现 30 秒按单调时钟调度、5 分钟摘要、每日文件轮换、SIGINT/SIGTERM 设置停止事件。
5. 份额档位固定为 50/100/500/2000/5000，区间固定 ±0.5%。
6. 重跑定向测试与全套测试。

### 任务 6：链上验收与交付证据

**文件：**
- 可能更新：`README.md`
- 生成（不纳入源码）：`log/observer_YYYY-MM-DD.jsonl`

**步骤：**
1. 在 `.venv` 中运行 `tools/probe_pool.py`，保存链 ID、区块、价格、tick、liquidity 输出。
2. 运行一次 Observer，将同一时段的字段与探针逐项比较。
3. 连续运行 10 分钟，确认进程无异常且 JSONL 约每 30 秒新增一行。
4. 检查所有 `.py` 文件不超过 200 行，并搜索签名、私钥和发送交易代码。
5. 重跑完整测试，记录命令、退出码与输出摘要。
