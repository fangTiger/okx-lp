import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.exec.intent import Intent, IntentStatus
from okxlp.strategy.rebalance import (
    BalanceSnapshot,
    RebalanceActions,
    RebalanceError,
    RebalanceJournal,
    RebalanceOrchestrator,
    RebalanceProgress,
    calculate_50_50_swap,
)
from okxlp.uniswap.swap import ScheduledSwap, SwapQuote


TOKEN0 = "0x1111111111111111111111111111111111111111"
TOKEN1 = "0x2222222222222222222222222222222222222222"
TARGET = "0x3333333333333333333333333333333333333333"


def intent(label):
    selectors = {
        "burn": "0x0c49ccbe",
        "collect": "0xfc6f7865",
        "swap": "0x04e45aaf",
        "mint": "0x88316456",
    }
    result = Intent.create(TARGET, selectors[label])
    object.__setattr__(result, "transaction", {"label": label})
    return result


class RecordingExecutor:
    def __init__(self, events, fail_at=None):
        self.events = events
        self.fail_at = fail_at
        self.rpc_methods = []

    def execute(self, current, *, allow_broadcast=False):
        label = current.transaction["label"]
        self.events.append(f"execute_{label}:{allow_broadcast}")
        if allow_broadcast:
            self.rpc_methods.append("eth_sendRawTransaction")
        if label == self.fail_at:
            raise RuntimeError("注入失败")
        status = IntentStatus.CONFIRMED if allow_broadcast else IntentStatus.DRY_RUN
        return SimpleNamespace(intent=SimpleNamespace(status=status))


class RebalanceTest(unittest.TestCase):
    def test_token0_surplus_is_swapped_to_token1_for_50_50(self):
        requirement = calculate_50_50_swap(
            BalanceSnapshot(TOKEN0, TOKEN1, 10**18, 0, 18, 6, "100")
        )

        self.assertEqual(requirement.token_in, TOKEN0)
        self.assertEqual(requirement.token_out, TOKEN1)
        self.assertEqual(requirement.amount_in, 5 * 10**17)
        self.assertEqual(str(requirement.amount_usd), "50")

    def test_token1_surplus_uses_the_same_calculation_path(self):
        requirement = calculate_50_50_swap(
            BalanceSnapshot(TOKEN0, TOKEN1, 0, 100_000_000, 18, 6, "100")
        )

        self.assertEqual(requirement.token_in, TOKEN1)
        self.assertEqual(requirement.token_out, TOKEN0)
        self.assertEqual(requirement.amount_in, 50_000_000)
        self.assertEqual(str(requirement.amount_usd), "50")

    def test_sub_one_usd_dust_does_not_create_swap(self):
        requirement = calculate_50_50_swap(
            BalanceSnapshot(
                TOKEN0, TOKEN1, 20_000_800_000_000_000, 20_000_000,
                18, 6, "1000",
            )
        )

        self.assertIsNone(requirement)

    def test_executes_strict_order_and_reads_balances_after_collect(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            actions = self._actions(events)
            orchestrator = RebalanceOrchestrator(
                executor=RecordingExecutor(events),
                journal=RebalanceJournal(Path(directory)),
                sleep=lambda seconds: events.append(f"sleep_{seconds}"),
            )

            progress = orchestrator.execute(actions, rebalance_id="run-1")

        self.assertEqual(
            events,
            [
                "build_burn", "execute_burn:False",
                "build_collect", "execute_collect:False",
                "read_balances", "build_swap", "sleep_20", "execute_swap:False",
                "build_mint", "execute_mint:False",
            ],
        )
        self.assertEqual(progress.completed, ("burn", "collect", "swap", "mint"))
        self.assertIsNone(progress.failed_stage)

    def test_failure_stops_before_mint_and_persists_completed_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            journal = RebalanceJournal(Path(directory))
            orchestrator = RebalanceOrchestrator(
                executor=RecordingExecutor(events, fail_at="swap"),
                journal=journal,
                sleep=lambda seconds: events.append(f"sleep_{seconds}"),
            )

            with self.assertRaisesRegex(RebalanceError, "swap.*注入失败"):
                orchestrator.execute(self._actions(events), rebalance_id="run-fail")

            saved = json.loads((Path(directory) / "run-fail.json").read_text(encoding="utf-8"))

        self.assertNotIn("build_mint", events)
        self.assertEqual(saved["completed"], ["burn", "collect"])
        self.assertEqual(saved["failed_stage"], "swap")
        self.assertIn("注入失败", saved["error"])

    def test_non_boolean_broadcast_permissions_are_rejected_at_execute_entry(self):
        for index, value in enumerate((1, "true", object())):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                events = []
                executor = RecordingExecutor(events)
                orchestrator = RebalanceOrchestrator(
                    executor=executor,
                    journal=RebalanceJournal(Path(directory)),
                    sleep=lambda _seconds: None,
                )

                with self.assertRaises(TypeError):
                    orchestrator.execute(
                        self._actions(events), allow_broadcast=value,
                        rebalance_id=f"invalid-{index}",
                    )

                self.assertNotIn("eth_sendRawTransaction", executor.rpc_methods)

    def test_non_boolean_broadcast_permissions_are_rejected_at_stage_boundary(self):
        for index, value in enumerate((1, "true", object())):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                events = []
                executor = RecordingExecutor(events)
                orchestrator = RebalanceOrchestrator(
                    executor=executor,
                    journal=RebalanceJournal(Path(directory)),
                    sleep=lambda _seconds: None,
                )
                progress = RebalanceProgress(f"invalid-stage-{index}")

                with self.assertRaises(TypeError):
                    orchestrator._run_stage(
                        progress, "burn", ((intent("burn"), 0),), value
                    )

                self.assertNotIn("eth_sendRawTransaction", executor.rpc_methods)

    @staticmethod
    def _actions(events):
        def build(label):
            events.append(f"build_{label}")
            return intent(label)

        def read_balances():
            events.append("read_balances")
            return BalanceSnapshot(TOKEN0, TOKEN1, 10**18, 0, 18, 6, "100")

        def build_swap(requirement):
            events.append("build_swap")
            quote = SwapQuote(
                requirement.amount_in, 1, 1, 1, 0, 100000, requirement.amount_usd
            )
            return (ScheduledSwap(intent("swap"), quote, delay_seconds=20),)

        return RebalanceActions(
            burn=lambda: build("burn"),
            collect=lambda: build("collect"),
            read_balances=read_balances,
            build_swap=build_swap,
            mint=lambda: build("mint"),
        )


if __name__ == "__main__":
    unittest.main()
