import contextlib
import importlib.util
import io
import sys
import unittest
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.uniswap.portfolio import (
    SELECTORS,
    OwnedPosition,
    PortfolioReader,
    PortfolioSnapshot,
)


OWNER = "0xb7394e865eb6f22df7aa199e59887e8aac0947a2"
NPM = "0x315e413a11ab0df498ef83873012430ca36638ae"
ROUTER = "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca"
TOKEN0 = "0x9147b03c16b18fc4f686f610f189f91ddf4347b4"
TOKEN1 = "0xb6ceceab302e2e4948951ee7843fc24e92933061"
OTHER_TOKEN = "0x1111111111111111111111111111111111111111"
BLOCK_NUMBER = 68886709
TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "read_portfolio.py"


def uint_word(value):
    return f"{value:064x}"


def signed_word(value):
    return uint_word(value if value >= 0 else (1 << 256) + value)


def address_word(address):
    return address[2:].lower().rjust(64, "0")


def position_data(
    *, token0=TOKEN0, token1=TOKEN1, fee=500,
    tick_lower=-201970, tick_upper=-201070, liquidity=21126254269852,
):
    words = [
        uint_word(0), address_word("0x0000000000000000000000000000000000000000"),
        address_word(token0), address_word(token1), uint_word(fee),
        signed_word(tick_lower), signed_word(tick_upper), uint_word(liquidity),
        uint_word(0), uint_word(0), uint_word(0), uint_word(0),
    ]
    return "0x" + "".join(words)


class FakeRpcClient:
    def __init__(self, *, token_ids=(), positions=None, balances=None, allowances=None):
        self.token_ids = tuple(token_ids)
        self.positions = positions or {}
        self.balances = balances or {TOKEN0: 123, TOKEN1: 456}
        self.allowances = allowances or {}
        self.calls = []
        self.chain_checks = 0

    def ensure_chain_id(self):
        self.chain_checks += 1
        return 196

    def block_number(self):
        return BLOCK_NUMBER

    def eth_call(self, to, data, block="latest"):
        to = to.lower()
        self.calls.append((to, data, block))
        selector = data[:10]
        if to == NPM and selector == "0x70a08231":
            return "0x" + uint_word(len(self.token_ids))
        if to == NPM and selector == "0x2f745c59":
            index = int(data[-64:], 16)
            return "0x" + uint_word(self.token_ids[index])
        if to == NPM and selector == "0x99fbab88":
            return self.positions[int(data[-64:], 16)]
        if selector == "0x70a08231":
            return "0x" + uint_word(self.balances[to])
        if selector == "0xdd62ed3e":
            spender = "0x" + data[-40:].lower()
            return "0x" + uint_word(self.allowances.get((to, spender), 0))
        raise AssertionError(f"意外的 eth_call：{to} {data}")


