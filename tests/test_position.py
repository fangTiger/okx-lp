import sys
import unittest
from decimal import Decimal
from pathlib import Path

from eth_abi import decode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.uniswap.position import PositionManager


NPM = "0x315e413a11ab0df498ef83873012430ca36638ae"
TOKEN0 = "0x9147b03c16b18fc4f686f610f189f91ddf4347b4"
TOKEN1 = "0xb6ceceab302e2e4948951ee7843fc24e92933061"
RECIPIENT = "0x1111111111111111111111111111111111111111"


class PositionManagerTest(unittest.TestCase):
    def setUp(self):
        self.manager = PositionManager(NPM)

    def test_mint_uses_m1_outward_aligned_tick_range(self):
        intent = self.manager.mint(
            token0=TOKEN0,
            token1=TOKEN1,
            fee=500,
            current_tick=-201526,
            width=Decimal("0.005"),
            tick_spacing=10,
            amount0_desired=10**15,
            amount1_desired=1_000_000,
            amount0_min=990_000_000_000_000,
            amount1_min=990_000,
            recipient=RECIPIENT,
            deadline=2_000_000_000,
        )

        values = decode(
            ["(address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)"],
            bytes.fromhex(intent.calldata[10:]),
        )[0]
        self.assertEqual(intent.target, NPM)
        self.assertEqual(intent.calldata[:10], "0x88316456")
        self.assertEqual(values[3:5], (-201580, -201470))
        self.assertEqual(values[5:9], (10**15, 1_000_000, 990_000_000_000_000, 990_000))

    def test_mint_accepts_precomputed_band_without_recalculating_ticks(self):
        intent = self.manager.mint(
            token0=TOKEN0,
            token1=TOKEN1,
            fee=500,
            tick_lower=-201591,
            tick_upper=-201463,
            amount0_desired=10**15,
            amount1_desired=1_000_000,
            amount0_min=990_000_000_000_000,
            amount1_min=990_000,
            recipient=RECIPIENT,
            deadline=2_000_000_000,
        )

        values = decode(
            ["(address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)"],
            bytes.fromhex(intent.calldata[10:]),
        )[0]
        self.assertEqual(values[3:5], (-201591, -201463))

    def test_decrease_liquidity_builds_burn_stage_intent(self):
        intent = self.manager.decrease_liquidity(
            token_id=7,
            liquidity=123,
            amount0_min=9,
            amount1_min=18,
            deadline=2_000_000_000,
        )

        values = decode(
            ["(uint256,uint128,uint256,uint256,uint256)"],
            bytes.fromhex(intent.calldata[10:]),
        )[0]
        self.assertEqual(intent.calldata[:10], "0x0c49ccbe")
        self.assertEqual(values, (7, 123, 9, 18, 2_000_000_000))

    def test_collect_defaults_to_full_uint128_amounts(self):
        intent = self.manager.collect(token_id=7, recipient=RECIPIENT)

        values = decode(
            ["(uint256,address,uint128,uint128)"],
            bytes.fromhex(intent.calldata[10:]),
        )[0]
        self.assertEqual(intent.calldata[:10], "0xfc6f7865")
        self.assertEqual(values[0], 7)
        self.assertEqual(values[2:], (2**128 - 1, 2**128 - 1))

    def test_burn_builds_empty_nft_cleanup_intent(self):
        intent = self.manager.burn(7)

        token_id = decode(["uint256"], bytes.fromhex(intent.calldata[10:]))[0]
        self.assertEqual(intent.calldata[:10], "0x42966c68")
        self.assertEqual(token_id, 7)

    def test_rebalance_builders_accept_preallocated_intent_ids(self):
        ids = ("11" * 16, "22" * 16, "33" * 16)

        decrease = self.manager.decrease_liquidity(
            token_id=7, liquidity=123, amount0_min=9, amount1_min=18,
            deadline=2_000_000_000, intent_id=ids[0],
        )
        collect = self.manager.collect(
            token_id=7, recipient=RECIPIENT, intent_id=ids[1]
        )
        mint = self.manager.mint(
            token0=TOKEN0, token1=TOKEN1, fee=500,
            current_tick=-201526, width=Decimal("0.005"), tick_spacing=10,
            amount0_desired=10**15, amount1_desired=1_000_000,
            amount0_min=0, amount1_min=0, recipient=RECIPIENT,
            deadline=2_000_000_000, intent_id=ids[2],
        )

        self.assertEqual(
            (decrease.intent_id, collect.intent_id, mint.intent_id), ids
        )


if __name__ == "__main__":
    unittest.main()
