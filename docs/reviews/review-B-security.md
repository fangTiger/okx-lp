# 安全与正确性审核报告（审核员 B）

审核基线：`main`，HEAD `374fd75`，审核日期 2026-08-26（Asia/Shanghai）。本次按磁盘上的当前版本审核；未修改源码、配置或测试。项目不存在 `graphify-out/graph.json` / `graphify-out/GRAPH_REPORT.md`，故降级为源码、OpenSpec、测试和实际运行审查。

## 结论

总体：**FAIL**

**禁止以真实资金上线。** 115 项现有测试虽然全部通过，但对抗验证实际复现了：私钥可从 signer 对象闭包读取；状态机之外三层广播门控接受 `1`、`"true"`、非空对象；白名单允许把 `collect` 收款人改成攻击地址；篡改 `SIGNED` 恢复记录可绕过白名单与模拟签出任意原生币转账；畸形 `eth_call` 结果仍进入广播；RPC 返回与本地计算不一致的 tx hash 仍会被标记 confirmed；再平衡崩溃恢复会重复已完成阶段；tick 上沿存在未向外对齐的边界反例。

此外，生产安全闭环尚未完成：M8（启动/唤醒链上对账）、M9（真实风控闸门）、M10（告警与记录）在任务清单中仍为未完成，见 `openspec/changes/add-xlayer-lp-automation/tasks.md:57-75`。仓库中没有生产 `MachineActions` 实现或 live 入口；唯一主状态机接线是永久禁止广播的验收工具，见 `tools/dry_run_m7.py:58-87,97-138`。

## 逐项核验

