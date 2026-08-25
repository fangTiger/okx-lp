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
from eth_utils import to_checksum_address

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.chain.gas import GasEstimator, load_gas_policy
from okxlp.chain.nonce import NonceManager
from okxlp.chain.signer import KeystoreSigner
from okxlp.chain.whitelist import TransactionWhitelist
from okxlp.exec.executor import TransactionExecutor
from okxlp.exec.intent import Intent, IntentStatus, IntentStore


NPM = "0x315e413a11ab0df498ef83873012430ca36638ae"
WASMLX = "0x9147b03c16b18fc4f686f610f189f91ddf4347b4"
USDC = "0xb6ceceab302e2e4948951ee7843fc24e92933061"


class MintSimulationRpc:
    def __init__(self):
        self.calls = []
        self.broadcasts = 0

    def call(self, method, params):
        self.calls.append((method, params))
        values = {
            "eth_call": "0x",
            "eth_estimateGas": hex(420_000),
            "eth_getBlockByNumber": {"baseFeePerGas": hex(10_000_000)},
            "eth_maxPriorityFeePerGas": hex(1_000_000),
            "eth_getTransactionCount": "0x0",
        }
        return values[method]

    def send_raw_transaction(self, _raw, *, allow_broadcast=False):
        self.broadcasts += 1
        raise AssertionError(f"dry-run 不得广播：{allow_broadcast}")


def mint_calldata(recipient):
    types = (
        "address", "address", "uint24", "int24", "int24", "uint256",
        "uint256", "uint256", "uint256", "address", "uint256",
    )
    values = (
        WASMLX, USDC, 500, -201_600, -201_500, 10**15,
        1_000_000, 0, 0, recipient, 2_000_000_000,
    )
    return "0x88316456" + encode(types, values).hex()


class M5MintDryRunTest(unittest.TestCase):
    def test_temporary_keystore_completes_mint_dry_run_without_broadcast(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            account = Account.create()
            password = secrets.token_urlsafe(24)
            env_name = "OKXLP_M5_TEST_PASSWORD"
            keystore = root / "keystore.json"
            keystore.write_text(
                json.dumps(Account.encrypt(account.key, password)), encoding="utf-8"
            )
            rpc = MintSimulationRpc()
            output = []
            with patch.dict(os.environ, {env_name: password}, clear=False):
                signer = KeystoreSigner(keystore, password_env=env_name)
                executor = TransactionExecutor(
                    rpc=rpc, signer=signer,
                    nonce_manager=NonceManager(rpc, signer.address),
                    gas_estimator=GasEstimator(rpc, load_gas_policy()),
                    whitelist=TransactionWhitelist.from_config(),
                    store=IntentStore(root / "intents"), chain_id=196,
                    printer=output.append,
                )
                result = executor.execute(Intent.create(NPM, mint_calldata(signer.address)))

            self.assertEqual(result.intent.status, IntentStatus.DRY_RUN)
            self.assertEqual(rpc.broadcasts, 0)
            self.assertEqual(rpc.calls[0][0], "eth_call")
            self.assertEqual(rpc.calls[0][1][0]["to"], NPM)
            self.assertTrue(rpc.calls[0][1][0]["data"].startswith("0x88316456"))
            self.assertIn(f'"to": "{to_checksum_address(NPM)}"', output[0])


if __name__ == "__main__":
    unittest.main()
