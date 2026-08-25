import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.uniswap.pool import SELECTORS, UniswapV3Pool, decode_int


def uint_word(value):
    return f"{value:064x}"


def signed_word(value):
    return uint_word(value if value >= 0 else (1 << 256) + value)


def address_word(address):
    return "0" * 24 + address[2:]


def dynamic_string(value):
    encoded = value.encode().hex()
    padded = encoded.ljust(((len(encoded) + 63) // 64) * 64, "0")
    return "0x" + uint_word(32) + uint_word(len(value.encode())) + padded


class FakeRpcClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.chain_checks = 0

    def ensure_chain_id(self):
        self.chain_checks += 1
        return 196

    def block_number(self):
        return 68886709

    def eth_call(self, to, data, block="latest"):
        self.calls.append((to, data, block))
        return self.responses[(to.lower(), data[:10])]


class PoolTest(unittest.TestCase):
    def setUp(self):
        self.pool = "0xc3d659028117f1ae5db9b9c68239b4a71f03ef37"
        self.token0 = "0x9147b03c16b18fc4f686f610f189f91ddf4347b4"
        self.token1 = "0xb6ceceab302e2e4948951ee7843fc24e92933061"
        self.factory = "0x4b2ab38dbf28d31d467aa8993f6c2585981d6804"
        sqrt_price = 3333962123355733730549486
        slot0 = "0x" + uint_word(sqrt_price) + signed_word(-201526) + uint_word(0) * 5
        self.responses = {
            (self.pool, SELECTORS["token0"]): "0x" + address_word(self.token0),
            (self.pool, SELECTORS["token1"]): "0x" + address_word(self.token1),
            (self.pool, SELECTORS["factory"]): "0x" + address_word(self.factory),
            (self.pool, SELECTORS["fee"]): "0x" + uint_word(500),
            (self.pool, SELECTORS["tick_spacing"]): "0x" + uint_word(10),
            (self.pool, SELECTORS["liquidity"]): "0x" + uint_word(14942241291635132),
            (self.pool, SELECTORS["slot0"]): slot0,
            (self.token0, SELECTORS["symbol"]): dynamic_string("wASMLx"),
            (self.token0, SELECTORS["name"]): dynamic_string("Wrapped ASML xStock"),
            (self.token0, SELECTORS["decimals"]): "0x" + uint_word(18),
            (self.token0, SELECTORS["balance_of"]): "0x" + uint_word(56345412000000000000),
            (self.token1, SELECTORS["symbol"]): "0x" + "USDC".encode().hex().ljust(64, "0"),
            (self.token1, SELECTORS["name"]): dynamic_string("USDC"),
            (self.token1, SELECTORS["decimals"]): "0x" + uint_word(6),
            (self.token1, SELECTORS["balance_of"]): "0x" + uint_word(144828242844),
        }

    def test_decodes_sign_extended_tick(self):
        self.assertEqual(decode_int(signed_word(-201526), signed=True), -201526)

    def test_reads_structured_snapshot_at_one_block(self):
        client = FakeRpcClient(self.responses)
        snapshot = UniswapV3Pool(client, self.pool).snapshot()

        self.assertEqual(snapshot.block, 68886709)
        self.assertEqual(snapshot.tick, -201526)
        self.assertEqual(snapshot.sqrt_price_x96, 3333962123355733730549486)
        self.assertEqual(snapshot.active_liquidity, 14942241291635132)
        self.assertEqual(snapshot.fee, 500)
        self.assertEqual(snapshot.tick_spacing, 10)
        self.assertEqual(snapshot.factory, self.factory)
        self.assertEqual(snapshot.token0.symbol, "wASMLx")
        self.assertEqual(snapshot.token0.name, "Wrapped ASML xStock")
        self.assertEqual(snapshot.token0.balance, Decimal("56.345412"))
        self.assertEqual(snapshot.token1.symbol, "USDC")
        self.assertEqual(snapshot.token1.balance, Decimal("144828.242844"))
        self.assertAlmostEqual(float(snapshot.price), 1770.77, places=6)
        self.assertEqual(client.chain_checks, 1)
        self.assertTrue(all(call[2] == hex(68886709) for call in client.calls))


if __name__ == "__main__":
    unittest.main()