| 编号 | 属性 | 判定 | 证据 |
|---|---|---|---|
| 1 | 私钥隔离 | **FAIL** | 口令确实只由 `os.environ.get(password_env)` 读取，异常也屏蔽底层原因：`src/okxlp/chain/signer.py:22-37`；`repr` 只显示地址：`src/okxlp/chain/signer.py:71-73`。但对象把签名闭包暴露为可读 `_signer` 槽：`src/okxlp/chain/signer.py:39-46,52-60`。攻击 A1 实测 `private_key_recovered_from_object_attribute=True`。设计要求的独立签名进程也未实现（现为同进程对象）。 |
| 2 | 广播门控 | **FAIL** | 状态机严格拒绝非 `bool`：`src/okxlp/strategy/machine_loop.py:14-18,24-35`。但编排器以真值决定期望状态并原样下传：`src/okxlp/strategy/rebalance.py:101-139`；executor 用 `if not allow_broadcast`：`src/okxlp/exec/executor.py:134-147`；RPC 同样用真值判断：`src/okxlp/chain/rpc.py:150-158`。攻击 A2 实测三层均接受 `1`、`"true"`、非空对象并到达 `eth_sendRawTransaction`。此外 `config/risk.yaml:3` 的 `mode: dry_run` 在 `src/`/`tools/` 没有执行读取；真实启动校验反而输出“模式=可请求实盘”。 |
| 3 | 白名单 | **FAIL** | 地址与 selector 双重绑定本身正确：`src/okxlp/chain/whitelist.py:60-70`。按官方 ABI 独立 keccak 的六个 selector 全部与 `config/execution.yaml:28-41` 匹配（运行记录 R2）。但只检查前 4 字节，不校验 token、fee、tokenId、金额或 recipient；`collect` 允许任意 recipient：`src/okxlp/uniswap/position.py:86-93`，swap 同样允许任意 recipient/token 对：`src/okxlp/uniswap/swap.py:133-149`。攻击 A3 实测攻击地址 recipient 被白名单接受并进入广播。`increaseLiquidity` 已开放但生产源码无调用者，搜索仅命中定义、配置和测试，扩大了签名面。 |
| 4 | 交易前模拟 | **FAIL** | JSON-RPC error/revert 会保存 `FAILED`、记录原原因并中止签名：`src/okxlp/exec/executor.py:89-100`，全量测试也实际输出 `模拟回滚...execution reverted: 价格保护`。但 executor 完全忽略成功响应的值，RPC 只校验 envelope、不校验 `result` 类型/hex：`src/okxlp/chain/rpc.py:130-140`。攻击 A5 中 `eth_call.result` 为 dict，最终仍 `status=confirmed` 并广播，模拟不是可靠 fail-closed。 |
| 5 | 幂等与崩溃恢复 | **FAIL** | 单个相同 Intent ID 的首次落盘具有内容身份校验：`src/okxlp/exec/intent.py:105-122`；发送态可按 receipt 对账：`src/okxlp/exec/intent.py:163-184`。但身份不包含 `transaction`，`save` 不校验状态转移：`src/okxlp/exec/intent.py:105-129`；恢复 `SIGNED` 时直接重签持久化 transaction：`src/okxlp/exec/executor.py:80-83,134-147`，攻击 A4 已绕过白名单。再平衡每次都用空进度覆盖同一 ID，且从不 load/resume：`src/okxlp/strategy/rebalance.py:101-107`；攻击 A7 显示重启后 burn/collect 重复执行。`reconcile_pending` 在生产源码无调用者，M8 链上头寸对账也未实现：`openspec/changes/add-xlayer-lp-automation/tasks.md:57-61`。 |
| 6 | 滑点保护 | **PASS** | `amountOutMinimum = floor(amountOut × (10000-slippage_bps)/10000)`：`src/okxlp/uniswap/swap.py:106-131`；上限来自 `config/risk.yaml:9-15`。独立计算与实现对 `10000→9970`、`10001→9970` 一致；`30.0001 bps` 在报价前拒绝且 `rpc_calls=0`（攻击 A10）。边界 `amountOut=1` 会得到 0，数学上是 floor 的正确结果，但直接调用该低层 API 时等于无最低到账保护，建议额外拒绝 `minimum == 0`。 |
| 7 | tick 数学 | **FAIL** | Decimal 价格↔tick 和 sqrtPriceX96 转换使用 80 位上下文：`src/okxlp/uniswap/tickmath.py:14-54`；极值 tick 往返实测通过。已知输入却不一致：独立计算 `tick=-201526` 对应 `1771.228546...`，而 `1770.77` 的 floor tick 为 `-201529`；现有测试只容忍 5 bps 并做 tick 自身往返，没有断言该价格映射：`tests/test_tickmath.py:20-32`。更严重的是区间只以整数 tick 为中心：`src/okxlp/strategy/machine_types.py:48-57`、`src/okxlp/uniswap/tickmath.py:57-73`。攻击 A8 构造合法分数 tick `-201529.5`，实现上沿 `-201480`，独立向外结果 `-201470`，实际上沿仅 `+0.496202% < +0.5%`，违反“一律向外”。 |
| 8 | 失败路径 | **PASS** | 编排器严格顺序为 burn→collect→swap→mint，任何 `BaseException` 立即记录失败并抛出：`src/okxlp/strategy/rebalance.py:101-149`；主状态机把阶段失败持久化锁停：`src/okxlp/strategy/machine_stages.py:14-60`，后续轮次在动作前保持锁停：`src/okxlp/strategy/machine.py:77-82`。攻击 A9 对四阶段逐一注入失败，所有后续阶段均被跳过。此 PASS 仅指受控异常路径；硬崩溃后的重复执行已在第 5 项判 FAIL。 |
| 9 | 数值精度 | **PASS** | 资金计算采用 raw integer + Decimal：50/50 计算及向下转换见 `src/okxlp/strategy/allocation.py:59-106`；滑点向下取整见 `src/okxlp/uniswap/swap.py:126-131`；gas buffer 向上取整见 `src/okxlp/chain/gas.py:94-121`。`float` 只出现在观测日志/展示和时间间隔：`src/okxlp/observer.py:54-78,108-109`，未进入当前交易决策。拆单用 `divmod` 保留 raw 总量：`src/okxlp/uniswap/swap.py:161-179`。 |
| 10 | 外部依赖失效 | **FAIL** | 全部节点超时会按节点×重试穷尽后抛 `RpcError`：`src/okxlp/chain/rpc.py:66-109`；状态机捕获并保持原状态、不广播：`src/okxlp/strategy/machine_loop.py:24-44`，攻击 A6 实测 attempts=6、state=IN_RANGE、broadcasts=[]。但生产配置只有一个 endpoint：`config/pools.yaml:5-9`，失联时无法撤出/重组。畸形模拟响应会 fail-open（A5）。更严重的是 executor 不比较本地预期 hash 与 RPC 返回 hash：本地计算见 `src/okxlp/exec/executor.py:124-131`，随后无条件以节点 hash 覆盖并按其 receipt 确认：`src/okxlp/exec/executor.py:146-173`；攻击 A6b 实测 `hash_match=False final_status=confirmed`，可破坏四阶段链上顺序。 |
| 11 | 出界判定 | **PASS** | `confirm_seconds`/`pin_timeout` 配置为 180/600：`config/risk.yaml:5-7`；未达时长、达到时长、回区间重置、超时强制确认分别实现在 `src/okxlp/strategy/outrange.py:85-137`。首次出界时间/方向写入 snapshot：`src/okxlp/strategy/machine.py:101-137`；状态文件校验与恢复见 `src/okxlp/strategy/machine_state.py:50-105`，检测器重建见 `src/okxlp/strategy/machine.py:174-180`。全量测试中的五个 `test_outrange` 和两个 restart timer 测试均实际为 `ok`（R1）。 |

