import unittest
from dataclasses import replace
from decimal import Decimal, ROUND_FLOOR
from types import SimpleNamespace

from eth_abi import decode, encode

from okxlp.exec.approval import ApprovalPlan
from okxlp.exec.executor import ExecutionResult
from okxlp.exec.intent import Intent, IntentStatus
from okxlp.strategy.actions import ActionError, ProductionActions
from okxlp.strategy.machine_state import PriceBand
from okxlp.strategy.machine_types import MarketSample
from okxlp.uniswap.portfolio import OwnedPosition, PortfolioSnapshot
from okxlp.uniswap.position import PositionManager
from okxlp.uniswap.swap import ScheduledSwap, SwapPolicy, SwapQuote
from okxlp.uniswap.tickmath import (
    position_amounts, price_to_sqrt_price_x96, tick_to_price,
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
BAND = PriceBand(-201_591, -201_463, Decimal("1990"), Decimal("2010"))
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
        self.fail_on = fail_on

    def execute(self, intent, *, allow_broadcast=False):
        self.intents.append(intent)
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
    dust_threshold_raw=10**12,
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

    def test_mint_uses_nonzero_minimums_deadline_owner_and_exact_band(self):
        actions, dependencies = make_actions()

        actions.enter(SAMPLE, BAND)

        values = decode_mint(dependencies.executor.intents[-1])
        self.assertEqual(values[3:5], (BAND.tick_lower, BAND.tick_upper))
        self.assertGreater(values[7], 0)
        self.assertGreater(values[8], 0)
        self.assertEqual(values[9].lower(), OWNER)
        self.assertEqual(values[10], 2_000_000_300)
        self.assertEqual(
            values[7], values[5] * (10_000 - 30) // 10_000
        )
        self.assertEqual(
            values[8], values[6] * (10_000 - 30) // 10_000
        )

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
        actions, dependencies = make_actions(reader=reader)
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