class PortfolioReaderTest(unittest.TestCase):
    def reader(self, rpc):
        return PortfolioReader(
            rpc, npm_address=NPM, token0=TOKEN0, token1=TOKEN1, fee=500
        )

    def test_locked_selectors_are_used(self):
        self.assertEqual(SELECTORS, {
            "balance_of": "0x70a08231",
            "token_of_owner_by_index": "0x2f745c59",
            "positions": "0x99fbab88",
            "allowance": "0xdd62ed3e",
            "owner_of": "0x6352211e",
        })

    def test_reads_one_pool_position_and_decodes_negative_ticks(self):
        rpc = FakeRpcClient(
            token_ids=(15857,), positions={15857: position_data()},
            allowances={(TOKEN0, NPM): 100, (TOKEN1, NPM): 200},
        )

        snapshot = self.reader(rpc).read(OWNER, spenders=(NPM,))

        self.assertEqual(snapshot.block, BLOCK_NUMBER)
        self.assertEqual(snapshot.owner, OWNER)
        self.assertEqual(snapshot.balance0_raw, 123)
        self.assertEqual(snapshot.balance1_raw, 456)
        self.assertEqual(snapshot.allowance_of(TOKEN0, NPM), 100)
        self.assertEqual(len(snapshot.positions), 1)
        position = snapshot.positions[0]
        self.assertEqual(position.token_id, 15857)
        self.assertEqual(position.token0, TOKEN0)
        self.assertEqual(position.token1, TOKEN1)
        self.assertEqual(position.fee, 500)
        self.assertEqual(position.tick_lower, -201970)
        self.assertEqual(position.tick_upper, -201070)
        self.assertEqual(position.liquidity, 21126254269852)

    def test_filters_positions_by_ordered_pair_and_fee(self):
        rpc = FakeRpcClient(token_ids=(1, 2, 3), positions={
            1: position_data(),
            2: position_data(fee=3000),
            3: position_data(token0=OTHER_TOKEN),
        })

        snapshot = self.reader(rpc).read(OWNER)

        self.assertEqual(tuple(item.token_id for item in snapshot.positions), (1,))
        self.assertEqual(snapshot.other_pool_position_count, 2)

    def test_keeps_zero_liquidity_position(self):
        rpc = FakeRpcClient(
            token_ids=(9,), positions={9: position_data(liquidity=0)}
        )

        snapshot = self.reader(rpc).read(OWNER)

        self.assertEqual(snapshot.positions[0].liquidity, 0)
        self.assertEqual(snapshot.token_ids, frozenset({9}))

    def test_rejects_positions_response_without_all_twelve_words(self):
        rpc = FakeRpcClient(
            token_ids=(9,), positions={9: position_data()[:-64]}
        )

        with self.assertRaisesRegex(ValueError, "positions.*12 个 ABI 字"):
            self.reader(rpc).read(OWNER)

    def test_zero_npm_balance_returns_empty_positions(self):
        snapshot = self.reader(FakeRpcClient()).read(OWNER)

        self.assertEqual(snapshot.positions, ())
        self.assertEqual(snapshot.token_ids, frozenset())

    def test_rejects_more_than_fifty_positions(self):
        rpc = FakeRpcClient(token_ids=range(51))

        with self.assertRaisesRegex(ValueError, "超过 50.*配置或地址"):
            self.reader(rpc).read(OWNER)

        self.assertEqual(len(rpc.calls), 1)

    def test_every_eth_call_uses_the_same_fixed_block(self):
        rpc = FakeRpcClient(
            token_ids=(15857,), positions={15857: position_data()}
        )

        self.reader(rpc).read(OWNER, spenders=(NPM, ROUTER))

        self.assertEqual(rpc.chain_checks, 1)
        self.assertTrue(rpc.calls)
        self.assertEqual({call[2] for call in rpc.calls}, {hex(BLOCK_NUMBER)})

    def test_allowance_sufficiency_includes_equality_boundary(self):
        snapshot = PortfolioSnapshot(
            block=1, owner=OWNER, positions=(), other_pool_position_count=0,
            balance0_raw=0, balance1_raw=0, allowances={(TOKEN0, NPM): 100},
        )

        self.assertTrue(snapshot.has_sufficient_allowance(TOKEN0.upper(), NPM, 100))
        self.assertFalse(snapshot.has_sufficient_allowance(TOKEN0, NPM, 101))

    def test_snapshot_does_not_expose_stale_fee_fields(self):
        names = {field.name for field in fields(PortfolioSnapshot)}

        self.assertFalse(any("tokens_owed" in name for name in names))
        self.assertFalse(any("fee_growth" in name for name in names))


def load_portfolio_tool():
    spec = importlib.util.spec_from_file_location("read_portfolio", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PortfolioToolTest(unittest.TestCase):
    def test_pool_id_is_optional_and_selectable(self):
        tool = load_portfolio_tool()

        self.assertIsNone(
            tool.build_parser().parse_args(["--owner", OWNER]).pool_id
        )
        selected = tool.build_parser().parse_args([
            "--owner", OWNER, "--pool-id", "wMRNAx_USDG",
        ])
        self.assertEqual(selected.pool_id, "wMRNAx_USDG")

    def test_owner_argument_is_required(self):
        tool = load_portfolio_tool()

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            tool.build_parser().parse_args([])

    def test_renders_locked_position_balances_allowances_and_range(self):
        tool = load_portfolio_tool()
        position = OwnedPosition(
            token_id=15857, token0=TOKEN0, token1=TOKEN1, fee=500,
            tick_lower=-201970, tick_upper=-201070, liquidity=21126254269852,
        )
        snapshot = PortfolioSnapshot(
            block=BLOCK_NUMBER, owner=OWNER, positions=(position,),
            other_pool_position_count=0, balance0_raw=10**18,
            balance1_raw=2_500_000, allowances={
                (TOKEN0, NPM): 11, (TOKEN0, ROUTER): 12,
                (TOKEN1, NPM): 21, (TOKEN1, ROUTER): 22,
            },
        )
        pool = SimpleNamespace(
            token0=SimpleNamespace(symbol="wASMLx", decimals=18, address=TOKEN0),
            token1=SimpleNamespace(symbol="USDC", decimals=6, address=TOKEN1),
        )

        output = tool.render_snapshot(
            snapshot, pool_config=pool, current_tick=-201526,
            npm_address=NPM, router_address=ROUTER,
        )

        for expected in (
            "区块        68886709", "NPM balanceOf 1",
            "other_pool_position_count 0", "tokenId      15857",
            f"token0       {TOKEN0}", f"token1       {TOKEN1}",
            "fee          500", "tickLower    -201970", "tickUpper    -201070",
            "liquidity    21126254269852", "in-range     是",
            "wASMLx raw=1000000000000000000 human=1",
            "USDC raw=2500000 human=2.5", "wASMLx -> NPM raw=11",
            "USDC -> SwapRouter02 raw=22",
        ):
            self.assertIn(expected, output)

    def test_tool_source_has_no_write_chain_entrypoint(self):
        source = TOOL_PATH.read_text(encoding="utf-8")

        self.assertNotIn("send_raw_transaction", source)
        self.assertNotIn("eth_sendRawTransaction", source)
        self.assertNotIn("Intent", source)


if __name__ == "__main__":
    unittest.main()
