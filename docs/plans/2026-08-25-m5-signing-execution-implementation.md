# M5 签名与执行层实施计划

> **执行要求：** 使用 `executing-plans` 逐项实施；本会话已由用户明确要求直接实现，因此按同一计划继续执行。

**目标：** 实现安全的 keystore 签名、nonce 与 EIP-1559 gas 管理、双白名单、可恢复 Intent，以及默认绝不广播的交易执行器。

**架构：** 延续 M1–M4 的同步标准库、冻结 dataclass 与依赖注入风格。链上地址、选择器和 gas 边界集中在 `config/execution.yaml`；策略只构造 Intent，执行器按“白名单→落盘→模拟→gas→nonce→签名→可选广播→确认”运行，任何失败均以中文错误和持久化状态收口。

**技术栈：** Python 3.11、`unittest`、PyYAML、eth-account、现有 `JsonRpcClient`。

---

### 任务 1：配置与 keystore 签名器（RED→GREEN）

**文件：**
- 新增：`config/execution.yaml`
- 新增：`src/okxlp/chain/signer.py`
- 修改：`requirements.txt`
- 新增测试：`tests/test_signer.py`

**步骤：**
1. 用临时目录和随机测试账户生成 keystore，测试只从指定环境变量读取口令、地址与签名结果。
2. 测试错误口令给出明确中文错误，且私钥不出现在日志、异常、`repr` 或 `str` 中。
3. 运行定向测试，确认因模块或依赖缺失而失败。
4. 加入 `eth-account` 并实现闭包持有密钥的最小签名器，所有解密异常统一净化。
5. 重跑定向测试确认通过。

### 任务 2：白名单、nonce 与 EIP-1559 gas（RED→GREEN）

**文件：**
- 新增：`src/okxlp/chain/whitelist.py`
- 新增：`src/okxlp/chain/nonce.py`
- 新增：`src/okxlp/chain/gas.py`
- 修改：`src/okxlp/chain/rpc.py`
- 新增测试：`tests/test_whitelist.py`、`tests/test_nonce.py`、`tests/test_gas.py`

**步骤：**
1. 测试非白名单地址、错误选择器、短 calldata 均被拒绝；仅配置中目标与其选择器同时匹配时放行。
2. 测试 nonce 首次与重启取链上 pending，进程内递增，并在链上 pending 前进时对账追平。
3. 测试 gas limit 缓冲、低费率下限，以及基础费、优先费或 gas limit 异常偏高时失败关闭。
4. 运行定向测试确认缺少实现。
5. 实现三个小模块，并只给 RPC 增加查询、模拟、回执和默认禁用的广播入口。
6. 重跑定向测试确认通过。

### 任务 3：Intent 原子持久化与恢复（RED→GREEN）

**文件：**
- 新增：`src/okxlp/exec/__init__.py`
- 新增：`src/okxlp/exec/intent.py`
- 新增测试：`tests/test_intent.py`

**步骤：**
1. 测试唯一 ID、UTC 创建时间、原子落盘、同 ID 内容冲突拒绝。
2. 测试重启读回未完成 Intent，并按交易回执更新为成功或失败；无回执时保持待处理。
3. 运行定向测试确认缺少实现。
4. 实现冻结 Intent、状态枚举与 IntentStore，写入时 flush、fsync、原子替换。
5. 重跑定向测试确认通过。

### 任务 4：默认禁用广播的执行器（RED→GREEN）

**文件：**
- 新增：`src/okxlp/exec/executor.py`
- 新增测试：`tests/test_executor.py`

**步骤：**
1. 测试顺序为白名单、持久化、`eth_call`、gas、nonce、签名；非白名单在签名前拒绝。
2. 测试模拟回滚时中止、持久化 revert 原因且绝不签名或广播。
3. 测试默认 dry-run 走到签名、打印完整未签名交易、状态终结为 `dry_run`，广播调用次数为零。
4. 测试只有执行器和 RPC 两处都显式允许时才具备广播路径，并验证回执状态处理；测试本身不发真实网络交易。
5. 运行定向测试确认缺少实现。
6. 实现最小执行流程与逐步中文日志，重跑定向测试。

### 任务 5：mint dry-run 与最终验证

**文件：**
- 新增测试：`tests/test_m5_dry_run.py`
- 修改：`openspec/changes/add-xlayer-lp-automation/tasks.md`

**步骤：**
1. 按真实 NPM `mint` ABI 构造 calldata，用临时 keystore 和确定性 RPC 测试替身完整经过 `eth_call` 与签名，断言没有广播并打印交易。
2. 尽可能对 X Layer 公共 RPC 运行同一只读模拟探针；若无资金临时地址导致合约回滚，明确区分“RPC/ABI 已到达真实 NPM”与“成功模拟测试替身”。
3. 运行所有定向测试与完整 unittest。
4. 检查所有新增/修改 Python 单文件不超过 200 行，搜索私钥、口令示例与广播调用。
5. 验收满足后勾选 OpenSpec M5 条目，最后再次运行完整测试并记录原始输出。
