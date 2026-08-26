import sys
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.strategy.machine import MainStateMachine, RiskDecision
from okxlp.strategy.machine_state import (
    MachineSnapshot, MachineState, MachineStateStore, PriceBand,
)
from okxlp.strategy.machine_journal import TransitionJournal
from okxlp.strategy.outrange import OutrangeDetector
from okxlp.uniswap.tickmath import aligned_tick_range, price_to_tick, tick_to_price
from tests.test_machine_safety import START, Stub


class FailOnceStateStore(MachineStateStore):
    def __init__(self, path):
        super().__init__(path)
        self.failed = False

    def save(self, snapshot):
        if not self.failed:
            self.failed = True
            raise OSError("注入状态落盘失败")
        super().save(snapshot)


class MachineRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stub = Stub()
        tick = price_to_tick(Decimal("100"), 18, 18)
        low, high = aligned_tick_range(tick, Decimal("0.005"), 10)
        self.band = PriceBand(
            low, high, tick_to_price(low, 18, 18), tick_to_price(high, 18, 18)
        )

    def machine(self, detector=None, state_store=None):
        return MainStateMachine(
            pool_id="pool-1", sessions=self.stub, risk_gate=self.stub,
            market=self.stub, actions=self.stub, rebalancer=self.stub,
            detector=detector or OutrangeDetector(),
            state_store=state_store or MachineStateStore(self.root / "state.json"),
            transition_journal=TransitionJournal(self.root / "machine.log"),
            clock=lambda: self.stub.now, sleep=lambda _seconds: None,
            alert=lambda _message: None, tick_spacing=10,
            token0_decimals=18, token1_decimals=18,
        )

    def save(self, snapshot):
        MachineStateStore(self.root / "state.json").save(snapshot)

    def test_truthy_non_boolean_broadcast_permission_is_rejected(self):
        self.save(MachineSnapshot(MachineState.ENTERING, self.band))
        machine = self.machine()

        with self.assertLogs("okxlp.strategy.machine", level="ERROR"):
            result = machine.step(allow_broadcast=1)

        self.assertEqual(result.state, MachineState.ENTERING)
        self.assertEqual(self.stub.broadcasts, [])

    def test_out_pending_timeout_survives_process_restart(self):
        self.save(MachineSnapshot(MachineState.OUT_PENDING, self.band, START, "ABOVE"))
        self.stub.now = START + timedelta(seconds=600)
        self.stub.price = Decimal("102")
        machine = self.machine(OutrangeDetector(confirm_seconds=900, pin_timeout=600))

        result = machine.step()

        self.assertEqual(result.state, MachineState.REBALANCING)
        self.assertIn("挂起超时", result.reason)
        self.assertEqual(self.stub.rebalances, 0)

    def test_out_pending_confirmation_timer_survives_process_restart(self):
        self.save(MachineSnapshot(MachineState.OUT_PENDING, self.band, START, "ABOVE"))
        self.stub.now = START + timedelta(seconds=180)
        self.stub.price = Decimal("102")
        machine = self.machine()

        result = machine.step()

        self.assertEqual(result.state, MachineState.REBALANCING)
        self.assertIn("持续位于界外", result.reason)

    def test_failed_state_save_restores_outside_detector_state(self):
        self.save(MachineSnapshot(MachineState.IN_RANGE, self.band))
        store = FailOnceStateStore(self.root / "state.json")
        machine = self.machine(state_store=store)
        self.stub.price = Decimal("102")

        with self.assertLogs("okxlp.strategy.machine", level="ERROR"):
            failed = machine.step()
        self.stub.price = Decimal("100")
        recovered = machine.step()

        self.assertEqual(failed.state, MachineState.IN_RANGE)
        self.assertEqual(recovered.state, MachineState.IN_RANGE)
        self.assertEqual(recovered.reason, "池价仍在区间内")

    def test_halt_freezes_exiting_without_forwarding_broadcast(self):
        self.save(MachineSnapshot(MachineState.EXITING, self.band))
        self.stub.risk = RiskDecision(False, "HALT 存在")
        machine = self.machine()

        result = machine.step(allow_broadcast=True)

        self.assertEqual(result.state, MachineState.EXITING)
        self.assertEqual(self.stub.broadcasts, [])
        self.assertIn("禁止撤出写链", result.reason)

    def test_leaving_market_exits_in_the_same_step(self):
        self.save(MachineSnapshot(MachineState.IN_RANGE, self.band))
        self.stub.session = (False, "上市地交易中")
        machine = self.machine()

        result = machine.step()

        self.assertEqual(result.state, MachineState.IDLE)
        self.assertEqual(self.stub.exit_calls, 1)
        self.assertIn("离开做市时段", result.reason)
        self.assertIn("撤出完成", result.reason)

    def test_new_exiting_state_does_not_exit_when_risk_forbids_it(self):
        self.save(MachineSnapshot(MachineState.IN_RANGE, self.band))
        self.stub.risk = RiskDecision(False, "HALT 存在", allow_exit=False)
        machine = self.machine()

        result = machine.step(allow_broadcast=True)

        self.assertEqual(result.state, MachineState.EXITING)
        self.assertEqual(self.stub.exit_calls, 0)
        self.assertIn("禁止撤出写链", result.reason)

    def test_circuit_breaker_can_explicitly_allow_defensive_exit(self):
        self.save(MachineSnapshot(MachineState.IN_RANGE, self.band))
        self.stub.risk = RiskDecision(False, "净值熔断", allow_exit=True)
        machine = self.machine()

        result = machine.step(allow_broadcast=True)

        self.assertEqual(result.state, MachineState.IDLE)
        self.assertEqual(self.stub.exit_calls, 1)
        self.assertEqual(self.stub.broadcasts, [True])


if __name__ == "__main__":
    unittest.main()