## 攻击尝试记录

### A1：从 signer 对象属性提取私钥

- 目的：验证“对象属性不可读出私钥”。
- 输入：临时随机账户 keystore；从 `signer._signer.__closure__` 遍历闭包单元，仅比较是否等于临时私钥，不打印密钥。
- 实际结果：**未挡住**。

```text
has_readable__signer_attribute=True
closure_cell_types=['HexBytes']
private_key_recovered_from_object_attribute=True
repr_contains_private_key=False
repr_contains_password=False
EXIT_CODE=0
```

### A2：四层注入 truthy 非布尔广播值

- 目的：验证 `1`、`"true"`、非空对象不能开启广播。
- 输入：分别传入三种值；传输层使用假 RPC，绝不触碰真实链。
- 实际结果：状态机挡住；编排器、executor、RPC 三层均未挡住。

```text
input_type=int state_machine=BLOCKED:TypeError orchestrator_completed=('burn', 'collect', 'swap', 'mint') executor_arg_types=['int', 'int', 'int', 'int']
input_type=str state_machine=BLOCKED:TypeError orchestrator_completed=('burn', 'collect', 'swap', 'mint') executor_arg_types=['str', 'str', 'str', 'str']
input_type=object state_machine=BLOCKED:TypeError orchestrator_completed=('burn', 'collect', 'swap', 'mint') executor_arg_types=['object', 'object', 'object', 'object']

input_type=int status=confirmed broadcast_count=1 rpc_permission=True
input_type=str status=confirmed broadcast_count=1 rpc_permission=True
input_type=object status=confirmed broadcast_count=1 rpc_permission=True

input_type=int accepted=True result=0xabababab methods=['eth_chainId', 'eth_sendRawTransaction']
input_type=str accepted=True result=0xabababab methods=['eth_chainId', 'eth_sendRawTransaction']
input_type=object accepted=True result=0xabababab methods=['eth_chainId', 'eth_sendRawTransaction']
```

### A3：白名单方法使用攻击者 recipient

- 目的：验证白名单是否约束资金接收人。
- 输入：真实白名单 NPM 地址、官方 `collect` selector、现有 tokenId 15857、recipient=`0x9999...9999`。
- 实际结果：**未挡住**；实际 TransactionWhitelist、TransactionExecutor 的假传输完整路径均通过。

```text
selector=0xfc6f7865 decoded_token_id=15857 decoded_recipient=0x9999999999999999999999999999999999999999
whitelist_accepted_attacker_recipient=True status=confirmed signed=1 broadcast_count=1
EXIT_CODE=0
```

### A4：篡改 SIGNED 恢复记录绕过白名单

