import json
import hashlib
import logging
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from eth_abi import encode
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.chain.calldata_policy import CalldataPolicy, CalldataPolicyError
from okxlp.chain.gas import GasQuote
from eth_utils import keccak

from okxlp.chain.rpc import JsonRpcClient, RpcError
from okxlp.chain.whitelist import TransactionWhitelist, WhitelistError
from okxlp.exec.authorization import RunMode
from okxlp.exec.executor import ExecutionError, TransactionExecutor
from okxlp.exec.intent import (
    Intent,
    IntentIntegrityError,
    IntentStatus,
    IntentStore,
    IntentStoreError,
)


TARGET = "0x" + "12" * 20
SENDER = "0x" + "34" * 20
NPM = "0x315e413a11ab0df498ef83873012430ca36638ae"
TOKEN0 = "0x9147b03c16b18fc4f686f610f189f91ddf4347b4"
TOKEN1 = "0xb6ceceab302e2e4948951ee7843fc24e92933061"
ATTACKER = "0x9999999999999999999999999999999999999999"
NOW = 2_000_000_000


def mint_calldata(recipient=SENDER):
    abi_type = "(address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)"
    values = (
        TOKEN0, TOKEN1, 500, -201600, -201500, 10, 20, 9, 18,
        recipient, NOW + 600,
    )
    return "0x88316456" + encode([abi_type], [values]).hex()


def collect_calldata(recipient):
    values = (15857, recipient, 2**128 - 1, 2**128 - 1)
    return "0xfc6f7865" + encode(
        ["(uint256,address,uint128,uint128)"], [values]
    ).hex()


class RecordingWhitelist:
    def __init__(self, events, allowed=True):
        self.events = events
        self.allowed = allowed

    def validate(self, target, calldata):
        self.events.append("白名单")
        if not self.allowed:
            raise WhitelistError("目标地址不在白名单")
        return calldata[:10]


class RecordingCalldataPolicy:
    def __init__(self, events, allowed=True):
        self.events = events
        self.allowed = allowed

    def validate(self, *, target, calldata, value, now_ts):
        self.events.append("参数策略")
        if not self.allowed:
            raise CalldataPolicyError(
                f"recipient 不合规：期望值={SENDER}，实际值={ATTACKER}"
            )


class RecordingRpc:
    def __init__(self, events, *, revert=None, receipt=None, returned_hash=None):
        self.events = events
        self.revert = revert
        self.receipt = receipt or {"status": "0x1"}
        self.returned_hash = returned_hash
        self.broadcasts = []

    def call(self, method, _params):
        self.events.append(method)
        if method == "eth_call":
            if self.revert:
                raise RpcError(f"execution reverted: {self.revert}")
            return "0x"
        if method == "eth_getTransactionReceipt":
            return self.receipt
        raise AssertionError(f"未预期 RPC：{method}")

    def send_raw_transaction(self, raw, *, allow_broadcast=False):
        self.events.append("eth_sendRawTransaction")
        self.broadcasts.append((raw, allow_broadcast))
        return self.returned_hash or "0x" + keccak(raw).hex()


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class RecordingGas:
    def __init__(self, events):
        self.events = events

    def estimate(self, _transaction):
        self.events.append("gas")
        return GasQuote(120_000, 21_000_000, 1_000_000)


class RecordingNonce:
    def __init__(self, events):
        self.events = events

    def reserve(self):
        self.events.append("nonce")
        return 7


class RecordingSigner:
    address = SENDER

    def __init__(self, events):
        self.events = events
        self.calls = 0

    def sign_transaction(self, _transaction):
        self.events.append("签名")
        self.calls += 1
        return b"\x02\x01"


class TransactionExecutorTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.events = []
        self.output = []
        self.rpc = RecordingRpc(self.events)
        self.signer = RecordingSigner(self.events)
        self.store = IntentStore(Path(self.directory.name))

    def _executor(self, whitelist=None, calldata_policy=None):
        return TransactionExecutor(
            rpc=self.rpc,
            signer=self.signer,
            nonce_manager=RecordingNonce(self.events),
            gas_estimator=RecordingGas(self.events),
            whitelist=whitelist or RecordingWhitelist(self.events),
            calldata_policy=calldata_policy or RecordingCalldataPolicy(self.events),
            store=self.store,
            chain_id=196,
            clock=lambda: NOW,
            printer=self.output.append,
            sleep=lambda _delay: None,
        )

    @staticmethod
    def _transaction(intent, **overrides):
        transaction = {
            "chainId": 196,
            "nonce": 7,
            "to": TARGET,
            "data": intent.calldata,
            "value": intent.value,
            "gas": 120_000,
            "maxFeePerGas": 21_000_000,
            "maxPriorityFeePerGas": 1_000_000,
            "type": 2,
        }
        transaction.update(overrides)
        return transaction

    def _signed(self, intent, **transaction_overrides):
        persisted = self.store.persist(intent)
        simulated = self.store.save(
            replace(persisted, status=IntentStatus.SIMULATED)
        )
        return self.store.save(
            replace(
                simulated, status=IntentStatus.SIGNED,
                transaction=self._transaction(intent, **transaction_overrides),
                nonce=7, tx_hash="0x" + "00" * 32,
            )
        )

    @staticmethod
    def _real_policy():
        return CalldataPolicy.from_config(
            Path("config/execution.yaml"), Path("config/pools.yaml"),
            executor_address=SENDER, allowed_token_ids={15857},
        )

    def _prepare_signed_mint(self):
        intent = Intent.create(NPM, mint_calldata())
        executor = self._executor(
            TransactionWhitelist.from_config(), self._real_policy()
        )
        with (
            patch.object(
                executor, "_finish_signed",
                side_effect=RuntimeError("模拟签名后进程崩溃"),
            ),
            self.assertRaisesRegex(RuntimeError, "进程崩溃"),
        ):
            executor.execute(intent, allow_broadcast=True)
        self.assertEqual(self.store.load(intent.intent_id).status, IntentStatus.SIGNED)
        self.events.clear()
        self.rpc.broadcasts.clear()
        self.signer = RecordingSigner(self.events)
        self.store = IntentStore(Path(self.directory.name))
        return intent

    def _tamper_transaction(self, intent, *, recompute_hash):
        path = Path(self.directory.name) / f"{intent.intent_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["transaction"]["to"] = ATTACKER
        data["transaction"]["value"] = 123456789
        if recompute_hash:
            payload = dict(data)
            payload.pop("content_hash")
            canonical = json.dumps(
                payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            )
            data["content_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_rejects_non_whitelisted_intent_before_persist_or_sign(self):
        intent = Intent.create(TARGET, "0x88316456")

        with self.assertLogs("okxlp.exec.executor", logging.INFO) as logs:
            with self.assertRaises(WhitelistError):
                self._executor(RecordingWhitelist(self.events, allowed=False)).execute(intent)

        self.assertEqual(self.signer.calls, 0)
        self.assertEqual(list(Path(self.directory.name).glob("*.json")), [])
        self.assertNotIn("eth_call", self.events)
        self.assertIn("白名单拒绝", "\n".join(logs.output))

    def test_rejects_parameter_policy_before_persist_or_sign(self):
        intent = Intent.create(TARGET, "0x88316456")

        with self.assertRaisesRegex(CalldataPolicyError, "recipient"):
            self._executor(
                calldata_policy=RecordingCalldataPolicy(self.events, allowed=False)
            ).execute(intent)

        self.assertEqual(self.events, ["白名单", "参数策略"])
        self.assertEqual(self.signer.calls, 0)
        self.assertEqual(self.rpc.broadcasts, [])
        self.assertEqual(list(Path(self.directory.name).glob("*.json")), [])

    def test_parameter_policy_failure_marks_existing_persisted_intent_failed(self):
        intent = Intent.create(TARGET, "0x88316456")
        self.store.persist(intent)

        with self.assertRaises(CalldataPolicyError):
            self._executor(
                calldata_policy=RecordingCalldataPolicy(self.events, allowed=False)
            ).execute(intent, allow_broadcast=True)

        self.assertEqual(self.store.load(intent.intent_id).status, IntentStatus.FAILED)
        self.assertEqual(self.signer.calls, 0)
        self.assertEqual(self.rpc.broadcasts, [])

    def test_parameter_policy_failure_marks_existing_signed_intent_failed(self):
        intent = Intent.create(TARGET, "0x88316456")
        self._signed(intent)
        self.events.clear()

        with self.assertRaises(CalldataPolicyError):
            self._executor(
                calldata_policy=RecordingCalldataPolicy(self.events, allowed=False)
            ).execute(intent, allow_broadcast=True)

        self.assertEqual(self.store.load(intent.intent_id).status, IntentStatus.FAILED)
        self.assertEqual(self.signer.calls, 0)
        self.assertEqual(self.rpc.broadcasts, [])

    def test_validation_failure_cannot_fail_different_intent_with_same_id(self):
        intent = Intent.create(TARGET, "0x88316456")
        self.store.persist(intent)
        conflicting = replace(intent, target=ATTACKER)

        with self.assertRaises(CalldataPolicyError):
            self._executor(
                calldata_policy=RecordingCalldataPolicy(self.events, allowed=False)
            ).execute(conflicting, allow_broadcast=True)

        stored = self.store.load(intent.intent_id)
        self.assertEqual(stored.status, IntentStatus.PERSISTED)
        self.assertEqual(stored.target, TARGET)
        self.assertEqual(self.signer.calls, 0)
        self.assertEqual(self.rpc.broadcasts, [])

    def test_a3_attacker_collect_is_rejected_without_broadcast(self):
        intent = Intent.create(NPM, collect_calldata(ATTACKER))

        with self.assertRaisesRegex(CalldataPolicyError, "recipient"):
            self._executor(
                TransactionWhitelist.from_config(), self._real_policy()
            ).execute(intent, allow_broadcast=True)

        self.assertEqual(self.signer.calls, 0)
        self.assertEqual(self.rpc.broadcasts, [])
        self.assertNotIn("eth_sendRawTransaction", self.events)

    def test_simulation_revert_is_persisted_and_aborts_signing(self):
        self.rpc.revert = "价格保护"
        intent = Intent.create(TARGET, "0x88316456")

        with self.assertRaisesRegex(ExecutionError, "价格保护"):
            self._executor().execute(intent)

        stored = self.store.load(intent.intent_id)
        self.assertEqual(stored.status, IntentStatus.FAILED)
        self.assertIn("价格保护", stored.error)
        self.assertEqual(self.signer.calls, 0)
        self.assertEqual(self.rpc.broadcasts, [])

    def test_default_dry_run_signs_prints_full_tx_and_never_broadcasts(self):
        intent = Intent.create(TARGET, "0x88316456" + "00" * 32, value=9)

        with self.assertLogs("okxlp.exec.executor", logging.INFO) as logs:
            result = self._executor().execute(intent)

        self.assertEqual(result.intent.status, IntentStatus.DRY_RUN)
        self.assertEqual(self.signer.calls, 1)
        self.assertEqual(self.rpc.broadcasts, [])
        self.assertEqual(
            self.events,
            ["白名单", "参数策略", "eth_call", "gas", "nonce", "签名"],
        )
        self.assertIn('"chainId": 196', self.output[0])
        self.assertIn('"nonce": 7', self.output[0])
        joined = "\n".join(logs.output)
        for step in ("白名单", "落盘", "模拟", "gas", "nonce", "签名", "dry-run"):
            self.assertIn(step, joined)

    def test_explicit_broadcast_path_waits_for_successful_receipt(self):
        intent = Intent.create(TARGET, "0x88316456")

        result = self._executor().execute(intent, allow_broadcast=True)

        self.assertEqual(result.intent.status, IntentStatus.CONFIRMED)
        self.assertEqual(len(self.rpc.broadcasts), 1)
        self.assertTrue(self.rpc.broadcasts[0][1])
        self.assertIn("eth_getTransactionReceipt", self.events)

    def test_non_boolean_broadcast_permissions_are_rejected_at_execute_entry(self):
        for value in (1, "true", object()):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    self._executor().execute(
                        Intent.create(TARGET, "0x88316456"), allow_broadcast=value
                    )

        self.assertNotIn("eth_sendRawTransaction", self.events)
        self.assertEqual(self.rpc.broadcasts, [])

    def test_non_boolean_broadcast_permissions_are_rejected_at_signed_boundary(self):
        intent = Intent.create(TARGET, "0x88316456")
        signed = self._signed(intent)

        for value in (1, "true", object()):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    self._executor()._finish_signed(signed, value)

        self.assertNotIn("eth_sendRawTransaction", self.events)

    def test_malformed_eth_call_fails_intent_without_broadcast(self):
        for malformed in ({"malformed": True}, 123, "nothex", "0xzz", None):
            with self.subTest(result=malformed), tempfile.TemporaryDirectory() as directory:
                methods = []

                def opener(request, **_kwargs):
                    body = json.loads(request.data)
                    methods.append(body["method"])
                    result = "0xc4" if body["method"] == "eth_chainId" else malformed
                    return FakeResponse(
                        {"jsonrpc": "2.0", "id": body["id"], "result": result}
                    )

                rpc = JsonRpcClient(
                    ["https://fake.invalid"], retries=0, urlopen=opener,
                    run_mode=RunMode.LIVE,
                )
                store = IntentStore(Path(directory))
                executor = TransactionExecutor(
                    rpc=rpc, signer=RecordingSigner([]),
                    nonce_manager=RecordingNonce([]), gas_estimator=RecordingGas([]),
                    whitelist=RecordingWhitelist([]),
                    calldata_policy=RecordingCalldataPolicy([]),
                    store=store, chain_id=196,
                )
                intent = Intent.create(TARGET, "0x88316456")

                with self.assertRaisesRegex(ExecutionError, "result 格式非法"):
                    executor.execute(intent, allow_broadcast=True)

                self.assertEqual(store.load(intent.intent_id).status, IntentStatus.FAILED)
                self.assertNotIn("eth_sendRawTransaction", methods)

    def test_mismatched_returned_hash_fails_without_waiting_for_receipt(self):
        self.rpc.returned_hash = "0x" + "ab" * 32
        intent = Intent.create(TARGET, "0x88316456")

        with self.assertRaisesRegex(ExecutionError, "本地签名不一致"):
            self._executor().execute(intent, allow_broadcast=True)

        stored = self.store.load(intent.intent_id)
        self.assertEqual(stored.status, IntentStatus.FAILED)
        self.assertIn("本地=", stored.error)
        self.assertIn("节点=", stored.error)
        self.assertNotIn("eth_getTransactionReceipt", self.events)

    def test_signed_recovery_recomputes_expected_hash_before_comparison(self):
        stale_hash = "0x" + "ab" * 32
        self.rpc.returned_hash = stale_hash
        intent = Intent.create(TARGET, "0x88316456")
        signed = self._signed(intent)
        self.store = IntentStore(Path(self.directory.name))

        with self.assertRaisesRegex(ExecutionError, "本地签名不一致"):
            self._executor().execute(intent, allow_broadcast=True)

        self.assertEqual(self.store.load(intent.intent_id).status, IntentStatus.FAILED)
        self.assertNotIn("eth_getTransactionReceipt", self.events)

    def test_signed_recovery_revalidates_and_resimulates_before_signing(self):
        intent = Intent.create(TARGET, "0x88316456")
        self._signed(intent)
        self.events.clear()

        result = self._executor().execute(intent)

        self.assertEqual(result.intent.status, IntentStatus.DRY_RUN)
        self.assertEqual(
            self.events,
            [
                "白名单", "参数策略", "白名单", "参数策略",
                "eth_call", "签名",
            ],
        )
        self.assertEqual(self.rpc.broadcasts, [])

    def test_signed_recovery_without_transaction_fails_before_simulation(self):
        intent = Intent.create(TARGET, "0x88316456")
        persisted = self.store.persist(intent)
        simulated = self.store.save(
            replace(persisted, status=IntentStatus.SIMULATED)
        )
        self.store.save(replace(simulated, status=IntentStatus.SIGNED))
        self.events.clear()

        with self.assertRaisesRegex(
            ExecutionError, "持久化交易与 Intent 不一致，已中止"
        ):
            self._executor().execute(intent, allow_broadcast=True)

        self.assertNotIn("eth_call", self.events)
        self.assertEqual(self.signer.calls, 0)
        self.assertEqual(self.rpc.broadcasts, [])
        self.assertEqual(self.store.load(intent.intent_id).status, IntentStatus.FAILED)

    def test_simulated_recovery_does_not_attempt_same_status_save(self):
        intent = Intent.create(TARGET, "0x88316456")
        persisted = self.store.persist(intent)
        self.store.save(replace(persisted, status=IntentStatus.SIMULATED))
        self.events.clear()

        result = self._executor().execute(intent)

        self.assertEqual(result.intent.status, IntentStatus.DRY_RUN)
        self.assertEqual(self.events.count("eth_call"), 1)
        self.assertEqual(self.rpc.broadcasts, [])

    def test_fresh_transaction_is_checked_before_signing(self):
        intent = Intent.create(TARGET, "0x88316456")
        executor = self._executor()
        original = executor._assert_transaction_matches_intent

        def record_check(current):
            self.events.append("交易一致性")
            return original(current)

        with patch.object(
            executor, "_assert_transaction_matches_intent",
            side_effect=record_check,
        ):
            result = executor.execute(intent)

        self.assertEqual(result.intent.status, IntentStatus.DRY_RUN)
        self.assertLess(self.events.index("交易一致性"), self.events.index("签名"))

    def test_fresh_transaction_mismatch_preserves_required_failure(self):
        class InvalidGas:
            @staticmethod
            def estimate(_transaction):
                return GasQuote(True, 21_000_000, 1_000_000)

        intent = Intent.create(TARGET, "0x88316456")
        executor = self._executor()
        executor.gas_estimator = InvalidGas()

        with self.assertRaisesRegex(
            ExecutionError, "持久化交易与 Intent 不一致，已中止"
        ):
            executor.execute(intent, allow_broadcast=True)

        self.assertEqual(self.store.load(intent.intent_id).status, IntentStatus.FAILED)
        self.assertEqual(self.signer.calls, 0)
        self.assertEqual(self.rpc.broadcasts, [])

    def test_signed_recovery_simulation_failure_marks_failed(self):
        intent = Intent.create(TARGET, "0x88316456")
        self._signed(intent)
        self.events.clear()
        self.rpc.revert = "恢复模拟失败"

        with self.assertRaisesRegex(ExecutionError, "恢复模拟失败"):
            self._executor().execute(intent, allow_broadcast=True)

        self.assertEqual(self.store.load(intent.intent_id).status, IntentStatus.FAILED)
        self.assertEqual(self.signer.calls, 0)
        self.assertEqual(self.rpc.broadcasts, [])

    def test_signed_transaction_rejects_non_integer_numeric_field(self):
        intent = Intent.create(TARGET, "0x88316456")
        signed = self._signed(intent, gas=True)

        with self.assertRaisesRegex(
            ExecutionError, "持久化交易与 Intent 不一致，已中止"
        ):
            self._executor()._finish_signed(signed, True)

        self.assertEqual(self.store.load(intent.intent_id).status, IntentStatus.FAILED)
        self.assertEqual(self.signer.calls, 0)
        self.assertEqual(self.rpc.broadcasts, [])

    def test_a4_recomputed_hash_tamper_marks_failed_without_broadcast(self):
        intent = self._prepare_signed_mint()
        self._tamper_transaction(intent, recompute_hash=True)

        with self.assertRaisesRegex(
            ExecutionError, "持久化交易与 Intent 不一致，已中止"
        ):
            self._executor(
                TransactionWhitelist.from_config(), self._real_policy()
            ).execute(intent, allow_broadcast=True)

        self.assertEqual(self.store.load(intent.intent_id).status, IntentStatus.FAILED)
        self.assertEqual(self.signer.calls, 0)
        self.assertEqual(self.rpc.broadcasts, [])

    def test_a4_stale_hash_tamper_marks_failed_without_broadcast(self):
        intent = self._prepare_signed_mint()
        self._tamper_transaction(intent, recompute_hash=False)

        with self.assertRaisesRegex(
            IntentStoreError, "Intent 落盘内容完整性校验失败"
        ):
            self._executor(
                TransactionWhitelist.from_config(), self._real_policy()
            ).execute(intent, allow_broadcast=True)

        self.assertEqual(self.store.load(intent.intent_id).status, IntentStatus.FAILED)
        self.assertEqual(self.signer.calls, 0)
        self.assertEqual(self.rpc.broadcasts, [])

    def test_corrupted_sent_record_is_quarantined_without_losing_raw_bytes(self):
        intent = Intent.create(TARGET, "0x88316456")
        signed = self._signed(intent)
        self.store.save(
            replace(
                signed, status=IntentStatus.SENT,
                tx_hash="0x" + "ab" * 32,
            )
        )
        path = Path(self.directory.name) / f"{intent.intent_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["transaction"]["value"] = 1
        path.write_text(json.dumps(data), encoding="utf-8")
        corrupted_bytes = path.read_bytes()

        with self.assertRaises(IntentIntegrityError):
            self._executor().execute(intent, allow_broadcast=True)

        quarantined = list(
            Path(self.directory.name).glob(
                f"{intent.intent_id}.corrupt-*.json"
            )
        )
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), corrupted_bytes)
        marker = self.store.load(intent.intent_id)
        self.assertEqual(marker.status, IntentStatus.FAILED)
        self.assertIn(quarantined[0].name, marker.error)
        self.assertEqual(self.store.load_pending(), ())
        self.assertEqual(self.signer.calls, 0)
        self.assertEqual(self.rpc.broadcasts, [])


if __name__ == "__main__":
    unittest.main()
