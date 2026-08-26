import sys
import unittest
from decimal import Decimal, localcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.uniswap import tickmath
from okxlp.uniswap.tickmath import (
    aligned_tick_range,
    aligned_tick_range_from_price,
    capital_to_liquidity,
    liquidity_share,
    liquidity_to_capital,
    position_amounts,
    price_to_sqrt_price_x96,
    price_to_tick,
    sqrt_price_x96_to_price,
    tick_to_price,
)


class TickMathTest(unittest.TestCase):
    def test_locked_snapshot_price_and_tick(self):
        price = Decimal("1770.77")
        sqrt_price_x96 = 3333962123355733730549486

        decoded = sqrt_price_x96_to_price(sqrt_price_x96, 18, 6)
        self.assertAlmostEqual(float(decoded), float(price), places=6)

        tick_price = tick_to_price(-201526, 18, 6)
        relative_error = abs(tick_price - price) / price
        self.assertLess(relative_error, Decimal("0.0005"))
        self.assertEqual(price_to_tick(tick_price, 18, 6), -201526)
        self.assertEqual(price_to_sqrt_price_x96(price, 18, 6), sqrt_price_x96)

    def test_historical_slot0_uses_probe_formula(self):
        price = sqrt_price_x96_to_price(3334556127410750031889556, 18, 6)
        self.assertAlmostEqual(float(price), 1771.401043919454, places=9)

    def test_half_percent_range_rounds_outward(self):
        lower, upper = aligned_tick_range(-201526, Decimal("0.005"), 10)
        self.assertEqual((lower, upper), (-201580, -201470))
        self.assertLessEqual(lower, price_to_tick(tick_to_price(-201526, 18, 6) * Decimal("0.995"), 18, 6))
        self.assertGreaterEqual(upper, price_to_tick(tick_to_price(-201526, 18, 6) * Decimal("1.005"), 18, 6))

    def test_lower_bound_uses_one_minus_width_before_alignment(self):
        lower, upper = aligned_tick_range(0, Decimal("0.005"), 10)
        self.assertEqual((lower, upper), (-60, 50))

    def test_exact_price_counterexample_rounds_both_sides_outward(self):
        actual = aligned_tick_range_from_price(
            Decimal("1.00009999"), Decimal("0.005"), 10, 0, 0,
        )

        self.assertEqual(actual, (-50, 60))

    def test_locked_snapshot_exact_price_range(self):
        actual = aligned_tick_range_from_price(
            Decimal("1770.77"), Decimal("0.005"), 10, 18, 6,
        )

        self.assertEqual(actual, (-201580, -201470))

    def test_exact_price_range_is_outward_for_every_tick_remainder(self):
        width = Decimal("0.005")
        fractions = tuple(map(Decimal, ("0", "0.25", "0.5", "0.75", "0.99")))
        with localcontext() as context:
            context.prec = 80
            for remainder in range(10):
                current_tick = 100 + remainder
                self.assertEqual(current_tick % 10, remainder)
                for fraction in fractions:
                    with self.subTest(remainder=remainder, fraction=fraction):
                        price = tick_to_price(current_tick, 0, 0) * (
                            tickmath.TICK_BASE ** fraction
                        )
                        lower, upper = aligned_tick_range_from_price(
                            price, width, 10, 0, 0,
                        )
                        self.assertLessEqual(
                            tick_to_price(lower, 0, 0), price * (Decimal(1) - width),
                        )
                        self.assertGreaterEqual(
                            tick_to_price(upper, 0, 0), price * (Decimal(1) + width),
                        )

    def test_exact_price_range_rejects_invalid_inputs(self):
        cases = (
            (Decimal("0"), Decimal("0.005"), 10, "价格必须大于零"),
            (Decimal("1"), Decimal("0"), 10, "区间宽度"),
            (Decimal("1"), Decimal("1"), 10, "区间宽度"),
            (Decimal("1"), Decimal("0.005"), 0, "tickSpacing"),
        )
        for price, width, spacing, message in cases:
            with self.subTest(price=price, width=width, spacing=spacing):
                with self.assertRaisesRegex(ValueError, message):
                    aligned_tick_range_from_price(price, width, spacing, 0, 0)

    def test_liquidity_and_capital_are_inverse(self):
        price = Decimal("1770.77")
        capital = Decimal("500")
        liquidity = capital_to_liquidity(capital, price, Decimal("0.005"), 18, 6)

        restored = liquidity_to_capital(liquidity, price, Decimal("0.005"), 18, 6)
        self.assertAlmostEqual(float(restored), float(capital), places=9)

    def test_share_uses_added_liquidity(self):
        active = Decimal("14942241291635132")
        mine = Decimal("1000000000000000")
        self.assertEqual(liquidity_share(active, mine), mine / (active + mine))

    def test_position_amounts_below_range_is_all_token0(self):
        sqrt_price = price_to_sqrt_price_x96(Decimal("0.99"), 0, 0)

        amount0, amount1 = position_amounts(10**18, 0, 100, sqrt_price)

        self.assertGreater(amount0, 0)
        self.assertEqual(amount1, 0)

    def test_position_amounts_above_range_is_all_token1(self):
        sqrt_price = price_to_sqrt_price_x96(Decimal("1.02"), 0, 0)

        amount0, amount1 = position_amounts(10**18, 0, 100, sqrt_price)

        self.assertEqual(amount0, 0)
        self.assertGreater(amount1, 0)

    def test_position_amounts_in_range_has_both_tokens(self):
        sqrt_price = price_to_sqrt_price_x96(Decimal("1.005"), 0, 0)

        amount0, amount1 = position_amounts(10**18, 0, 100, sqrt_price)

        self.assertGreater(amount0, 0)
        self.assertGreater(amount1, 0)

    def test_position_15857_counterexample_matches_independent_formula(self):
        liquidity = 21126254269852
        price = Decimal("1771.4431701141646")
        sqrt_price = price_to_sqrt_price_x96(price, 18, 6)

        amount0, amount1 = position_amounts(
            liquidity, -201970, -201070, sqrt_price,
        )

        expected0_raw = Decimal("0.011284654228689677") * Decimal(10**18)
        expected1_raw = Decimal("19.573854999999594") * Decimal(10**6)
        self.assertLess(
            abs(Decimal(amount0) - expected0_raw) / expected0_raw,
            Decimal("1e-9"),
        )
        self.assertLess(expected0_raw - Decimal(amount0), Decimal(1))
        # API 按 raw 单位向下取整，USDC 的 6 位精度会截去报告中的小数 raw。
        self.assertEqual(amount1, int(expected1_raw))
        self.assertLess(expected1_raw - Decimal(amount1), Decimal(1))
        actual_value = (
            Decimal(amount0) / Decimal(10**18) * price
            + Decimal(amount1) / Decimal(10**6)
        )
        floor_adjusted_value = (
            expected0_raw / Decimal(10**18) * price
            + Decimal(int(expected1_raw)) / Decimal(10**6)
        )
        self.assertLess(
            abs(actual_value - floor_adjusted_value) / floor_adjusted_value,
            Decimal("1e-9"),
        )
        self.assertLess(
            Decimal("39.5639786605") - actual_value,
            Decimal("0.0000011"),
        )


if __name__ == "__main__":
    unittest.main()