- 目的：验证崩溃恢复记录不能改变已授权交易。
- 输入：Intent 身份为白名单 NPM `mint`、value=0；持久化 `transaction` 改为向攻击地址发送 `123456789` wei。
- 实际结果：**未挡住**；恢复分支重签恶意 transaction，未重新模拟或校验一致性。

```text
whitelisted_intent_target=0x315e413a11ab0df498ef83873012430ca36638ae signed_transaction_target=0x9999999999999999999999999999999999999999
signed_native_value=123456789 status=confirmed broadcast_count=1
EXIT_CODE=0
```

### A5：RPC 对 eth_call 返回畸形成功 result

- 目的：验证模拟成功必须是有效 EVM hex 返回，而非任意 JSON 值。
- 输入：实际 JsonRpcClient 收到 `{"jsonrpc":"2.0", ..., "result":{"malformed":true}}`。
- 实际结果：**未挡住**；继续签名、调用广播、按 receipt confirmed。

```text
malformed_eth_call_result_type=dict status=confirmed signed=1
rpc_methods=['eth_chainId', 'eth_call', 'eth_sendRawTransaction', 'eth_getTransactionReceipt']
EXIT_CODE=0
```

### A6：全部 RPC 节点超时

- 目的：验证全部节点挂掉时不产生交易决策。
- 输入：2 个 endpoint、每个 3 次尝试，全部 `TimeoutError("timed out")`；主状态机位于 IN_RANGE 且 market 抛 RpcError。
- 实际结果：**挡住广播**，但保持原仓位，无法撤出。

```text
all_nodes_down=RpcError attempts=6 backoffs=[Decimal('0.25'), Decimal('0.50')]
machine_state_after_rpc_failure=IN_RANGE broadcasts=[] reason=步骤失败，停留在 IN_RANGE：all RPC offline
EXIT_CODE=0
```

### A6b：RPC 返回另一笔交易的 hash 与成功回执

- 目的：验证节点返回 hash 必须等于本地 raw transaction 的 keccak。
- 输入：本地 raw=`0x0201`；RPC 返回 `0xabab...abab` 并给该 hash 返回 status=1。
- 实际结果：**未挡住**；hash 不一致仍 confirmed。

```text
locally_expected_hash=0x114a3fe82a0219fcc31abd15617966a125f12b0fd3409105fc83b487a9d82de4
rpc_returned_and_stored_hash=0xabababababababababababababababababababababababababababababababab
hash_match=False final_status=confirmed
EXIT_CODE=0
```

### A7：再平衡中断后按相同 ID 重启

- 目的：验证已完成阶段不会在崩溃/重启后重复执行。
- 输入：首次在 swap 注入失败，日志已记录 burn、collect 完成；随后以相同 `rebalance_id` 重启执行。
- 实际结果：**未挡住**；burn、collect 均执行第二次。

```text
journal_before_restart_completed=['burn', 'collect'] failed_stage=swap
after_restart_completed=('burn', 'collect', 'swap', 'mint')
burn_execute_count=2 collect_execute_count=2
EXIT_CODE=0
```

补充：即使第一轮完整完成，同一 ID 再执行也会把四阶段全部执行第二次：

```text
first_completed=('burn', 'collect', 'swap', 'mint') second_completed=('burn', 'collect', 'swap', 'mint')
burn_execute_count=2 collect_execute_count=2 mint_execute_count=2
```

### A8：tickSpacing 上沿向外取整反例

- 目的：验证任何合法池价位置都“宁宽勿窄”。
- 输入：tickSpacing=10，实际 raw tick=`-201529.5`，其合法 current tick=`floor(raw)=-201530`，width=0.5%。
- 实际结果：**未挡住**；实现上沿少一档，窄于目标。

```text
constructed_raw_tick=-201529.5 price_to_tick=-201530 supplied_current_tick=-201530
implementation_range=(-201590, -201480) independent_outward_range_from_actual_price=(-201580, -201470)
implementation_upper_relative_width=0.004962022778052023171034788047520103507801542812467915115489377208594923769601912417628176684099341 target_width=0.005
upper_is_outward=False
EXIT_CODE=0
```

