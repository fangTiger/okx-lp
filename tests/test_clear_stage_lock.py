import importlib.util
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from okxlp.uniswap.portfolio import OwnedPosition, PortfolioSnapshot
from okxlp.uniswap.tickmath import price_to_sqrt_price_x96, price_to_tick


OWNER = "0xb7394e865eb6f22df7aa199e59887e8aac0947a2"
TOKEN0 = "0x9147b03c16b18fc4f686f610f189f91ddf4347b4"
TOKEN1 = "0xb6ceceab302e2e4948951ee7843fc24e92933061"
NPM = "0x315e413a11ab0df498ef83873012430ca36638ae"
ROUTER = "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca"
POOL_ADDRESS = "0x1111111111111111111111111111111111111111"
TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "clear_stage_lock.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("clear_stage_lock", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeReader:
    def __init__(self, positions=()):
        self.calls = []
        self.snapshot = PortfolioSnapshot(
            block=68_886_709,
            owner=OWNER,
            positions=tuple(positions),
            other_pool_position_count=0,
            balance0_raw=14_364_270_000_000_000,
            balance1_raw=174_690_000,
            allowances={},
        )

    def read(self, owner, *, spenders=()):
        self.calls.append((owner, tuple(spenders)))
        return self.snapshot


class FakeRpc:
    def eth_call(self, to, data, block=None):
        if to != POOL_ADDRESS or block != hex(68_886_709):
            raise AssertionError("只允许读取对账区块的目标池 slot0")
        price = Decimal("1738")
        sqrt_price = price_to_sqrt_price_x96(price, 18, 6)
        tick = price_to_tick(price, 18, 6)
        encoded_tick = tick % (1 << 256)
        return "0x" + f"{sqrt_price:064x}{encoded_tick:064x}" + "0" * (5 * 64)


def active_position():
    return OwnedPosition(
        token_id=15_857, token0=TOKEN0, token1=TOKEN1, fee=500,
        tick_lower=-202_980, tick_upper=-202_870, liquidity=123_456,
    )


class ClearStageLockToolTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name)
        self.state_path = self.state_dir / "machine_state_pool-1.json"
        self.payload = {
            "state": "ENTERING",
            "band": {
                "tick_lower": -202_980,
                "tick_upper": -202_870,
                "price_lower": "1729",
                "price_upper": "1747",
            },
            "out_since": None,
            "out_direction": None,
            "failure": "mint 阶段执行失败：RPC HTTP 错误 403",
            "failed_at": "2026-08-26T03:00:00Z",
        }
        self.state_path.write_text(
            json.dumps(self.payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def context(reader):
        token0 = SimpleNamespace(address=TOKEN0, symbol="wASMLx", decimals=18)
        token1 = SimpleNamespace(address=TOKEN1, symbol="USDC", decimals=6)
        pool = SimpleNamespace(
            pool_id="pool-1", address=POOL_ADDRESS,
            token0=token0, token1=token1,
        )
        return SimpleNamespace(
            pool=pool, rpc=FakeRpc(), reader=reader, spenders=(NPM, ROUTER),
        )

    def invoke(self, arguments, *, answer="我确认清除", positions=()):
        tool = load_tool()
        reader = FakeReader(positions)
        output = []
        code = tool.main(
            ["--pool-id", "pool-1", "--owner", OWNER, *arguments],
            context_factory=lambda _pool_id: self.context(reader),
            state_dir=self.state_dir,
            input_fn=lambda _prompt: answer,
            printer=output.append,
        )
        return code, "\n".join(output), reader

    def test_wrong_confirmation_returns_nonzero_and_preserves_exact_bytes(self):
        before = self.state_path.read_bytes()

        code, output, reader = self.invoke([], answer="确认")

        self.assertNotEqual(code, 0)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(reader.calls, [(OWNER, (NPM, ROUTER))])
        self.assertIn("当前链上头寸", output)
        self.assertIn("两腿余额", output)
        self.assertIn("当前池价", output)
        self.assertIn(self.payload["failure"], output)

    def test_yes_clears_only_failure_fields(self):
        code, _output, _reader = self.invoke(["--yes"])

        self.assertEqual(code, 0)
        cleared = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIsNone(cleared["failure"])
        self.assertIsNone(cleared["failed_at"])
        for key in set(self.payload) - {"failure", "failed_at"}:
            self.assertEqual(cleared[key], self.payload[key])

    def test_liquid_position_with_entering_state_prints_warning(self):
        code, output, _reader = self.invoke(["--yes"], positions=(active_position(),))

        self.assertEqual(code, 0)
        self.assertIn("警告", output)
        self.assertIn("状态与链上不一致", output)


if __name__ == "__main__":
    unittest.main()
