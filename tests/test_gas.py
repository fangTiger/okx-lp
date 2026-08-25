import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.chain.gas import GasError, GasEstimator, GasPolicy, load_gas_policy


class FakeRpc:
    def __init__(self, *, estimate=100_000, base=10_000_000, priority=1_000_000):
        self.values = {
            "eth_estimateGas": hex(estimate),
            "eth_getBlockByNumber": {"baseFeePerGas": hex(base)},
            "eth_maxPriorityFeePerGas": hex(priority),
        }

    def call(self, method, _params):
        return self.values[method]


def policy():
    return GasPolicy(
        gas_limit_multiplier=Decimal("1.2"),
        min_gas_limit=21_000,
        max_gas_limit=1_500_000,
        base_fee_multiplier=Decimal("2"),
        min_max_fee_per_gas=10_000_000,
        max_max_fee_per_gas=1_000_000_000,
        min_priority_fee_per_gas=1_000_000,
        max_priority_fee_per_gas=100_000_000,
    )


class GasEstimatorTest(unittest.TestCase):
    def test_estimates_eip1559_fees_and_buffered_gas_limit(self):
        quote = GasEstimator(FakeRpc(), policy()).estimate({"to": "0x" + "12" * 20})

        self.assertEqual(quote.gas_limit, 120_000)
        self.assertEqual(quote.max_priority_fee_per_gas, 1_000_000)
        self.assertEqual(quote.max_fee_per_gas, 21_000_000)

    def test_applies_low_fee_and_gas_floors(self):
        quote = GasEstimator(
            FakeRpc(estimate=1, base=0, priority=0), policy()
        ).estimate({"to": "0x" + "12" * 20})

        self.assertEqual(quote.gas_limit, 21_000)
        self.assertEqual(quote.max_priority_fee_per_gas, 1_000_000)
        self.assertEqual(quote.max_fee_per_gas, 10_000_000)

    def test_rejects_abnormally_high_chain_values(self):
        cases = (
            FakeRpc(estimate=1_500_001),
            FakeRpc(base=1_000_000_001),
            FakeRpc(priority=100_000_001),
        )
        for rpc in cases:
            with self.subTest(values=rpc.values):
                with self.assertRaises(GasError):
                    GasEstimator(rpc, policy()).estimate({"to": "0x" + "12" * 20})

    def test_loads_actual_gas_policy_from_config(self):
        loaded = load_gas_policy(Path("config/execution.yaml"))

        self.assertEqual(loaded.max_gas_limit, 1_500_000)
        self.assertEqual(loaded.max_max_fee_per_gas, 1_000_000_000)


if __name__ == "__main__":
    unittest.main()
