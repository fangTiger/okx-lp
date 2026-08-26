import inspect
import unittest
from dataclasses import replace
from decimal import Decimal, ROUND_FLOOR
from types import SimpleNamespace

from eth_abi import decode, encode

from okxlp.exec.approval import ApprovalPlan
from okxlp.exec.executor import ExecutionResult
from okxlp.exec.intent import Intent, IntentStatus
from okxlp.strategy.actions import ActionError, ProductionActions
from okxlp.strategy.allocation import calculate_50_50_swap
from okxlp.strategy.machine_state import PriceBand
from okxlp.strategy.machine_types import MarketSample
from okxlp.uniswap.portfolio import OwnedPosition, PortfolioSnapshot
from okxlp.uniswap.position import PositionManager
from okxlp.uniswap.swap import ScheduledSwap, SwapPolicy, SwapQuote
from okxlp.uniswap.tickmath import (
    mint_amounts_for_budget, position_amounts, price_to_sqrt_price_x96,
    price_to_tick, tick_to_price,
)


OWNER = "0xb7394e865eb6f22df7aa199e59887e8aac0947a2"
NPM = "0x315e413a11ab0df498ef83873012430ca36638ae"
ROUTER = "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca"
TOKEN0 = "0x9147b03c16b18fc4f686f610f189f91ddf4347b4"
TOKEN1 = "0xb6ceceab302e2e4948951ee7843fc24e92933061"
SAMPLE = MarketSample(
    Decimal("2000"), -201_526,
    price_to_sqrt_price_x96(Decimal("2000"), 18, 6),
)
FAILURE_SAMPLE = MarketSample(
    Decimal("1738"), -202_925,
    price_to_sqrt_price_x96(Decimal("1738"), 18, 6),
)
MINT_REGRESSION_PRICE = Decimal(
    "1745.3959081193072478579642945455192641995700571299668624074510048721507239987230"
)
MINT_REGRESSION_SAMPLE = MarketSample(
    MINT_REGRESSION_PRICE,
    price_to_tick(MINT_REGRESSION_PRICE, 18, 6),
    price_to_sqrt_price_x96(MINT_REGRESSION_PRICE, 18, 6),
)
BAND = PriceBand(-201_591, -201_463, Decimal("1990"), Decimal("2010"))
WIDE_BAND = PriceBand(
    BAND.tick_lower, BAND.tick_upper, Decimal("1700"), Decimal("2100")
)
MINT_REGRESSION_BAND = PriceBand(
    -201_730, -201_620, Decimal("1735"), Decimal("1755"),
)
INCIDENT_PRICE = Decimal("1756")
INCIDENT_TICK = price_to_tick(INCIDENT_PRICE, 18, 6)
INCIDENT_SAMPLE = MarketSample(
    INCIDENT_PRICE,
    INCIDENT_TICK,
    price_to_sqrt_price_x96(INCIDENT_PRICE, 18, 6),
)
INCIDENT_BAND = PriceBand(
    (INCIDENT_TICK // 10 - 5) * 10,
    (INCIDENT_TICK // 10 + 6) * 10,
    Decimal("1746"),
    Decimal("1766"),
)
POOL = SimpleNamespace(
    token0=SimpleNamespace(address=TOKEN0, symbol="wASMLx", decimals=18),
    token1=SimpleNamespace(address=TOKEN1, symbol="USDC", decimals=6),
    fee_bps=Decimal("5"),
    tick_spacing=10,
)


def owned_position(liquidity=21_126_254_269_852) -> OwnedPosition:
    return OwnedPosition(
        token_id=15_857,
        token0=TOKEN0,
        token1=TOKEN1,
        fee=500,
        tick_lower=-201_580,
        tick_upper=-201_470,
        liquidity=liquidity,
    )


def snapshot(*, balance0=0, balance1=500_000_000, positions=()):
    return PortfolioSnapshot(
        block=68_886_709,
        owner=OWNER,
        positions=tuple(positions),
        other_pool_position_count=0,
        balance0_raw=balance0,
        balance1_raw=balance1,
        allowances={},
    )


class SequenceReader:
    def __init__(self, *snapshots):
        self.snapshots = list(snapshots or (snapshot(),))
        self.calls = []

    def read(self, owner, *, spenders=()):
        self.calls.append((owner, tuple(spenders)))
        index = min(len(self.calls) - 1, len(self.snapshots) - 1)
        return self.snapshots[index]


class FixedFactGate:
    def __init__(self, limit):
        self.limit = Decimal(str(limit))
        self.calls = []

    def max_position_usd(self, configured, probe):
        self.calls.append((Decimal(str(configured)), Decimal(str(probe))))
        return self.limit


class FakeApprovalManager:
    def __init__(self):
        self.calls = []

    def plan(self, owner, requirements, *, intent_ids=None):
        requirements = tuple(requirements)
        self.calls.append((owner, requirements, intent_ids))
        plans = []
        for token, spender, needed in requirements:
            calldata = "0x095ea7b3" + encode(
                ["address", "uint256"], [spender, max(needed, 1)]
            ).hex()
            intent = Intent.create(token, calldata)
            plans.append(ApprovalPlan(token, spender, 0, max(needed, 1), intent))
        return tuple(plans)


class FakeSwapRouter:
    def __init__(self):
        self.router_address = ROUTER
        self.calls = []

    def plan_exact_input_single(self, **kwargs):
        self.calls.append(kwargs)
        amount_in = kwargs["amount_in"]
        if kwargs["token_in"] == TOKEN1:
            amount_out = max(1, amount_in * 10**12 // 2_000)
        else:
            amount_out = max(1, amount_in * 2_000 // 10**12)
        quote = SwapQuote(
            amount_in, amount_out, max(1, amount_out * 9_970 // 10_000),
            0, 0, 123_000, Decimal("30"),
        )
        params = (
            kwargs["token_in"], kwargs["token_out"], kwargs["fee"],
            kwargs["recipient"], amount_in, quote.amount_out_minimum, 0,
        )
        calldata = "0x04e45aaf" + encode(
            ["(address,address,uint24,address,uint256,uint256,uint160)"],
            [params],
        ).hex()
        intent_ids = kwargs.get("intent_ids")
        intent = Intent.create(
            ROUTER, calldata,
            intent_id=None if intent_ids is None else intent_ids[0],
        )
        return (ScheduledSwap(intent, quote),)


class CountingPositionManager:
    def __init__(self):
        self.address = NPM
        self.manager = PositionManager(NPM)
        self.calls = []

    def __getattr__(self, name):
        method = getattr(self.manager, name)

        def tracked(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return method(*args, **kwargs)

        return tracked


class RecordingExecutor:
    def __init__(self, fail_on=None):
        self.intents = []
        self.simulation_checks = []
        self.fail_on = fail_on

    def execute(
        self, intent, *, allow_broadcast=False, simulation_check=None,
    ):
        self.intents.append(intent)
        self.simulation_checks.append(simulation_check)
        if self.fail_on == len(self.intents):
            raise RuntimeError("注入执行失败")
        status = (
            IntentStatus.CONFIRMED if allow_broadcast else IntentStatus.DRY_RUN
        )
        completed = replace(intent, status=status)
        return ExecutionResult(completed, {})


def make_actions(
    *, reader=None, fact_limit=Decimal("100"), executor=None,
    approval_manager=None, position_manager=None, swap_router=None,
    dust_threshold_raw=10**12, pool_snapshot_reader=None,
):
    dependencies = SimpleNamespace(
        reader=reader or SequenceReader(),
        fact_gate=FixedFactGate(fact_limit),
        executor=executor or RecordingExecutor(),
        approval_manager=approval_manager or FakeApprovalManager(),
        position_manager=position_manager or CountingPositionManager(),
        swap_router=swap_router or FakeSwapRouter(),
    )
    actions = ProductionActions(
        executor=dependencies.executor,
        reader=dependencies.reader,
        approval_manager=dependencies.approval_manager,
        position_manager=dependencies.position_manager,
        swap_router=dependencies.swap_router,
        owner=OWNER,
        pool=POOL,
        fact_gate=dependencies.fact_gate,
        swap_policy=SwapPolicy(max_slippage_bps=Decimal("30")),
        deadline_seconds=300,
        clock=lambda: 2_000_000_000,
        dust_threshold_raw=dust_threshold_raw,
        pool_snapshot_reader=(
            (lambda: SAMPLE)
            if pool_snapshot_reader is None else pool_snapshot_reader
        ),
    )
    return actions, dependencies


def decode_mint(intent):
    return decode(
        ["(address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)"],
        bytes.fromhex(intent.calldata[10:]),
    )[0]


def decode_decrease(intent):
    return decode(
        ["(uint256,uint128,uint256,uint256,uint256)"],
        bytes.fromhex(intent.calldata[10:]),
    )[0]


def sample_at_tick(tick):
    price = tick_to_price(tick, 18, 6)
    return MarketSample(
        price, tick, price_to_sqrt_price_x96(price, 18, 6)
    )


def expected_minimum(value):
    return int(
        (
            Decimal(value) * (Decimal(10_000) - Decimal(30))
            / Decimal(10_000)
        ).to_integral_value(rounding=ROUND_FLOOR)
    )


class ProductionEnterTest(unittest.TestCase):
    @staticmethod
    def _asset_raw(amount):
        return int(Decimal(str(amount)) * Decimal(10**18))

    @staticmethod
    def _usdc_raw(amount):
        return int(Decimal(str(amount)) * Decimal(10**6))

    def test_pool_snapshot_reader_is_required_constructor_dependency(self):
        parameter = inspect.signature(ProductionActions).parameters[
            "pool_snapshot_reader"
        ]

        self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_fresh_enter_swaps_25_usdc_to_asset(self):
        reader = SequenceReader(snapshot(balance0=0, balance1=self._usdc_raw("199.69")))
        actions, dependencies = make_actions(
            reader=reader, fact_limit=50,
            pool_snapshot_reader=lambda: FAILURE_SAMPLE,
        )

        actions.enter(FAILURE_SAMPLE, WIDE_BAND)

        call = dependencies.swap_router.calls[0]
        self.assertEqual(len(dependencies.swap_router.calls), 1)
        self.assertEqual((call["token_in"], call["token_out"]), (TOKEN1, TOKEN0))
        self.assertEqual(call["amount_in"], self._usdc_raw("25"))
        self.assertEqual(call["amount_usd"], Decimal("25"))

    def test_fault_reentry_skips_swap_and_mints_existing_balances(self):
        asset_raw = self._asset_raw("0.01436427")
        reader = SequenceReader(
            snapshot(balance0=asset_raw, balance1=self._usdc_raw("174.69"))
        )
        actions, dependencies = make_actions(
            reader=reader, fact_limit=50,
            pool_snapshot_reader=lambda: FAILURE_SAMPLE,
        )

        actions.enter(FAILURE_SAMPLE, WIDE_BAND)

        self.assertEqual(dependencies.swap_router.calls, [])
        mint = decode_mint(dependencies.executor.intents[-1])
        expected_usdc = int(
            (
                (Decimal("50") - Decimal("0.01436427") * FAILURE_SAMPLE.price)
                * Decimal(10**6)
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        self.assertEqual(
            mint[5:7],
            mint_amounts_for_budget(
                asset_raw, expected_usdc,
                WIDE_BAND.tick_lower, WIDE_BAND.tick_upper,
                FAILURE_SAMPLE.sqrt_price_x96,
            ),
        )

    def test_asset_heavy_enter_sells_asset_for_usdc(self):
        reader = SequenceReader(
            snapshot(
                balance0=self._asset_raw("0.03"),
                balance1=self._usdc_raw("174.69"),
            )
        )
        actions, dependencies = make_actions(
            reader=reader, fact_limit=50,
            pool_snapshot_reader=lambda: FAILURE_SAMPLE,
        )

        actions.enter(FAILURE_SAMPLE, WIDE_BAND)

        call = dependencies.swap_router.calls[0]
        expected_raw = int(
            (Decimal("25") / FAILURE_SAMPLE.price * Decimal(10**18))
            .to_integral_value(rounding=ROUND_FLOOR)
        )
        self.assertEqual((call["token_in"], call["token_out"]), (TOKEN0, TOKEN1))
        self.assertEqual(call["amount_in"], expected_raw)
        self.assertEqual(
            call["amount_usd"],
            Decimal(expected_raw) / Decimal(10**18) * FAILURE_SAMPLE.price,
        )

    def test_asset_slightly_heavy_within_dust_skips_swap(self):
        asset_raw = int(
            (Decimal("25.5") / FAILURE_SAMPLE.price * Decimal(10**18))
            .to_integral_value(rounding=ROUND_FLOOR)
        )
        reader = SequenceReader(
            snapshot(balance0=asset_raw, balance1=self._usdc_raw("174.69"))
        )
        actions, dependencies = make_actions(
            reader=reader, fact_limit=50,
            pool_snapshot_reader=lambda: FAILURE_SAMPLE,
        )

        actions.enter(FAILURE_SAMPLE, WIDE_BAND)

        self.assertEqual(dependencies.swap_router.calls, [])

    def test_insufficient_usdc_narrows_capital_and_swaps_half(self):
        reader = SequenceReader(snapshot(balance0=0, balance1=self._usdc_raw("10")))
        actions, dependencies = make_actions(
            reader=reader, fact_limit=50,
            pool_snapshot_reader=lambda: FAILURE_SAMPLE,
        )

        actions.enter(FAILURE_SAMPLE, WIDE_BAND)

        call = dependencies.swap_router.calls[0]
        self.assertEqual((call["token_in"], call["token_out"]), (TOKEN1, TOKEN0))
        self.assertEqual(call["amount_in"], self._usdc_raw("5"))
        self.assertEqual(call["amount_usd"], Decimal("5"))

    def test_live_mint_uses_post_swap_actual_balances_capped_by_capital(self):
        after_swap_asset = self._asset_raw("0.014")
        after_swap_usdc = self._usdc_raw("25.5")
        reader = SequenceReader(
            snapshot(balance0=0, balance1=self._usdc_raw("199.69")),
            snapshot(balance0=after_swap_asset, balance1=after_swap_usdc),
        )
        actions, dependencies = make_actions(
            reader=reader, fact_limit=50,
            pool_snapshot_reader=lambda: FAILURE_SAMPLE,
        )

        actions.enter(FAILURE_SAMPLE, WIDE_BAND, allow_broadcast=True)

        mint = decode_mint(dependencies.executor.intents[-1])
        self.assertEqual(
            mint[5:7],
            mint_amounts_for_budget(
                after_swap_asset, after_swap_usdc,
                WIDE_BAND.tick_lower, WIDE_BAND.tick_upper,
                FAILURE_SAMPLE.sqrt_price_x96,
            ),
        )
        self.assertEqual(len(reader.calls), 2)
        self.assertEqual(len(dependencies.fact_gate.calls), 1)
        self.assertEqual(mint[7], 0)
        self.assertEqual(mint[8], 0)

    def test_mint_desired_uses_exact_band_ratio_within_wallet_budgets(self):
        budget0 = 14_364_270_543_869_171
        budget1 = 24_928_642
        reader = SequenceReader(snapshot(balance0=budget0, balance1=budget1))
        actions, dependencies = make_actions(
            reader=reader, fact_limit=100,
            pool_snapshot_reader=lambda: MINT_REGRESSION_SAMPLE,
        )

        actions.enter(MINT_REGRESSION_SAMPLE, MINT_REGRESSION_BAND)

        self.assertEqual(dependencies.swap_router.calls, [])
        mint = decode_mint(dependencies.executor.intents[-1])
        expected = mint_amounts_for_budget(
            budget0, budget1,
            MINT_REGRESSION_BAND.tick_lower,
            MINT_REGRESSION_BAND.tick_upper,
            MINT_REGRESSION_SAMPLE.sqrt_price_x96,
        )
        self.assertEqual(mint[5:7], expected)

    def test_incident_narrow_band_mint_uses_zero_per_leg_minimums(self):
        budget0 = 14_242_824_627_958_472
        budget1 = 24_880_000
        band = PriceBand(
            -201_710, -201_600,
            tick_to_price(-201_710, 18, 6),
            tick_to_price(-201_600, 18, 6),
        )
        baseline_price = Decimal("1747")
        baseline_sqrt = price_to_sqrt_price_x96(baseline_price, 18, 6)
        actions, _dependencies = make_actions()

        baseline = actions._mint_params(
            budget0, budget1, band, baseline_sqrt
        )

        self.assertEqual(baseline[2:], (0, 0))
        old_minimums = tuple(
            value * (10_000 - 30) // 10_000 for value in baseline[:2]
        )
        moved_amounts = {}
        for price in (
            Decimal("1745.25"), Decimal("1748.75"), Decimal("1740.01")
        ):
            with self.subTest(price=price):
                actual = mint_amounts_for_budget(
                    budget0, budget1, band.tick_lower, band.tick_upper,
                    price_to_sqrt_price_x96(price, 18, 6),
                )
                moved_amounts[price] = actual
                self.assertGreaterEqual(actual[0], baseline[2])
                self.assertGreaterEqual(actual[1], baseline[3])
        self.assertLess(
            moved_amounts[Decimal("1745.25")][1], old_minimums[1]
        )

    def test_mint_simulation_check_accepts_52_8_percent_and_rejects_40(self):
        budget0 = 14_242_824_627_958_472
        budget1 = 24_880_000
        actions, _dependencies = make_actions()
        check = actions._mint_simulation_check(
            budget0, budget1, Decimal("1740.01")
        )

        def result(amount0, amount1):
            return "0x" + encode(
                ["uint256", "uint128", "uint256", "uint256"],
                [15_857, 21_126_254_269_852, amount0, amount1],
            ).hex()

        moved_amounts = mint_amounts_for_budget(
            budget0, budget1, -201_710, -201_600,
            price_to_sqrt_price_x96(Decimal("1740.01"), 18, 6),
        )
        budget_usd = (
            Decimal(budget0) / Decimal(10**18) * Decimal("1740.01")
            + Decimal(budget1) / Decimal(10**6)
        )
        deposited_usd = (
            Decimal(moved_amounts[0]) / Decimal(10**18)
            * Decimal("1740.01")
            + Decimal(moved_amounts[1]) / Decimal(10**6)
        )
        self.assertAlmostEqual(
            deposited_usd / budget_usd,
            Decimal("0.528"),
            delta=Decimal("0.001"),
        )
        check(result(*moved_amounts))
        with self.assertRaisesRegex(
            ActionError, "实际存入.*预算.*5000 bps"
        ):
            check(result(
                budget0 * 4_000 // 10_000,
                budget1 * 4_000 // 10_000,
            ))

    def test_enter_mint_uses_fresh_pool_snapshot_price(self):
        budget0 = 14_242_824_627_958_472
        budget1 = 24_880_000
        baseline = MarketSample(
            Decimal("1747"), price_to_tick(Decimal("1747"), 18, 6),
            price_to_sqrt_price_x96(Decimal("1747"), 18, 6),
        )
        latest = MarketSample(
            Decimal("1748.75"), price_to_tick(Decimal("1748.75"), 18, 6),
            price_to_sqrt_price_x96(Decimal("1748.75"), 18, 6),
        )
        band = PriceBand(
            -201_710, -201_600,
            tick_to_price(-201_710, 18, 6),
            tick_to_price(-201_600, 18, 6),
        )
        actions, dependencies = make_actions(
            reader=SequenceReader(snapshot(balance0=budget0, balance1=budget1)),
            fact_limit=100, pool_snapshot_reader=lambda: latest,
        )

        actions.enter(baseline, band)

        mint = decode_mint(dependencies.executor.intents[-1])
        latest_expected = mint_amounts_for_budget(
            budget0, budget1, band.tick_lower, band.tick_upper,
            latest.sqrt_price_x96,
        )
        baseline_expected = mint_amounts_for_budget(
            budget0, budget1, band.tick_lower, band.tick_upper,
            baseline.sqrt_price_x96,
        )
        self.assertEqual(mint[5:7], latest_expected)
        self.assertNotEqual(mint[5:7], baseline_expected)
        self.assertIsNotNone(dependencies.executor.simulation_checks[-1])

    def test_enter_rejects_fresh_price_outside_band_without_mint_intent(self):
        latest = MarketSample(
            Decimal("1760"), price_to_tick(Decimal("1760"), 18, 6),
            price_to_sqrt_price_x96(Decimal("1760"), 18, 6),
        )
        position = CountingPositionManager()
        actions, dependencies = make_actions(
            reader=SequenceReader(snapshot(
                balance0=14_242_824_627_958_472, balance1=24_880_000,
            )),
            position_manager=position, fact_limit=100,
            pool_snapshot_reader=lambda: latest,
        )

        with self.assertRaisesRegex(
            ActionError, "价格已离开目标区间.*等待下一轮"
        ):
            actions.enter(MINT_REGRESSION_SAMPLE, MINT_REGRESSION_BAND)

        self.assertNotIn(
            "0x88316456",
            [intent.calldata[:10] for intent in dependencies.executor.intents],
        )
        self.assertFalse(any(name == "mint" for name, _args, _kwargs in position.calls))

    def test_mint_outside_range_allows_zero_desired_and_minimum_leg(self):
        sample = sample_at_tick(BAND.tick_lower - 10)
        actions, _dependencies = make_actions(fact_limit=100)

        mint = actions._mint_params(
            10**18, 100_000_000, BAND, sample.sqrt_price_x96
        )

        self.assertGreater(mint[0], 0)
        self.assertEqual(mint[1], 0)
        self.assertEqual(mint[2:], (0, 0))

    def test_zero_available_capital_fails_before_constructing_any_intent(self):
        approval = FakeApprovalManager()
        position = CountingPositionManager()
        swap = FakeSwapRouter()
        executor = RecordingExecutor()
        actions, dependencies = make_actions(
            fact_limit=0,
            approval_manager=approval,
            position_manager=position,
            swap_router=swap,
            executor=executor,
        )

        with self.assertRaisesRegex(
            ActionError,
            "可用本金为 0，请先在 config/risk.yaml 设置 limits.total_capital_usd",
        ):
            actions.enter(SAMPLE, BAND)

        self.assertEqual(approval.calls, [])
        self.assertEqual(position.calls, [])
        self.assertEqual(swap.calls, [])
        self.assertEqual(executor.intents, [])
        self.assertEqual(len(dependencies.reader.calls), 1)

    def test_all_approvals_execute_before_swap_and_mint(self):
        actions, dependencies = make_actions()

        actions.enter(SAMPLE, BAND)

        selectors = [intent.calldata[:10] for intent in dependencies.executor.intents]
        self.assertEqual(
            selectors,
            ["0x095ea7b3"] * 4 + ["0x04e45aaf", "0x88316456"],
        )
        requirements = dependencies.approval_manager.calls[0][1]
        self.assertEqual(
            tuple((token, spender) for token, spender, _needed in requirements),
            (
                (TOKEN1, ROUTER),
                (TOKEN0, ROUTER),
                (TOKEN1, NPM),
                (TOKEN0, NPM),
            ),
        )

    def test_mint_uses_zero_minimums_deadline_owner_and_exact_band(self):
        actions, dependencies = make_actions()
        in_range_sample = sample_at_tick(
            (BAND.tick_lower + BAND.tick_upper) // 2
        )

        actions.enter(in_range_sample, BAND)

        values = decode_mint(dependencies.executor.intents[-1])
        self.assertEqual(values[3:5], (BAND.tick_lower, BAND.tick_upper))
        self.assertEqual(values[7:9], (0, 0))
        self.assertEqual(values[9].lower(), OWNER)
        self.assertEqual(values[10], 2_000_000_300)

    def test_invalid_broadcast_types_fail_before_any_dependency_call(self):
        for invalid in (1, "true", object()):
            with self.subTest(invalid=invalid):
                actions, dependencies = make_actions()

                with self.assertRaises(TypeError):
                    actions.enter(SAMPLE, BAND, allow_broadcast=invalid)

                self.assertEqual(dependencies.reader.calls, [])
                self.assertEqual(dependencies.approval_manager.calls, [])
                self.assertEqual(dependencies.position_manager.calls, [])
                self.assertEqual(dependencies.swap_router.calls, [])
                self.assertEqual(dependencies.executor.intents, [])


class ProductionExitTest(unittest.TestCase):
    def test_in_range_decrease_uses_exact_slippage_minimums(self):
        position = owned_position(liquidity=21_126_254_269_852)
        sample = sample_at_tick(-201_525)
        reader = SequenceReader(
            snapshot(positions=(position,)),
            snapshot(positions=(owned_position(0),)),
        )
        actions, dependencies = make_actions(reader=reader)

        actions.exit(sample)

        values = decode_decrease(dependencies.executor.intents[0])
        expected0, expected1 = position_amounts(
            position.liquidity, position.tick_lower,
            position.tick_upper, sample.sqrt_price_x96,
        )
        self.assertGreater(values[2], 0)
        self.assertGreater(values[3], 0)
        self.assertEqual(values[2], expected_minimum(expected0))
        self.assertEqual(values[3], expected_minimum(expected1))

    def test_exit_orders_decrease_collect_swap_and_burn(self):
        reader = SequenceReader(
            snapshot(positions=(owned_position(),)),
            snapshot(balance0=2 * 10**18, positions=(owned_position(0),)),
        )
        actions, dependencies = make_actions(reader=reader)

        actions.exit(SAMPLE)

        self.assertEqual(
            [intent.calldata[:10] for intent in dependencies.executor.intents],
            ["0x0c49ccbe", "0xfc6f7865", "0x04e45aaf", "0x42966c68"],
        )
        self.assertEqual(
            dependencies.swap_router.calls[0]["amount_in"], 2 * 10**18
        )
        self.assertEqual(
            dependencies.swap_router.calls[0]["amount_usd"], Decimal("4000")
        )

    def test_exit_swaps_all_asset_even_when_value_exceeds_capital_limit(self):
        asset_raw = int(
            (
                Decimal("99.8") / INCIDENT_PRICE * Decimal(10**18)
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        reader = SequenceReader(
            snapshot(positions=(owned_position(),)),
            snapshot(
                balance0=asset_raw,
                balance1=99_960_000,
                positions=(owned_position(0),),
            ),
        )
        actions, dependencies = make_actions(reader=reader, fact_limit=50)

        actions.exit(INCIDENT_SAMPLE)

        self.assertEqual(
            dependencies.swap_router.calls[0]["amount_in"], asset_raw
        )

    def test_exit_skips_swap_when_actual_wasmlx_balance_is_zero(self):
        reader = SequenceReader(
            snapshot(positions=(owned_position(),)),
            snapshot(balance0=0, positions=(owned_position(0),)),
        )
        actions, dependencies = make_actions(reader=reader)

        actions.exit(SAMPLE)

        self.assertEqual(
            [intent.calldata[:10] for intent in dependencies.executor.intents],
            ["0x0c49ccbe", "0xfc6f7865", "0x42966c68"],
        )
        self.assertEqual(dependencies.swap_router.calls, [])

    def test_exit_rejects_invalid_broadcast_type_without_constructing_intent(self):
        for invalid in (1, "true", object()):
            with self.subTest(invalid=invalid):
                actions, dependencies = make_actions()

                with self.assertRaises(TypeError):
                    actions.exit(SAMPLE, allow_broadcast=invalid)

                self.assertEqual(dependencies.reader.calls, [])
                self.assertEqual(dependencies.position_manager.calls, [])
                self.assertEqual(dependencies.swap_router.calls, [])
                self.assertEqual(dependencies.executor.intents, [])

    def test_live_exit_rejects_remaining_exposure_above_dust(self):
        reader = SequenceReader(
            snapshot(positions=(owned_position(),)),
            snapshot(balance0=2 * 10**18, positions=(owned_position(0),)),
            snapshot(balance0=10**12 + 1),
        )
        actions, dependencies = make_actions(reader=reader)

        with self.assertRaisesRegex(ActionError, "剩余.*wASMLx.*敞口"):
            actions.exit(SAMPLE, allow_broadcast=True)

        self.assertEqual(len(dependencies.executor.intents), 4)
        self.assertEqual(len(reader.calls), 3)

    def test_executor_error_stops_all_later_exit_stages(self):
        executor = RecordingExecutor(fail_on=2)
        reader = SequenceReader(snapshot(positions=(owned_position(),)))
        actions, dependencies = make_actions(reader=reader, executor=executor)

        with self.assertRaisesRegex(ActionError, "collect.*注入执行失败"):
            actions.exit(SAMPLE)

        self.assertEqual(
            [intent.calldata[:10] for intent in dependencies.executor.intents],
            ["0x0c49ccbe", "0xfc6f7865"],
        )
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(dependencies.swap_router.calls, [])


class ProductionRebalanceActionsTest(unittest.TestCase):
    @staticmethod
    def _incident_portfolio(*, with_position=True):
        positions = (owned_position(),) if with_position else ()
        return snapshot(
            balance0=14_364_270_543_869_171,
            balance1=174_693_571,
            positions=positions,
        )

    def test_read_balances_caps_incident_wallet_to_fifty_dollars(self):
        portfolio = self._incident_portfolio()
        actions, _dependencies = make_actions(
            reader=SequenceReader(portfolio), fact_limit=50,
            pool_snapshot_reader=lambda: INCIDENT_SAMPLE,
        )

        balances = actions.rebalance_actions(
            INCIDENT_SAMPLE, INCIDENT_BAND
        ).read_balances()

        value = (
            Decimal(balances.amount0_raw) / Decimal(10**18) * INCIDENT_PRICE
            + Decimal(balances.amount1_raw) / Decimal(10**6)
        )
        self.assertLessEqual(value, Decimal("50"))
        self.assertNotEqual(
            (balances.amount0_raw, balances.amount1_raw),
            (portfolio.balance0_raw, portfolio.balance1_raw),
        )

    def test_incident_rebalance_swap_is_dust_after_capital_limit(self):
        actions, _dependencies = make_actions(
            reader=SequenceReader(self._incident_portfolio()), fact_limit=50
        )
        balances = actions.rebalance_actions(
            INCIDENT_SAMPLE, INCIDENT_BAND
        ).read_balances()

        requirement = calculate_50_50_swap(balances, Decimal("1"))
        precise = calculate_50_50_swap(balances, Decimal("0.01"))

        self.assertIsNone(requirement)
        self.assertIsNotNone(precise)
        self.assertLess(precise.amount_usd, Decimal("1"))

    def test_rebalance_mint_uses_band_ratio_and_zero_minimums(self):
        portfolio = self._incident_portfolio()
        actions, _dependencies = make_actions(
            reader=SequenceReader(portfolio), fact_limit=50,
            pool_snapshot_reader=lambda: INCIDENT_SAMPLE,
        )

        mint = decode_mint(
            actions.rebalance_actions(
                INCIDENT_SAMPLE, INCIDENT_BAND
            ).mint("4" * 32)
        )

        asset_value = (
            Decimal(portfolio.balance0_raw) / Decimal(10**18) * INCIDENT_PRICE
        )
        budget1 = int(
            ((Decimal("50") - asset_value) * Decimal(10**6))
            .to_integral_value(rounding=ROUND_FLOOR)
        )
        expected = mint_amounts_for_budget(
            portfolio.balance0_raw,
            budget1,
            INCIDENT_BAND.tick_lower,
            INCIDENT_BAND.tick_upper,
            INCIDENT_SAMPLE.sqrt_price_x96,
        )
        self.assertEqual(mint[5:7], expected)
        self.assertEqual(mint[7:9], (0, 0))

    def test_rebalance_mint_uses_fresh_pool_snapshot_price(self):
        portfolio = self._incident_portfolio()
        latest = MarketSample(
            Decimal("1760"), price_to_tick(Decimal("1760"), 18, 6),
            price_to_sqrt_price_x96(Decimal("1760"), 18, 6),
        )
        actions, _dependencies = make_actions(
            reader=SequenceReader(portfolio), fact_limit=50,
            pool_snapshot_reader=lambda: latest,
        )

        mint = decode_mint(
            actions.rebalance_actions(
                INCIDENT_SAMPLE, INCIDENT_BAND
            ).mint("7" * 32)
        )

        budget0, budget1 = actions._capital_budget(portfolio, latest.price)
        expected = mint_amounts_for_budget(
            budget0, budget1,
            INCIDENT_BAND.tick_lower, INCIDENT_BAND.tick_upper,
            latest.sqrt_price_x96,
        )
        initial_expected = mint_amounts_for_budget(
            budget0, budget1,
            INCIDENT_BAND.tick_lower, INCIDENT_BAND.tick_upper,
            INCIDENT_SAMPLE.sqrt_price_x96,
        )
        self.assertEqual(mint[5:7], expected)
        self.assertNotEqual(mint[5:7], initial_expected)

    def test_rebalance_rejects_fresh_price_outside_band_without_mint(self):
        latest = MarketSample(
            Decimal("1770"), price_to_tick(Decimal("1770"), 18, 6),
            price_to_sqrt_price_x96(Decimal("1770"), 18, 6),
        )
        position_manager = CountingPositionManager()
        actions, _dependencies = make_actions(
            reader=SequenceReader(self._incident_portfolio()),
            position_manager=position_manager, fact_limit=50,
            pool_snapshot_reader=lambda: latest,
        )
        callbacks = actions.rebalance_actions(
            INCIDENT_SAMPLE, INCIDENT_BAND
        )

        with self.assertRaisesRegex(
            ActionError, "价格已离开目标区间.*等待下一轮"
        ):
            callbacks.mint("8" * 32)

        self.assertFalse(any(
            name == "mint" for name, _args, _kwargs
            in position_manager.calls
        ))

    def test_enter_and_rebalance_build_identical_mint_params(self):
        portfolio = self._incident_portfolio(with_position=False)
        enter_actions, enter_dependencies = make_actions(
            reader=SequenceReader(portfolio), fact_limit=50,
            pool_snapshot_reader=lambda: INCIDENT_SAMPLE,
        )
        rebalance_actions, _dependencies = make_actions(
            reader=SequenceReader(
                replace(portfolio, positions=(owned_position(),))
            ),
            fact_limit=50,
            pool_snapshot_reader=lambda: INCIDENT_SAMPLE,
        )

        enter_actions.enter(INCIDENT_SAMPLE, INCIDENT_BAND)
        enter_mint = decode_mint(enter_dependencies.executor.intents[-1])
        rebalance_mint = decode_mint(
            rebalance_actions.rebalance_actions(
                INCIDENT_SAMPLE, INCIDENT_BAND
            ).mint("5" * 32)
        )

        self.assertEqual(enter_mint[5:9], rebalance_mint[5:9])

    def test_capital_budget_caps_asset_heavy_wallet(self):
        asset_raw = int(
            (
                Decimal("99.8") / INCIDENT_PRICE * Decimal(10**18)
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        portfolio = snapshot(balance0=asset_raw, balance1=99_960_000)
        actions, _dependencies = make_actions(fact_limit=50)

        budget0, budget1 = actions._capital_budget(
            portfolio, INCIDENT_PRICE
        )

        value = (
            Decimal(budget0) / Decimal(10**18) * INCIDENT_PRICE
            + Decimal(budget1) / Decimal(10**6)
        )
        self.assertLessEqual(budget0, portfolio.balance0_raw)
        self.assertLessEqual(value, Decimal("50"))
        self.assertLessEqual(Decimal("50") - value, Decimal("0.000001"))

    def test_asset_heavy_rebalance_mint_keeps_pre_swap_capital_slice(self):
        asset_raw = int(
            (
                Decimal("99.8") / INCIDENT_PRICE * Decimal(10**18)
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        before_swap = snapshot(
            balance0=asset_raw,
            balance1=99_960_000,
            positions=(owned_position(),),
        )
        reader = SequenceReader(before_swap, before_swap)
        actions, _dependencies = make_actions(
            reader=reader, fact_limit=50,
            pool_snapshot_reader=lambda: INCIDENT_SAMPLE,
        )
        callbacks = actions.rebalance_actions(
            INCIDENT_SAMPLE, INCIDENT_BAND
        )

        balances = callbacks.read_balances()
        requirement = calculate_50_50_swap(balances, Decimal("0.01"))
        self.assertIsNotNone(requirement)
        swaps = callbacks.build_swap(
            requirement,
            tuple(f"{index:032x}" for index in range(1, 6)),
        )
        quoted_received = sum(item.quote.amount_out for item in swaps)
        actual_received = quoted_received - 123
        reader.snapshots.append(
            snapshot(
                balance0=asset_raw - requirement.amount_in,
                balance1=99_960_000 + actual_received,
                positions=(owned_position(0),),
            )
        )

        mint = decode_mint(callbacks.mint("6" * 32))

        expected_budget0 = balances.amount0_raw - requirement.amount_in
        expected_budget1 = balances.amount1_raw + actual_received
        expected = mint_amounts_for_budget(
            expected_budget0,
            expected_budget1,
            INCIDENT_BAND.tick_lower,
            INCIDENT_BAND.tick_upper,
            INCIDENT_SAMPLE.sqrt_price_x96,
        )
        self.assertNotEqual(expected, (0, 0))
        self.assertEqual(mint[5:7], expected)
        self.assertEqual(mint[7:9], (0, 0))

    def test_below_range_decrease_allows_zero_token1_minimum(self):
        position = owned_position(liquidity=21_126_254_269_852)
        sample = sample_at_tick(position.tick_lower - 10)
        actions, _dependencies = make_actions(
            reader=SequenceReader(snapshot(positions=(position,)))
        )

        values = decode_decrease(
            actions.rebalance_actions(sample, BAND).burn("1" * 32)
        )

        self.assertGreater(values[2], 0)
        self.assertEqual(values[3], 0)

    def test_above_range_decrease_allows_zero_token0_minimum(self):
        position = owned_position(liquidity=21_126_254_269_852)
        sample = sample_at_tick(position.tick_upper + 10)
        actions, _dependencies = make_actions(
            reader=SequenceReader(snapshot(positions=(position,)))
        )

        values = decode_decrease(
            actions.rebalance_actions(sample, BAND).burn("2" * 32)
        )

        self.assertEqual(values[2], 0)
        self.assertGreater(values[3], 0)

    def test_nonzero_liquidity_never_encodes_two_zero_minimums(self):
        position = owned_position(liquidity=21_126_254_269_852)
        for tick in (
            position.tick_lower - 10,
            (position.tick_lower + position.tick_upper) // 2,
            position.tick_upper + 10,
        ):
            with self.subTest(tick=tick):
                sample = sample_at_tick(tick)
                actions, _dependencies = make_actions(
                    reader=SequenceReader(snapshot(positions=(position,)))
                )
                values = decode_decrease(
                    actions.rebalance_actions(sample, BAND).burn("3" * 32)
                )
                self.assertNotEqual(values[2:4], (0, 0))

    def test_callbacks_use_current_position_ids_balances_and_preallocated_ids(self):
        reader = SequenceReader(
            snapshot(
                balance0=2 * 10**18,
                balance1=3_000_000,
                positions=(owned_position(),),
            )
        )
        actions, dependencies = make_actions(reader=reader, fact_limit=5_000)
        callbacks = actions.rebalance_actions(SAMPLE, BAND)
        ids = tuple(f"{index:032x}" for index in range(1, 7))

        decrease = callbacks.burn(ids[0])
        collect = callbacks.collect(ids[1])
        balances = callbacks.read_balances()
        requirement = SimpleNamespace(
            token_in=TOKEN0,
            token_out=TOKEN1,
            amount_in=10**18,
            amount_usd=Decimal("2000"),
        )
        swaps = callbacks.build_swap(requirement, ids[2:])
        mint = callbacks.mint(ids[-1])

        decrease_values = decode(
            ["(uint256,uint128,uint256,uint256,uint256)"],
            bytes.fromhex(decrease.calldata[10:]),
        )[0]
        collect_values = decode(
            ["(uint256,address,uint128,uint128)"],
            bytes.fromhex(collect.calldata[10:]),
        )[0]
        self.assertEqual(
            decrease_values[:2], (15_857, owned_position().liquidity)
        )
        self.assertEqual(decrease.intent_id, ids[0])
        self.assertEqual(collect_values[:2], (15_857, OWNER))
        self.assertEqual(collect.intent_id, ids[1])
        self.assertEqual(balances.amount0_raw, 2 * 10**18)
        self.assertEqual(balances.amount1_raw, 3_000_000)
        self.assertEqual(balances.price_token1_per_token0, SAMPLE.price)
        self.assertEqual(swaps[0].intent.intent_id, ids[2])
        self.assertEqual(decode_mint(mint)[3:5], (BAND.tick_lower, BAND.tick_upper))
        self.assertEqual(mint.intent_id, ids[-1])
        self.assertEqual(
            dependencies.swap_router.calls[0]["intent_ids"], ids[2:]
        )


if __name__ == "__main__":
    unittest.main()