### A9：再平衡四阶段逐一失败

- 目的：验证任一阶段失败立即停住、后续阶段不执行。
- 输入：分别在 burn、collect、swap、mint 注入异常。
- 实际结果：**均挡住后续阶段**，失败阶段和已完成阶段正确落盘。

```text
failed_stage=burn persisted_failed_stage=burn completed=[] later_stage_skipped=True
failed_stage=collect persisted_failed_stage=collect completed=['burn'] later_stage_skipped=True
failed_stage=swap persisted_failed_stage=swap completed=['burn', 'collect'] later_stage_skipped=True
failed_stage=mint persisted_failed_stage=mint completed=['burn', 'collect', 'swap'] later_stage_skipped=True
EXIT_CODE=0
```

### A10：滑点边界与超限

- 目的：独立核算 floor 方向，并验证超过 30 bps 在 RPC 前拒绝。
- 输入：amountOut 10000、10001、1；slippage 30 bps；另传 30.0001 bps。
- 实际结果：公式与 floor 一致，超限被挡住。

```text
amount_out=10000 expected_floor=9970 actual_min=9970 match=True
amount_out=10001 expected_floor=9970 actual_min=9970 match=True
amount_out=1 expected_floor=0 actual_min=0 match=True
above_limit=BLOCKED:ValueError:滑点 30.0001 bps 超过配置上限 30 bps rpc_calls=0
EXIT_CODE=0
```

## 发现的问题

### CRITICAL-1：私钥可从 signer 对象闭包直接恢复

- 问题：`KeystoreSigner._signer` 是可读 Python 函数对象，闭包捕获了解密后的 `HexBytes` 私钥。
- 影响：任何拿到 signer 引用的策略代码、依赖或注入代码都能导出私钥；这不构成“独立签名进程”隔离。
- 复现：攻击 A1；源码 `src/okxlp/chain/signer.py:34-46,52-60`。
- 建议修法：签名器移至独立最小权限进程/硬件钱包或系统 Keychain，策略进程只通过严格 IPC 请求签名；IPC 服务自行校验 chainId、target、selector、完整参数与金额。不要以 Python 闭包作为密钥隔离边界。

### CRITICAL-2：广播权限只在状态机严格，三层可被任意真值开启；dry_run 配置未执行

- 问题：编排器、executor、RPC 均用 truthiness；`mode: dry_run` 没有进入执行链，事实 verifier 也不读取该 mode。
- 影响：配置解析、CLI 或调用者传入 `1`/字符串/对象时会真实进入广播；绕过状态机直接调用 executor 同样可发交易。
- 复现：攻击 A2；`src/okxlp/strategy/rebalance.py:101-139`、`src/okxlp/exec/executor.py:134-147`、`src/okxlp/chain/rpc.py:150-158`。真实启动 R3 在 `config/risk.yaml:3` 为 dry_run 时输出“模式=可请求实盘”。
- 建议修法：四层都使用 `type(value) is bool and value is True`；executor/RPC 再独立读取一个不可由策略覆盖的运行授权（例如启动时一次性 capability），并把 `risk.yaml.mode`、事实闸门、HALT、仓位/次数/回撤限制组合成发送前最终否决门。

### CRITICAL-3：SIGNED 恢复记录可替换任意交易，绕过白名单、模拟与 Intent value

- 问题：Intent identity 不包含 `transaction`；`save` 不验证合法状态迁移；恢复 SIGNED 时重签存储 transaction，且不验证 `to/data/value/chainId/nonce` 与 Intent 一致。
- 影响：本地状态文件被篡改、部分损坏或被低权限同机进程改写后，可让热钱包签出任意原生币转账/合约调用。
- 复现：攻击 A4；`src/okxlp/exec/intent.py:105-129`、`src/okxlp/exec/executor.py:80-83,134-147`。
- 建议修法：持久化不可变 canonical transaction hash，并在每次签名/恢复前重新从 Intent 构造交易、重新白名单与完整参数校验、重新模拟；执行严格状态转移表；状态记录做权限隔离和完整性校验。绝不信任 JSON 中的 transaction 对象。

