import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.strategy.machine import MainStateMachine, MarketSample, RiskDecision
from okxlp.strategy.machine_state import (
    MachineSnapshot, MachineState, MachineStateStore, PriceBand,
)
from okxlp.strategy.machine_journal import TransitionJournal
from okxlp.strategy.outrange import OutrangeDetector
from okxlp.uniswap.tickmath import aligned_tick_range, price_to_tick, tick_to_price


UTC = timezone.utc
START = datetime(2026, 8, 29, 8, 0, tzinfo=UTC)


class Stub:
    def __init__(self):
        self.now = START
        self.session = (True, "允许做市")
        self.risk = RiskDecision(True, "风控放行")
        self.price = Decimal("100")
        self.broadcasts = []
        self.rebalances = 0
        self.exit_calls = 0
        self.fail_enter = False

    def should_make_market(self, _now):
        return self.session

    def check(self, _now):
        return self.risk

    def snapshot(self, _now):
        return MarketSample(self.price, price_to_tick(self.price, 18, 18))

    def enter(self, _sample, _band, *, allow_broadcast=False):
        self.broadcasts.append(allow_broadcast)
        if self.fail_enter:
            raise RuntimeError("注入建仓失败")

    def rebalance_actions(self, _sample, _band):
        return "actions"

    def exit(self, _sample, *, allow_broadcast=False):
        self.exit_calls += 1
        self.broadcasts.append(allow_broadcast)

    def execute(self, _actions, *, allow_broadcast=False):
        self.broadcasts.append(allow_broadcast)
        self.rebalances += 1


class FailOnceJournal:
    def __init__(self):
        self.failed = False

    def append(self, _record):
        if not self.failed:
            self.failed = True
            raise OSError("注入转移日志失败")


class MachineSafetyTest(unittest.TestCase):
    def machine(
        self, stub, root, *, detector=None, sleep=lambda _seconds: None,
        alerts=None, transition_journal=None,
    ):
        return MainStateMachine(
            pool_id="pool-1", sessions=stub, risk_gate=stub, market=stub,
            actions=stub, rebalancer=stub, detector=detector or OutrangeDetector(),
            state_store=MachineStateStore(root / "state.json"),
            transition_journal=(
                transition_journal or TransitionJournal(root / "machine.log")
            ),
            clock=lambda: stub.now, sleep=sleep,
            alert=(alerts if alerts is not None else []).append,
            tick_spacing=10, token0_decimals=18, token1_decimals=18,
        )

    def test_non_market_session_never_enters(self):
        with tempfile.TemporaryDirectory() as directory:
            stub = Stub()
            stub.session = (False, "上市地交易中")
            machine = self.machine(stub, Path(directory))

            result = machine.step()

        self.assertEqual(machine.state, MachineState.IDLE)
        self.assertIn("保持 IDLE", result.reason)
        self.assertEqual(stub.broadcasts, [])

    def test_outside_waits_until_confirmation_before_rebalancing(self):
        with tempfile.TemporaryDirectory() as directory:
            stub = Stub()
            root = Path(directory)
            machine = self.machine(stub, root)
            machine.step()
            machine.step()
            stub.price = Decimal("102")
            stub.now += timedelta(seconds=5)
            machine.step()
            stub.now += timedelta(seconds=179)
            pending = machine.step()
            stub.now += timedelta(seconds=1)
            confirmed = machine.step()

            self.assertEqual(pending.state, MachineState.OUT_PENDING)
            self.assertEqual(confirmed.state, MachineState.REBALANCING)
            self.assertEqual(stub.rebalances, 0)
            self.assertIn("持续位于界外", confirmed.reason)

    def test_action_failure_stays_in_current_state_and_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            stub, alerts = Stub(), []
            machine = self.machine(stub, Path(directory), alerts=alerts)
            machine.step()
            stub.fail_enter = True

            with self.assertLogs("okxlp.strategy.machine", level="ERROR"):
                result = machine.step()
            stub.fail_enter = False
            held = machine.step()
            restarted = self.machine(stub, Path(directory))
            held_after_restart = restarted.step()

            self.assertEqual(machine.state, MachineState.ENTERING)
            self.assertIn("步骤失败", result.reason)
            self.assertIn("注入建仓失败", alerts[0])
            self.assertIn("阶段已锁停", held.reason)
            self.assertIn("阶段已锁停", held_after_restart.reason)
            self.assertEqual(stub.broadcasts, [False])

    def test_failed_transition_restores_outside_detector_state(self):
        with tempfile.TemporaryDirectory() as directory:
            stub, root = Stub(), Path(directory)
            tick = price_to_tick(Decimal("100"), 18, 18)
            low, high = aligned_tick_range(tick, Decimal("0.005"), 10)
            band = PriceBand(
                low, high, tick_to_price(low, 18, 18), tick_to_price(high, 18, 18),
            )
            MachineStateStore(root / "state.json").save(
                MachineSnapshot(MachineState.IN_RANGE, band)
            )
            machine = self.machine(
                stub, root, transition_journal=FailOnceJournal(),
            )
            stub.price = Decimal("102")

            with self.assertLogs("okxlp.strategy.machine", level="ERROR"):
                failed = machine.step()
            stub.price = Decimal("100")
            recovered = machine.step()

        self.assertEqual(failed.state, MachineState.IN_RANGE)
        self.assertEqual(recovered.state, MachineState.IN_RANGE)
        self.assertEqual(recovered.reason, "池价仍在区间内")

    def test_run_uses_fast_and_slow_intervals_and_broadcast_defaults_off(self):
        with tempfile.TemporaryDirectory() as directory:
            stub, sleeps = Stub(), []
            root = Path(directory)
            machine = self.machine(stub, root, sleep=sleeps.append)
            stub.session = (False, "上市地交易中")
            machine.run(max_iterations=1)
            stub.session = (True, "允许做市")
            machine.run(max_iterations=2)

            self.assertEqual(sleeps, [60, 5, 5])
            self.assertEqual(stub.broadcasts, [False])

    def test_explicit_run_broadcast_permission_is_forwarded(self):
        with tempfile.TemporaryDirectory() as directory:
            stub, root = Stub(), Path(directory)
            tick = price_to_tick(Decimal("100"), 18, 18)
            low, high = aligned_tick_range(tick, Decimal("0.005"), 10)
            band = PriceBand(low, high, tick_to_price(low, 18, 18), tick_to_price(high, 18, 18))
            MachineStateStore(root / "state.json").save(MachineSnapshot(MachineState.ENTERING, band))
            machine = self.machine(stub, root)

            machine.run(allow_broadcast=True, max_iterations=1)

            self.assertEqual(stub.broadcasts, [True])

if __name__ == "__main__":
    unittest.main()
