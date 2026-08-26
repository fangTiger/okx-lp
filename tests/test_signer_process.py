import inspect
import json
import os
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eth_abi import encode
from eth_account import Account

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.chain.calldata_policy import CalldataPolicy
from okxlp.chain.signer_process import RemoteSigner, RemoteSignerError


CHAIN_ID = 196
NPM = "0x315e413a11ab0df498ef83873012430ca36638ae"
ROUTER = "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca"
TOKEN0 = "0x9147b03c16b18fc4f686f610f189f91ddf4347b4"
TOKEN1 = "0xb6ceceab302e2e4948951ee7843fc24e92933061"
ATTACKER = "0x9999999999999999999999999999999999999999"
TOKEN_ID = 15857


def collect_calldata(recipient: str) -> str:
    """构造参数完整的 collect calldata。"""
    values = (TOKEN_ID, recipient, 2**128 - 1, 2**128 - 1)
    return "0xfc6f7865" + encode(
        ["(uint256,address,uint128,uint128)"], [values]
    ).hex()


def mark_unpickle_execution(path: str) -> str:
    """攻击回归仅写临时标记，不读取或输出任何密钥。"""
    Path(path).write_text("executed", encoding="utf-8")
    return "executed"


class MaliciousPicklePayload:
    """用于确认子进程不会反序列化主进程对象。"""

    def __init__(self, marker: Path) -> None:
        self.marker = marker

    def __reduce__(self):
        return mark_unpickle_execution, (str(self.marker),)


class RemoteSignerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.account = Account.create()
        self.password = secrets.token_urlsafe(24)
        self.env_name = "OKXLP_REMOTE_SIGNER_TEST_PASSWORD"
        self.keystore = Path(self.directory.name) / "keystore.json"
        self.keystore.write_text(
            json.dumps(Account.encrypt(self.account.key, self.password)),
            encoding="utf-8",
        )
        self.execution_path = Path("config/execution.yaml").resolve()

    def _policy(self) -> CalldataPolicy:
        return CalldataPolicy(
            executor_address=self.account.address,
            npm_address=NPM,
            router_address=ROUTER,
            token0=TOKEN0,
            token1=TOKEN1,
            fee=500,
            allowed_token_ids=frozenset({TOKEN_ID}),
        )

    def _signer(self) -> RemoteSigner:
        with patch.dict(
            os.environ, {self.env_name: self.password}, clear=False
        ):
            signer = RemoteSigner(
                keystore_path=self.keystore,
                password_env=self.env_name,
                chain_id=CHAIN_ID,
                execution_path=self.execution_path,
                calldata_policy=self._policy(),
            )
        self.addCleanup(signer.close)
        return signer

    def _transaction(self, *, recipient=None, to=NPM, chain_id=CHAIN_ID):
        return {
            "chainId": chain_id,
            "nonce": 0,
            "to": to,
            "data": collect_calldata(recipient or self.account.address),
            "value": 0,
            "gas": 120_000,
            "maxFeePerGas": 20_000_000,
            "maxPriorityFeePerGas": 1_000_000,
            "type": 2,
        }

    def test_signs_legal_collect_and_recovers_temporary_address(self):
        signer = self._signer()

        raw = signer.sign_transaction(self._transaction())

        self.assertEqual(signer.address, self.account.address)
        self.assertEqual(Account.recover_transaction(raw), self.account.address)

    def test_parent_attributes_and_function_closures_do_not_hold_private_key(self):
        signer = self._signer()
        values = list(getattr(signer, "__dict__", {}).values())
        for current_class in type(signer).__mro__:
            slots = getattr(current_class, "__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for name in slots:
                if hasattr(signer, name):
                    values.append(getattr(signer, name))

        for value in values:
            self.assertNotEqual(value, self.account.key)
            if inspect.isfunction(value) and value.__closure__:
                for cell in value.__closure__:
                    self.assertNotEqual(cell.cell_contents, self.account.key)

    def test_child_policy_rejects_target_chain_and_collect_recipient(self):
        signer = self._signer()
        cases = (
            self._transaction(to=ATTACKER),
            self._transaction(chain_id=CHAIN_ID + 1),
            self._transaction(recipient=ATTACKER),
        )

        for transaction in cases:
            with self.subTest(transaction=transaction):
                with self.assertRaises(RemoteSignerError):
                    signer.sign_transaction(transaction)

    def test_ipc_rejects_pickle_payload_before_it_reaches_child(self):
        signer = self._signer()
        marker = Path(self.directory.name) / "pickle-executed"
        transaction = self._transaction()
        transaction["unexpected"] = MaliciousPicklePayload(marker)

        with self.assertRaises(RemoteSignerError):
            signer.sign_transaction(transaction)

        self.assertFalse(marker.exists())

    def test_missing_password_environment_is_reported_without_secret(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                RemoteSignerError, "环境变量"
            ) as caught:
                RemoteSigner(
                    keystore_path=self.keystore,
                    password_env=self.env_name,
                    chain_id=CHAIN_ID,
                    execution_path=self.execution_path,
                    calldata_policy=self._policy(),
                )

        self.assertNotIn(self.password, str(caught.exception))
        self.assertNotIn(self.account.key.hex(), str(caught.exception))

    def test_sign_after_close_is_rejected(self):
        signer = self._signer()
        signer.close()

        with self.assertRaisesRegex(RemoteSignerError, "已关闭"):
            signer.sign_transaction(self._transaction())

    def test_broken_pipe_marks_signer_closed_and_reclaims_child(self):
        class BrokenConnection:
            closed = False

            @staticmethod
            def send(_message):
                raise BrokenPipeError

            def close(self):
                self.closed = True

        signer = self._signer()
        signer._connection.close()
        broken = BrokenConnection()
        signer._connection = broken

        with self.assertRaisesRegex(RemoteSignerError, "子进程"):
            signer.sign_transaction(self._transaction())

        self.assertTrue(signer._closed)
        self.assertTrue(broken.closed)
        self.assertFalse(signer._process.is_alive())


if __name__ == "__main__":
    unittest.main()