### CRITICAL-4：方法白名单不约束 recipient/token/fee/金额，允许资金导向攻击者

- 问题：只看 target 与 selector。官方 ABI 中 `mint`、`collect`、`exactInputSingle` 都含 recipient，但代码未绑定 signer/安全金库；swap 也未绑定唯一 token 对与 fee=500。
- 影响：一个仍处于“白名单方法”的 Intent 就能把 collect 资产、swap 输出或新 LP NFT 发给攻击者。
- 复现：攻击 A3；`src/okxlp/chain/whitelist.py:60-70`、`src/okxlp/uniswap/position.py:52-65,86-93`、`src/okxlp/uniswap/swap.py:133-149`。
- 建议修法：签名边界解码 ABI 并校验完整参数：recipient 必须是执行地址/Safe，token0/token1 必须是配置池两币，fee 必须 500，tokenId 必须是已对账且归属本地址的头寸，value/amount/deadline/tick/最低到账必须受硬上限约束。删除生产路径未用的 `increaseLiquidity` selector，除非有明确用例与相同参数策略。

### CRITICAL-5：RPC 可伪造模拟成功或替换 tx hash，系统仍推进 confirmed

- 问题：`eth_call.result` 无类型/hex 校验；广播返回 hash 不与本地 `keccak(raw)` 比对，receipt 也不核对交易内容。
- 影响：唯一 RPC 节点故障或恶意时，可让未模拟/未广播的阶段被认为成功，随后 collect/swap/mint 越过实际未完成的前置阶段，直接造成资金状态错乱。
- 复现：攻击 A5、A6b；`src/okxlp/chain/rpc.py:130-140`、`src/okxlp/exec/executor.py:89-100,124-173`。
- 建议修法：严格校验所有 RPC result schema；要求 returned hash 精确等于本地 hash；receipt 前再取 `eth_getTransactionByHash` 核对 from/to/input/value/nonce/chainId，并等待明确确认深度。至少配置两个独立运营方 RPC 并交叉核对关键读数。

### HIGH-1：高层再平衡不是幂等的，崩溃后重复已完成阶段

- 问题：`RebalanceJournal` 会保存进度，但 `execute` 每次先覆盖为空，从不加载已完成阶段；各次 action 又创建新随机 Intent ID。
- 影响：在 burn/collect/swap/mint 后任一崩溃窗口重启，都可能重复前序交易；最危险的是 swap 或 mint 已上链但进度/主状态尚未落盘。
- 复现：攻击 A7；`src/okxlp/strategy/rebalance.py:101-149`。
- 建议修法：以确定性 rebalance ID + stage ID 生成确定性 Intent ID；启动时先对账链上 position、余额、nonce、receipt，再从第一个未完成阶段继续。不得仅凭本地 stage JSON 决定重放。

### HIGH-2：上线必需的 M8/M9/M10 尚未实现，现有风控参数只是未接线配置

- 问题：启动/唤醒链上对账、心跳、HALT 每笔检查、每日次数、回撤熔断、所有 Intent 风控、Telegram 告警均标为未完成。
- 影响：Mac 睡眠/断网/重启后没有“以链上为准”的恢复闭环；`total_capital_usd: 0`、probe cap、每日 30 次、3% 回撤等配置不能阻止 executor 直接发交易。
- 复现：`openspec/changes/add-xlayer-lp-automation/tasks.md:57-75`；生产源码搜索 `max_rebalances_per_day|daily_loss_pct|consecutive_tx_failures` 无命中，仅配置存在于 `config/risk.yaml:9-29`；`TransactionExecutor(` 只在测试中实例化。
- 建议修法：把 M8/M9/M10 作为真实资金前置发布门，完成后重新做独立审计与真实链小额演练。上线入口必须无法在未完成 startup reconcile 时构造任何写链 Intent。

### HIGH-3：只有一个 RPC endpoint，全部失效时系统只能冻结暴露

