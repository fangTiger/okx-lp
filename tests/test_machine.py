import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.strategy.machine import MainStateMachine, MarketSample, RiskDecision
from okxlp.strategy.machine_journal import TransitionJournal
from okxlp.strategy.machine_state import MachineState, MachineStateStore
from okxlp.strategy.machine_types import build_price_band
from okxlp.strategy.outrange import OutrangeDetector
from okxlp.uniswap.tickmath import price_to_tick


UTC = timezone.utc
START = datetime(2026, 8, 29, 8, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


class FakeSessions:
    def __init__(self, calls):
        self.calls = calls
        self.allowed = True
        self.reason = "允许做市"

    def should_make_market(self, _now):
        self.calls.append("session")
        return self.allowed, self.reason


class FakeRisk:
    def __init__(self, calls):
        self.calls = calls
        self.decision = RiskDecision(True, "风控放行")

    def check(self, _now):
        self.calls.append("risk")
        return self.decision


class FakeMarket:
    def __init__(self, calls):
        self.calls = calls
        self.set("100")

    def set(self, price):
        value = Decimal(price)
        self.sample = MarketSample(value, price_to_tick(value, 18, 18))

    def snapshot(self, _now):
        self.calls.append("market")
        return self.sample


class FakeActions:
    def __init__(self, calls):
        self.calls = calls
        self.enter_bands = []

    def enter(self, _sample, _band, *, allow_broadcast=False):
        self.calls.append(f"enter:{allow_broadcast}")
        self.enter_bands.append(_band)

    def rebalance_actions(self, _sample, _band):
        self.calls.append("build_rebalance")
        return "actions"

    def exit(self, _sample, *, allow_broadcast=False):
        self.calls.append(f"exit:{allow_broadcast}")


class FakeRebalancer:
    def __init__(self, calls):
        self.calls = calls

    def execute(self, actions, *, allow_broadcast=False):
        self.calls.append(f"rebalance:{actions}:{allow_broadcast}")


class MachineLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.calls = []
        self.clock = FakeClock()
        self.sessions = FakeSessions(self.calls)
        self.risk = FakeRisk(self.calls)
        self.market = FakeMarket(self.calls)
        root = Path(self.temporary.name)
        self.log_path = root / "machine.log"
        self.machine = MainStateMachine(
            pool_id="pool-1", sessions=self.sessions, risk_gate=self.risk,
            market=self.market, actions=FakeActions(self.calls),
            rebalancer=FakeRebalancer(self.calls), detector=OutrangeDetector(),
            state_store=MachineStateStore(root / "state.json"),
            transition_journal=TransitionJournal(self.log_path),
            clock=self.clock, sleep=lambda _seconds: None,
            tick_spacing=10, token0_decimals=18, token1_decimals=18,
        )

    def step(self, seconds=0):
        self.clock.now += timedelta(seconds=seconds)
        return self.machine.step()

    def test_replay_completes_full_lifecycle_with_correct_reasons(self):
        self.step()
        self.step(5)
        self.market.set("102")
        self.step(5)
        self.step(179)
        self.step(1)
        self.step(5)
        self.sessions.allowed = False
        self.sessions.reason = "上市地交易中"
        self.step(5)
        self.step(5)

        records = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            [item["new_state"] for item in records],
            ["ENTERING", "IN_RANGE", "OUT_PENDING", "REBALANCING", "IN_RANGE", "EXITING", "IDLE"],
        )
        expected = ("做市条件满足", "建仓完成", "池价越过区间上沿", "确认需要重组",
                    "再平衡完成", "离开做市时段", "撤出完成")
        for record, reason in zip(records, expected):
            self.assertIn(reason, record["reason"])
        self.assertIn("enter:False", self.calls)
        self.assertIn("rebalance:actions:False", self.calls)
        self.assertIn("exit:False", self.calls)
        self.assertEqual(self.machine.state, MachineState.IDLE)

    def test_each_cycle_checks_session_then_risk_before_strategy(self):
        self.step()

        self.assertEqual(self.calls[:3], ["session", "risk", "market"])

    def test_initial_band_uses_the_sample_exact_price(self):
        self.market.set("1.00009999")

        result = self.step()

        self.assertEqual(result.state, MachineState.ENTERING)
        self.assertEqual(
            (self.machine.band.tick_lower, self.machine.band.tick_upper),
            (-50, 60),
        )

    def test_entering_rebuilds_and_persists_band_from_current_price(self):
        self.market.set("100")
        self.step()
        stale_band = self.machine.band
        self.market.set("120")

        result = self.step(5)

        expected = build_price_band(Decimal("120"), 10, 18, 18)
        self.assertNotEqual(stale_band, expected)
        self.assertEqual(self.machine.actions.enter_bands[-1], expected)
        self.assertLessEqual(expected.price_lower, Decimal("120"))
        self.assertLess(Decimal("120"), expected.price_upper)
        self.assertEqual(result.state, MachineState.IN_RANGE)
        self.assertEqual(self.machine.band, expected)

    def test_in_range_keeps_minted_band_when_current_price_moves(self):
        self.market.set("100")
        self.step()
        self.step(5)
        minted_band = self.machine.band
        self.market.set("120")

        result = self.step(5)

        self.assertEqual(result.state, MachineState.OUT_PENDING)
        self.assertEqual(self.machine.band, minted_band)
        self.assertNotEqual(
            self.machine.band,
            build_price_band(Decimal("120"), 10, 18, 18),
        )


if __name__ == "__main__":
    unittest.main()
