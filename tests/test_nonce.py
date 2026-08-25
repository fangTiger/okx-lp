import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.chain.nonce import NonceManager


ADDRESS = "0x" + "12" * 20


class FakeRpc:
    def __init__(self, pending):
        self.pending = iter(pending)
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return hex(next(self.pending))


class NonceManagerTest(unittest.TestCase):
    def test_reconciles_pending_nonce_then_increments_locally(self):
        rpc = FakeRpc([5, 5, 8])
        manager = NonceManager(rpc, ADDRESS)

        self.assertEqual([manager.reserve(), manager.reserve(), manager.reserve()], [5, 6, 8])
        self.assertEqual(
            rpc.calls,
            [("eth_getTransactionCount", [ADDRESS, "pending"])] * 3,
        )

    def test_new_instance_uses_chain_pending_state(self):
        first = NonceManager(FakeRpc([3]), ADDRESS)
        self.assertEqual(first.reserve(), 3)

        restarted = NonceManager(FakeRpc([12]), ADDRESS)
        self.assertEqual(restarted.reserve(), 12)

    def test_explicit_sync_resets_local_state_to_chain(self):
        rpc = FakeRpc([4, 4, 9])
        manager = NonceManager(rpc, ADDRESS)
        self.assertEqual(manager.reserve(), 4)
        self.assertEqual(manager.reserve(), 5)

        self.assertEqual(manager.sync(), 9)
        self.assertEqual(manager.next_nonce, 9)


if __name__ == "__main__":
    unittest.main()
