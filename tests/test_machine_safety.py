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
        self.reference = Decimal("100")
        self.broadcasts = []
        self.rebalances = 0
        self.fail_enter = False

    def should_make_market(self, _now):
        return self.session

    def check(self, _now):
        return self.risk

    def snapshot(self, _now):
        return MarketSample(self.price, price_to_tick(self.price, 18, 18), self.reference)

    def enter(self, _sample, _band, *, allow_broadcast=False):
        self.broadcasts.append(allow_broadcast)
        if self.fail_enter:
            raise RuntimeError("注入建仓失败")

    def rebalance_actions(self, _sample, _band):
        return "actions"

    def exit(self, _sample, *, allow_broadcast=False):
        self.broadcasts.append(allow_broadcast)

    def execute(self, _actions, *, allow_broadcast=False):
        self.broadcasts.append(allow_broadcast)
        self.rebalances += 1


class MachineSafetyTest(unittest.TestCase):
    def machine(self, stub, root, *, detector=None, sleep=lambda _seconds: None, alerts=None):
        return MainStateMachine(
            pool_id="pool-1", sessions=stub, risk_gate=stub, market=stub,
            actions=stub, rebalancer=stub, detector=detector or OutrangeDetector(),
            state_store=MachineStateStore(root / "state.json"),
            transition_journal=TransitionJournal(root / "machine.log"),
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

    def test_pin_waits_until_timeout_before_rebalancing(self):
        with tempfile.TemporaryDirectory() as directory:
            stub = Stub()
            root = Path(directory)
            machine = self.machine(stub, root)
            machine.step()
            machine.step()
            for _index in range(3):
                stub.now += timedelta(seconds=60)
                machine.step()
            stub.price, stub.reference = Decimal("102"), Decimal("100")
            stub.now += timedelta(seconds=5)
            machine.step()
            stub.now += timedelta(seconds=5)
            pending = machine.step()
            stub.now += timedelta(seconds=594)
            before_timeout = machine.step()
            stub.now += timedelta(seconds=1)
            timed_out = machine.step()

            self.assertEqual(pending.state, MachineState.OUT_PENDING)
            self.assertEqual(before_timeout.state, MachineState.OUT_PENDING)
            self.assertEqual(timed_out.state, MachineState.REBALANCING)
            self.assertEqual(stub.rebalances, 0)
            self.assertIn("挂起超时", timed_out.reason)

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
