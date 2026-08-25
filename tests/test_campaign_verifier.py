import sys
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.campaign.verifier import VerificationError, verify_campaign
from okxlp.config import load_config
from okxlp.uniswap.pool import PoolSnapshot, TokenMetadata


def make_snapshot(config, *, fee=500, token1_decimals=6):
    pool = config.pools[0]
    return PoolSnapshot(
        block=68886709,
        address=pool.address,
        factory=pool.factory,
        fee=fee,
        tick_spacing=10,
        sqrt_price_x96=3333962123355733730549486,
        tick=-201526,
        active_liquidity=14942241291635132,
        token0=TokenMetadata(pool.token0.address, "wASMLx", "Wrapped ASML xStock", 18, 0),
        token1=TokenMetadata(pool.token1.address, "USDC", "USDC", token1_decimals, 0),
    )


class FakeRpc:
    def __init__(self, no_code=()):
        self.no_code = {address.lower() for address in no_code}

    def ensure_chain_id(self):
        return 196

    def call(self, method, params):
        if method != "eth_getCode":
            raise AssertionError(f"非预期 RPC 方法：{method}")
        return "0x" if params[0].lower() in self.no_code else "0x6000"


class FakePool:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self):
        return self._snapshot


class CampaignVerifierTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config(Path("config/pools.yaml"))

    def _verify(self, config, snapshot, rpc=None):
        return verify_campaign(
            config,
            rpc or FakeRpc(),
            pool_factory=lambda _rpc, _address: FakePool(snapshot),
        )

    def test_matching_chain_values_pass(self):
        report = self._verify(self.config, make_snapshot(self.config))

        self.assertEqual(report.verified_pool_ids, ("wASMLx_USDC",))
        self.assertEqual(report.block, 68886709)

    def test_wrong_fee_lists_configuration_and_chain_values(self):
        wrong_pool = replace(self.config.pools[0], fee_bps=Decimal("30"))
        wrong_config = replace(self.config, pools=(wrong_pool,))

        with self.assertRaises(VerificationError) as captured:
            self._verify(wrong_config, make_snapshot(self.config))

        message = str(captured.exception)
        self.assertIn("fee_bps", message)
        self.assertIn("配置值=30", message)
        self.assertIn("链上值=5", message)
        self.assertIn("拒绝启动", message)

    def test_token_code_and_decimals_differences_are_aggregated(self):
        pool = self.config.pools[0]
        snapshot = make_snapshot(self.config, token1_decimals=18)

        with self.assertRaises(VerificationError) as captured:
            self._verify(self.config, snapshot, FakeRpc(no_code=(pool.token0.address,)))

        message = str(captured.exception)
        self.assertIn("token0.has_code", message)
        self.assertIn("配置值=存在", message)
        self.assertIn("链上值=不存在", message)
        self.assertIn("token1.decimals", message)
        self.assertIn("配置值=6", message)
        self.assertIn("链上值=18", message)


if __name__ == "__main__":
    unittest.main()