- 问题：客户端支持故障转移，但生产配置仅 `https://rpc.xlayer.tech` 一个节点。
- 影响：节点故障不会 fail-open 发交易，但持仓出界、交易时段开始或熔断时也无法撤出，直接暴露价格风险。
- 复现：`config/pools.yaml:5-9`；攻击 A6。
- 建议修法：配置至少两个独立提供商，并对 chainId、block hash、slot0、nonce、receipt 做一致性/新鲜度检查；当读数分歧时冻结开仓并告警，恢复后先对账。

### MEDIUM-1：区间以整数 tick 而非实际池价为中心，上沿存在向内取整反例

- 问题：`build_price_band` 丢弃 `MarketSample.price`，只传整数 tick 给 `aligned_tick_range`。
- 影响：特定 tick 余数和 tick 内价格位置下，上沿比 ±0.5% 目标窄一个 tickSpacing（本池为约 0.1% 档位），增加不必要出界概率。
- 复现：攻击 A8；`src/okxlp/strategy/machine_types.py:48-57`。
- 建议修法：从同一 slot0 的 `sqrtPriceX96`/实际 Decimal price 直接算上下目标价格，再分别 floor/ceil 到 tickSpacing；添加所有 `current_tick % tickSpacing` 与 tick 内分数位置的性质测试，断言下沿价格不高于目标、上沿价格不低于目标。

### MEDIUM-2：锁定快照的 tick 与价格不是同一数学状态，现有测试未真正交叉断言

- 问题：`-201526` 对应 `1771.228546...`，`1770.77` 对应 floor tick `-201529`；相差 3 tick。测试只要求相对误差小于 5 bps。
- 影响：错误/异步快照仍可通过测试，掩盖 token 顺序、decimals、区块一致性或数据采样问题。
- 复现：R4；配置快照 `config/pools.yaml:38-43`，测试 `tests/test_tickmath.py:20-32`。
- 建议修法：锁定同一区块的完整 `sqrtPriceX96 + tick + token decimals`，断言 `price_to_tick(decoded_price) == slot0.tick`（允许的关系应严格符合 Uniswap TickMath），并把来源区块写入 fixture。

## 实际运行记录

### R1：全量测试

命令：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -B -m unittest discover -s tests -v
```

实际摘要（退出码 0）：

```text
---------------------------------------------------------------------
Ran 115 tests in 4.786s

OK
EXIT_CODE=0
```

其中与本审核直接相关的实际用例均为 `ok`：

```text
test_simulation_revert_is_persisted_and_aborts_signing ... Intent ... 模拟回滚，中止执行：execution reverted: 价格保护
ok
test_truthy_non_boolean_broadcast_permission_is_rejected ... ok
test_out_pending_confirmation_timer_survives_process_restart ... ok
test_out_pending_timeout_survives_process_restart ... ok
test_outside_before_confirmation_stays_pending ... ok
test_outside_at_confirmation_is_confirmed ... ok
test_returning_inside_before_confirmation_resets_timer_and_state ... ok
test_outside_at_timeout_is_confirmed_as_upper_bound ... ok
test_failure_stops_before_mint_and_persists_completed_stage ... ok
```

说明：现有全量测试没有覆盖 A1–A8 中复现的关键绕过，因此“115 tests OK”不能作为实盘安全结论。

### R2：官方 ABI + 独立 keccak selector 核对

ABI 来源：Uniswap 官方 [`INonfungiblePositionManager.sol`](https://github.com/Uniswap/v3-periphery/blob/main/contracts/interfaces/INonfungiblePositionManager.sol) 与官方 [`IV3SwapRouter.sol`](https://github.com/Uniswap/swap-router-contracts/blob/main/contracts/interfaces/IV3SwapRouter.sol)。计算使用独立脚本 `eth_utils.keccak(text=canonical_signature)[:4]`，未调用项目内 selector 常量。

```text
npm.mint | mint((address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)) | computed=0x88316456 | configured=0x88316456 | match=True
npm.increaseLiquidity | increaseLiquidity((uint256,uint256,uint256,uint256,uint256,uint256)) | computed=0x219f5d17 | configured=0x219f5d17 | match=True
npm.decreaseLiquidity | decreaseLiquidity((uint256,uint128,uint256,uint256,uint256)) | computed=0x0c49ccbe | configured=0x0c49ccbe | match=True
npm.collect | collect((uint256,address,uint128,uint128)) | computed=0xfc6f7865 | configured=0xfc6f7865 | match=True
npm.burn | burn(uint256) | computed=0x42966c68 | configured=0x42966c68 | match=True
swap_router02.exactInputSingle | exactInputSingle((address,address,uint24,address,uint256,uint256,uint160)) | computed=0x04e45aaf | configured=0x04e45aaf | match=True
EXIT_CODE=0
```

### R3：真实 X Layer 只读启动校验

命令（只读，无签名/广播）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -B -m okxlp.campaign.verifier
```

