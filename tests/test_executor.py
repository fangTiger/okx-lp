import json
import logging
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.chain.gas import GasQuote
from eth_utils import keccak

from okxlp.chain.rpc import JsonRpcClient, RpcError
from okxlp.chain.whitelist import WhitelistError
from okxlp.exec.authorization import RunMode
from okxlp.exec.executor import ExecutionError, TransactionExecutor
from okxlp.exec.intent import Intent, IntentStatus, IntentStore


TARGET = "0x" + "12" * 20
SENDER = "0x" + "34" * 20


class RecordingWhitelist:
    def __init__(self, events, allowed=True):
        self.events = events
        self.allowed = allowed

    def validate(self, target, calldata):
        self.events.append("白名单")
        if not self.allowed:
            raise WhitelistError("目标地址不在白名单")
        return calldata[:10]


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

    def _executor(self, whitelist=None):
        return TransactionExecutor(
            rpc=self.rpc,
            signer=self.signer,
            nonce_manager=RecordingNonce(self.events),
            gas_estimator=RecordingGas(self.events),
            whitelist=whitelist or RecordingWhitelist(self.events),
            store=self.store,
            chain_id=196,
            printer=self.output.append,
            sleep=lambda _delay: None,
        )

    def test_rejects_non_whitelisted_intent_before_persist_or_sign(self):
        intent = Intent.create(TARGET, "0x88316456")

        with self.assertLogs("okxlp.exec.executor", logging.INFO) as logs:
            with self.assertRaises(WhitelistError):
                self._executor(RecordingWhitelist(self.events, allowed=False)).execute(intent)

        self.assertEqual(self.signer.calls, 0)
        self.assertEqual(list(Path(self.directory.name).glob("*.json")), [])
        self.assertNotIn("eth_call", self.events)
        self.assertIn("白名单拒绝", "\n".join(logs.output))

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
            ["白名单", "eth_call", "gas", "nonce", "签名"],
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
        persisted = self.store.persist(intent)
        signed = self.store.save(
            replace(
                persisted, status=IntentStatus.SIGNED,
                transaction={"chainId": 196}, tx_hash="0x" + "00" * 32,
            )
        )

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
                    whitelist=RecordingWhitelist([]), store=store, chain_id=196,
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
        persisted = self.store.persist(intent)
        self.store.save(
            replace(
                persisted, status=IntentStatus.SIGNED,
                transaction={"chainId": 196}, tx_hash=stale_hash,
            )
        )

        with self.assertRaisesRegex(ExecutionError, "本地签名不一致"):
            self._executor().execute(intent, allow_broadcast=True)

        self.assertEqual(self.store.load(intent.intent_id).status, IntentStatus.FAILED)
        self.assertNotIn("eth_getTransactionReceipt", self.events)


if __name__ == "__main__":
    unittest.main()
