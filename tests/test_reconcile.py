import unittest

from okxlp.exec.reconcile import ReconcileError, reconcile_on_startup
from okxlp.uniswap.portfolio import OwnedPosition, PortfolioSnapshot


OWNER = "0xb7394e865eb6f22df7aa199e59887e8aac0947a2"
TOKEN0 = "0x9147b03c16b18fc4f686f610f189f91ddf4347b4"
TOKEN1 = "0xb6ceceab302e2e4948951ee7843fc24e92933061"
NPM = "0x315e413a11ab0df498ef83873012430ca36638ae"
ROUTER = "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca"


def position(token_id: int, liquidity: int) -> OwnedPosition:
    return OwnedPosition(
        token_id=token_id,
        token0=TOKEN0,
        token1=TOKEN1,
        fee=500,
        tick_lower=-201580,
        tick_upper=-201470,
        liquidity=liquidity,
    )


class FakeReader:
    def __init__(self, positions=(), other_pool_position_count=0):
        self.calls = []
        self.snapshot = PortfolioSnapshot(
            block=68_886_709,
            owner=OWNER,
            positions=tuple(positions),
            other_pool_position_count=other_pool_position_count,
            balance0_raw=0,
            balance1_raw=0,
            allowances={},
        )

    def read(self, owner, *, spenders=()):
        self.calls.append((owner, tuple(spenders)))
        return self.snapshot


class ReconcileOnStartupTest(unittest.TestCase):
    def test_zero_positions_is_legal_idle_state(self):
        reader = FakeReader()

        result = reconcile_on_startup(reader, OWNER, spenders=(NPM, ROUTER))

        self.assertIs(result.snapshot, reader.snapshot)
        self.assertEqual(result.token_ids, frozenset())
        self.assertIsNone(result.active_position)
        self.assertEqual(result.warnings, ())
        self.assertEqual(reader.calls, [(OWNER, (NPM, ROUTER))])

    def test_one_liquid_position_is_active(self):
        active = position(15_857, 123)
        result = reconcile_on_startup(
            FakeReader((active,)), OWNER, spenders=()
        )

        self.assertEqual(result.token_ids, frozenset({15_857}))
        self.assertIs(result.active_position, active)
        self.assertEqual(result.warnings, ())

    def test_two_positions_with_one_empty_warns_and_selects_liquid_one(self):
        empty = position(7, 0)
        active = position(15_857, 999)

        result = reconcile_on_startup(
            FakeReader((empty, active)), OWNER, spenders=()
        )

        self.assertEqual(result.token_ids, frozenset({7, 15_857}))
        self.assertIs(result.active_position, active)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("本池头寸数为 2", result.warnings[0])

    def test_two_liquid_positions_require_manual_recovery(self):
        reader = FakeReader((position(7, 100), position(15_857, 200)))

        with self.assertRaisesRegex(ReconcileError, "流动性大于 0.*2"):
            reconcile_on_startup(reader, OWNER, spenders=())

    def test_other_pool_positions_are_reported_as_warning(self):
        result = reconcile_on_startup(
            FakeReader(other_pool_position_count=3), OWNER, spenders=()
        )

        self.assertEqual(len(result.warnings), 1)
        self.assertIn("其他池头寸数为 3", result.warnings[0])


if __name__ == "__main__":
    unittest.main()
