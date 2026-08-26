import sys
import unittest
from decimal import Decimal
from pathlib import Path

from eth_abi import decode, encode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.uniswap.swap import SwapPolicy, SwapRouter


ROUTER = "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca"
QUOTER = "0xd1b797d92d87b688193a2b976efc8d577d204343"
TOKEN_IN = "0xb6ceceab302e2e4948951ee7843fc24e92933061"
TOKEN_OUT = "0x9147b03c16b18fc4f686f610f189f91ddf4347b4"
RECIPIENT = "0x1111111111111111111111111111111111111111"


class QuoteRpc:
    def __init__(self, amount_out=None):
        self.calls = []
        self.amount_out = amount_out

    def eth_call(self, to, data, block="latest"):
        self.calls.append((to, data, block))
        params = decode(
            ["(address,address,uint256,uint24,uint160)"],
            bytes.fromhex(data[10:]),
        )[0]
        amount_out = params[2] * 2 if self.amount_out is None else self.amount_out
        return "0x" + encode(
            ["uint256", "uint160", "uint32", "uint256"],
            [amount_out, 123456, 2, 150000],
        ).hex()


class MinimumRandom:
    def randint(self, lower, _upper):
        return lower


class SwapRouterTest(unittest.TestCase):
    def setUp(self):
        self.rpc = QuoteRpc()
        self.policy = SwapPolicy(
            max_slippage_bps=Decimal("30"),
            split_threshold_usd=Decimal("500"),
            split_parts_min=3,
            split_parts_max=5,
            split_interval_seconds_min=20,
            split_interval_seconds_max=30,
        )
        self.router = SwapRouter(
            rpc=self.rpc,
            router_address=ROUTER,
            quoter_address=QUOTER,
            policy=self.policy,
            random_source=MinimumRandom(),
        )

    def test_quote_uses_quoter_v2_and_30_bps_minimum(self):
        quote = self.router.quote_exact_input_single(
            token_in=TOKEN_IN,
            token_out=TOKEN_OUT,
            fee=500,
            amount_in=5_000,
        )

        self.assertEqual(self.rpc.calls[0][0], QUOTER)
        self.assertEqual(self.rpc.calls[0][1][:10], "0xc6a5026a")
        self.assertEqual(quote.amount_out, 10_000)
        self.assertEqual(quote.amount_out_minimum, 9_970)
        self.assertEqual(quote.slippage_bps, Decimal("30"))

    def test_exact_input_single_uses_quote_in_router_intent(self):
        scheduled = self.router.exact_input_single(
            token_in=TOKEN_IN,
            token_out=TOKEN_OUT,
            fee=500,
            recipient=RECIPIENT,
            amount_in=5_000,
        )

        params = decode(
            ["(address,address,uint24,address,uint256,uint256,uint160)"],
            bytes.fromhex(scheduled.intent.calldata[10:]),
        )[0]
        self.assertEqual(scheduled.intent.target, ROUTER)
        self.assertEqual(scheduled.intent.calldata[:10], "0x04e45aaf")
        self.assertEqual(params[4:6], (5_000, 9_970))

    def test_slippage_above_configured_limit_is_rejected_before_quote(self):
        with self.assertRaisesRegex(ValueError, "滑点.*30"):
            self.router.quote_exact_input_single(
                token_in=TOKEN_IN,
                token_out=TOKEN_OUT,
                fee=500,
                amount_in=5_000,
                slippage_bps=Decimal("31"),
            )

        self.assertEqual(self.rpc.calls, [])

    def test_zero_amount_out_minimum_is_rejected(self):
        router = SwapRouter(
            rpc=QuoteRpc(amount_out=1),
            router_address=ROUTER,
            quoter_address=QUOTER,
            policy=self.policy,
        )

        with self.assertRaisesRegex(ValueError, "最低到账数量为零"):
            router.quote_exact_input_single(
                token_in=TOKEN_IN,
                token_out=TOKEN_OUT,
                fee=500,
                amount_in=1,
                slippage_bps=Decimal("30"),
            )

    def test_amount_below_threshold_stays_single(self):
        plan = self.router.plan_exact_input_single(
            token_in=TOKEN_IN,
            token_out=TOKEN_OUT,
            fee=500,
            recipient=RECIPIENT,
            amount_in=499_000_000,
            amount_usd=Decimal("499"),
        )

        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].delay_seconds, 0)

    def test_negative_usd_amount_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "amount_usd.*非负"):
            self.router.plan_exact_input_single(
                token_in=TOKEN_IN,
                token_out=TOKEN_OUT,
                fee=500,
                recipient=RECIPIENT,
                amount_in=1,
                amount_usd=Decimal("-1"),
            )

    def test_amount_at_threshold_splits_and_preserves_raw_total(self):
        plan = self.router.plan_exact_input_single(
            token_in=TOKEN_IN,
            token_out=TOKEN_OUT,
            fee=500,
            recipient=RECIPIENT,
            amount_in=500_000_002,
            amount_usd=Decimal("500"),
        )

        self.assertEqual(len(plan), 3)
        self.assertEqual(sum(item.quote.amount_in for item in plan), 500_000_002)
        self.assertEqual([item.delay_seconds for item in plan], [0, 20, 20])
        self.assertEqual(len(self.rpc.calls), 3)

    def test_split_plan_uses_first_preallocated_intent_ids(self):
        intent_ids = tuple(f"{index:032x}" for index in range(1, 6))

        plan = self.router.plan_exact_input_single(
            token_in=TOKEN_IN,
            token_out=TOKEN_OUT,
            fee=500,
            recipient=RECIPIENT,
            amount_in=500_000_002,
            amount_usd=Decimal("500"),
            intent_ids=intent_ids,
        )

        self.assertEqual(
            tuple(item.intent.intent_id for item in plan), intent_ids[:3]
        )

    def test_actual_risk_config_loads_swap_defaults(self):
        policy = SwapPolicy.from_config(Path("config/risk.yaml"))

        self.assertEqual(policy.max_slippage_bps, Decimal("30"))
        self.assertEqual(policy.split_threshold_usd, Decimal("500"))
        self.assertEqual((policy.split_parts_min, policy.split_parts_max), (3, 5))
        self.assertEqual(
            (policy.split_interval_seconds_min, policy.split_interval_seconds_max),
            (20, 30),
        )


if __name__ == "__main__":
    unittest.main()