沙箱内首次运行因网络权限失败，退出码 2：

```text
RPC 调用 eth_chainId 失败，共尝试 3 次；最后错误：https://rpc.xlayer.tech: <urlopen error [Errno 1] Operation not permitted>
EXIT_CODE=2
```

获得只读网络权限后实际通过，退出码 0：

```text
WARNING 存在规模未校准事实，允许写链但限制仓位到探针上限：
- F2｜预算池间分配规则：尚未校准，仅允许探针仓位
- F3｜手续费口径 accrued/collected：尚未校准，仅允许探针仓位
INFO 链上校验通过：池=wASMLx_USDC，区块=68909456，模式=可请求实盘
EXIT_CODE=0
```

该校验只覆盖池/代币配置；它不证明 NPM/router 白名单参数安全，也未执行 `config/risk.yaml` 的 `mode: dry_run`。

### R4：已知快照独立 tick 计算

输入：tick=-201526、price=1770.77、token0=18、token1=6；公式 `price = 1.0001^tick × 10^(18-6)`。

```text
independent_price_at_tick=1771.228546313069639523611561741924512444806404942560704685560257521338099969350991567254920155247184
snapshot_price=1770.77 independent_raw_tick=-201528.5893248490594158944487276081427418249686165782795343227957847568220065987693798951988641984228 floor_tick=-201529 implementation_price_to_tick=-201529
independent_outward_from_snapshot=(-201580,-201470) implementation_from_current_tick=(-201580,-201470)
independent_boundary_prices=(1761.690165878320727556066187733090251768279067535366035843350296431360041026389098452603743283354970,1781.174752255614523074747969421731268046722765040184915980699535541187784693706802748274993085881285)
relative_widths_from_snapshot=(0.0051276191270911933474894041952990779331708423254482310840197787226121737851956502241376670694923847,0.005875834950679378504688903370698209279987104502665459647893027067991768944417853672851354544001358)
EXIT_CODE=0
```

极值往返补充：

```text
tick=-887272 roundtrip_tick=-887272 tick_match=True sqrt_floor_price_le_original=True
tick=-1 roundtrip_tick=-1 tick_match=True sqrt_floor_price_le_original=True
tick=0 roundtrip_tick=0 tick_match=True sqrt_floor_price_le_original=True
tick=1 roundtrip_tick=1 tick_match=True sqrt_floor_price_le_original=True
tick=887272 roundtrip_tick=887272 tick_match=True sqrt_floor_price_le_original=True
EXIT_CODE=0
```

### R5：范围与未实现项搜索

实际搜索结果：

```text
tools/dry_run_m7.py:37:class DryRunRiskGate:
config/risk.yaml:14:  max_rebalances_per_day: 30
config/risk.yaml:26:  daily_loss_pct: 0.03
config/risk.yaml:27:  consecutive_tx_failures: 3
tests/test_executor.py:97:        return TransactionExecutor(
tests/test_m5_dry_run.py:77:                executor = TransactionExecutor(
```

生产源码没有 `TransactionExecutor(` 实例化，也没有每日次数/回撤/连续失败限制实现。`mode` 搜索结果仅命中配置、测试、Intent 状态与 verifier 文案，未命中执行门控。

---

最终发布判定：**FAIL；在 CRITICAL-1 至 CRITICAL-5、HIGH-1 至 HIGH-3 修复并重新独立审核前，不得连接持有真实资金的签名地址。**
