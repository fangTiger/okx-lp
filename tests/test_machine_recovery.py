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

    def machine(self):
        return MainStateMachine(
            pool_id="pool-1", sessions=self.stub, risk_gate=self.stub,
            market=self.stub, actions=self.stub, rebalancer=self.stub,
            detector=OutrangeDetector(),
            state_store=MachineStateStore(self.root / "state.json"),
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
        self.stub.price, self.stub.reference = Decimal("102"), Decimal("100")
        machine = self.machine()

        result = machine.step()

        self.assertEqual(result.state, MachineState.REBALANCING)
        self.assertIn("挂起超时", result.reason)
        self.assertEqual(self.stub.rebalances, 0)

    def test_out_pending_basis_survives_restart_for_immediate_true_move(self):
        self.save(MachineSnapshot(
            MachineState.OUT_PENDING, self.band, START, "ABOVE",
            basis_ewma=Decimal("0"),
        ))
        self.stub.now = START + timedelta(seconds=10)
        self.stub.price = self.stub.reference = Decimal("102")
        machine = self.machine()

        result = machine.step()

        self.assertEqual(result.state, MachineState.REBALANCING)
        self.assertIn("确认真实移动", result.reason)

    def test_halt_freezes_exiting_without_forwarding_broadcast(self):
        self.save(MachineSnapshot(MachineState.EXITING, self.band))
        self.stub.risk = RiskDecision(False, "HALT 存在")
        machine = self.machine()

        result = machine.step(allow_broadcast=True)

        self.assertEqual(result.state, MachineState.EXITING)
        self.assertEqual(self.stub.broadcasts, [])
        self.assertIn("禁止撤出写链", result.reason)

    def test_circuit_breaker_can_explicitly_allow_defensive_exit(self):
        self.save(MachineSnapshot(MachineState.IN_RANGE, self.band))
        self.stub.risk = RiskDecision(False, "净值熔断", allow_exit=True)
        machine = self.machine()

        machine.step(allow_broadcast=True)
        result = machine.step(allow_broadcast=True)

        self.assertEqual(result.state, MachineState.IDLE)
        self.assertEqual(self.stub.broadcasts, [True])


if __name__ == "__main__":
    unittest.main()
