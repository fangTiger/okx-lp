import tempfile
import textwrap
import unittest
from pathlib import Path

from eth_abi import encode

from okxlp.chain.calldata_policy import CalldataPolicy, CalldataPolicyError


NPM = "0x315e413a11ab0df498ef83873012430ca36638ae"
ROUTER = "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca"
TOKEN0 = "0x9147b03c16b18fc4f686f610f189f91ddf4347b4"
TOKEN1 = "0xb6ceceab302e2e4948951ee7843fc24e92933061"
EXECUTOR = "0x1111111111111111111111111111111111111111"
ATTACKER = "0x9999999999999999999999999999999999999999"
THIRD_TOKEN = "0x7777777777777777777777777777777777777777"
TOKEN_ID = 15857
NOW = 2_000_000_000

MINT_TYPE = "(address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)"
DECREASE_TYPE = "(uint256,uint128,uint256,uint256,uint256)"
COLLECT_TYPE = "(uint256,address,uint128,uint128)"
SWAP_TYPE = "(address,address,uint24,address,uint256,uint256,uint160)"


def calldata(selector, abi_type, values):
    return selector + encode([abi_type], [values]).hex()


class CalldataPolicyTest(unittest.TestCase):
    def setUp(self):
        self.policy = CalldataPolicy(
            executor_address=EXECUTOR,
            npm_address=NPM,
            router_address=ROUTER,
            token0=TOKEN0,
            token1=TOKEN1,
            fee=500,
            allowed_token_ids=frozenset({TOKEN_ID}),
        )

    def mint(self, *, token0=TOKEN0, token1=TOKEN1, recipient=EXECUTOR,
             fee=500, deadline=NOW + 600):
        values = (token0, token1, fee, -201600, -201500, 10, 20, 9, 18,
                  recipient, deadline)
        return calldata("0x88316456", MINT_TYPE, values)

    def decrease(self, *, token_id=TOKEN_ID, liquidity=123, deadline=NOW + 600):
        return calldata(
            "0x0c49ccbe", DECREASE_TYPE,
            (token_id, liquidity, 0, 0, deadline),
        )

    def collect(self, *, token_id=TOKEN_ID, recipient=EXECUTOR):
        return calldata(
            "0xfc6f7865", COLLECT_TYPE,
            (token_id, recipient, 2**128 - 1, 2**128 - 1),
        )

    def burn(self, *, token_id=TOKEN_ID):
        return calldata("0x42966c68", "uint256", token_id)

    def swap(self, *, token_in=TOKEN0, token_out=TOKEN1, fee=500,
             recipient=EXECUTOR, amount_in=10, amount_out_minimum=9):
        values = (
            token_in, token_out, fee, recipient, amount_in,
            amount_out_minimum, 0,
        )
        return calldata("0x04e45aaf", SWAP_TYPE, values)

    def assert_rejected(self, target, encoded, *, value=0, message=None):
        context = self.assertRaisesRegex(CalldataPolicyError, message) if message else self.assertRaises(CalldataPolicyError)
        with context:
            self.policy.validate(
                target=target, calldata=encoded, value=value, now_ts=NOW
            )

    def test_constructor_normalizes_addresses(self):
        policy = CalldataPolicy(
            executor_address="0x" + "AA" * 20,
            npm_address=NPM,
            router_address=ROUTER,
            token0=TOKEN0,
            token1=TOKEN1,
            fee=500,
            allowed_token_ids=frozenset(),
        )

        self.assertEqual(policy.executor_address, "0x" + "aa" * 20)

    def test_from_config_loads_addresses_tokens_and_fee(self):
        policy = CalldataPolicy.from_config(
            Path("config/execution.yaml"), Path("config/pools.yaml"),
            executor_address=EXECUTOR, allowed_token_ids={TOKEN_ID},
        )

        self.assertEqual(policy.npm_address, NPM)
        self.assertEqual(policy.router_address, ROUTER)
        self.assertEqual(policy.token0, TOKEN0)
        self.assertEqual(policy.token1, TOKEN1)
        self.assertEqual(policy.fee, 500)
        self.assertEqual(policy.allowed_token_ids, frozenset({TOKEN_ID}))

    def test_from_config_rejects_missing_required_fee(self):
        with tempfile.TemporaryDirectory() as directory:
            pools = Path(directory) / "pools.yaml"
            pools.write_text(textwrap.dedent(f"""
                pools:
                  - address: "0xc3d659028117f1ae5db9b9c68239b4a71f03ef37"
                    token0:
                      address: "{TOKEN0}"
                    token1:
                      address: "{TOKEN1}"
            """), encoding="utf-8")

            with self.assertRaises(CalldataPolicyError):
                CalldataPolicy.from_config(
                    Path("config/execution.yaml"), pools,
                    executor_address=EXECUTOR, allowed_token_ids={TOKEN_ID},
                )

    def test_all_supported_calls_pass_with_valid_parameters(self):
        calls = (
            (NPM, self.mint()),
            (NPM, self.decrease()),
            (NPM, self.collect()),
            (NPM, self.burn()),
            (ROUTER, self.swap()),
        )

        for target, encoded in calls:
            with self.subTest(selector=encoded[:10]):
                self.policy.validate(
                    target=target, calldata=encoded, value=0, now_ts=NOW
                )

    def test_a3_collect_rejects_attacker_recipient(self):
        self.assert_rejected(
            NPM, self.collect(recipient=ATTACKER), message="recipient"
        )

    def test_swap_parameter_attacks_are_rejected_individually(self):
        attacks = (
            (self.swap(recipient=ATTACKER), "recipient"),
            (self.swap(token_out=THIRD_TOKEN), "tokenOut"),
            (self.swap(fee=3000), "fee"),
            (self.swap(amount_out_minimum=0), "amountOutMinimum"),
        )

        for encoded, message in attacks:
            with self.subTest(message=message):
                self.assert_rejected(ROUTER, encoded, message=message)

    def test_mint_parameter_attacks_are_rejected_individually(self):
        attacks = (
            (self.mint(recipient=ATTACKER), "recipient"),
            (self.mint(token0=TOKEN1, token1=TOKEN0), "token0"),
            (self.mint(deadline=2**256 - 1), "deadline"),
        )

        for encoded, message in attacks:
            with self.subTest(message=message):
                self.assert_rejected(NPM, encoded, message=message)

    def test_nonzero_value_is_rejected_for_any_whitelisted_call(self):
        self.assert_rejected(NPM, self.collect(), value=1, message="value")

    def test_non_integer_now_timestamp_is_rejected(self):
        with self.assertRaisesRegex(CalldataPolicyError, "now_ts"):
            self.policy.validate(
                target=NPM, calldata=self.collect(), value=0, now_ts=True
            )

    def test_trailing_bytes_and_unknown_target_selector_pair_are_rejected(self):
        self.assert_rejected(NPM, self.collect() + "00", message="尾随")
        self.assert_rejected(NPM, self.swap(), message="目标地址与方法选择器")

    def test_calldata_with_embedded_whitespace_is_rejected(self):
        encoded = self.collect()
        malformed = encoded[:12] + " " + encoded[12:]

        self.assert_rejected(NPM, malformed, message="十六进制")

    def test_empty_allowed_token_ids_rejects_position_mutations(self):
        policy = CalldataPolicy(
            EXECUTOR, NPM, ROUTER, TOKEN0, TOKEN1, 500, frozenset()
        )

        for encoded in (self.decrease(), self.collect(), self.burn()):
            with self.subTest(selector=encoded[:10]):
                with self.assertRaisesRegex(CalldataPolicyError, "tokenId"):
                    policy.validate(
                        target=NPM, calldata=encoded, value=0, now_ts=NOW
                    )


if __name__ == "__main__":
    unittest.main()
