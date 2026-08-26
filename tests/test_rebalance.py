import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.exec.intent import ID_PATTERN, Intent, IntentStatus
from okxlp.strategy.rebalance import (
    BalanceSnapshot,
    RebalanceActions,
    RebalanceError,
    RebalanceJournal,
    RebalanceOrchestrator,
    RebalanceProgress,
    calculate_50_50_swap,
    deterministic_intent_id,
)
from okxlp.uniswap.swap import ScheduledSwap, SwapQuote


TOKEN0 = "0x1111111111111111111111111111111111111111"
TOKEN1 = "0x2222222222222222222222222222222222222222"
TARGET = "0x3333333333333333333333333333333333333333"


def intent(label, intent_id=None):
    selectors = {
        "burn": "0x0c49ccbe",
        "collect": "0xfc6f7865",
        "swap": "0x04e45aaf",
        "mint": "0x88316456",
    }
    result = Intent.create(TARGET, selectors[label], intent_id=intent_id)
    object.__setattr__(result, "transaction", {"label": label})
    return result


class RecordingExecutor:
    def __init__(self, events, fail_at=None):
        self.events = events
        self.fail_at = fail_at
        self.rpc_methods = []

    def execute(
        self, current, *, allow_broadcast=False, simulation_check=None,
    ):
        label = current.transaction["label"]
        self.events.append(f"execute_{label}:{allow_broadcast}")
        if simulation_check is not None:
            simulation_check("0x")
        if allow_broadcast:
            self.rpc_methods.append("eth_sendRawTransaction")
        if label == self.fail_at:
            raise RuntimeError("注入失败")
        status = IntentStatus.CONFIRMED if allow_broadcast else IntentStatus.DRY_RUN
        return SimpleNamespace(intent=SimpleNamespace(status=status))


class RebalanceTest(unittest.TestCase):
    def test_journal_load_returns_none_for_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = RebalanceJournal(Path(directory))

            self.assertIsNone(journal.load("missing-run"))

    def test_journal_load_rejects_corrupted_or_invalid_progress(self):
        invalid_payloads = (
            b"{broken",
            b"\xff",
            json.dumps(
                {
                    "rebalance_id": "run-invalid",
                    "completed": "burn",
                    "intent_ids": [],
                    "failed_stage": None,
                    "error": None,
                }
            ).encode(),
            json.dumps(
                {
                    "rebalance_id": "run-invalid",
                    "completed": [],
                    "intent_ids": [],
                    "failed_stage": None,
                    "error": "不应存在的错误",
                }
            ).encode(),
        )
        for index, payload in enumerate(invalid_payloads):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                journal = RebalanceJournal(Path(directory))
                path = Path(directory) / "run-invalid.json"
                path.write_bytes(payload)

                with self.assertRaises(RebalanceError):
                    journal.load("run-invalid")

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

    def test_mint_stage_passes_its_simulation_check_to_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            actions = replace(
                self._actions(events),
                mint_simulation_check=lambda result: events.append(
                    f"check_mint:{result}"
                ),
            )
            orchestrator = RebalanceOrchestrator(
                executor=RecordingExecutor(events),
                journal=RebalanceJournal(Path(directory)),
                sleep=lambda _seconds: None,
            )

            orchestrator.execute(actions, rebalance_id="mint-check")

        self.assertIn("check_mint:0x", events)
        self.assertEqual(events[-2:], ["execute_mint:False", "check_mint:0x"])

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

    def test_failed_stage_restart_is_fail_closed_without_repeating_completed_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            journal = RebalanceJournal(Path(directory))
            first = RebalanceOrchestrator(
                executor=RecordingExecutor(events, fail_at="swap"),
                journal=journal,
                sleep=lambda _seconds: None,
            )
            with self.assertRaises(RebalanceError):
                first.execute(self._actions(events), rebalance_id="resume-failed")

            restarted = RebalanceOrchestrator(
                executor=RecordingExecutor(events), journal=journal,
                sleep=lambda _seconds: None,
            )
            with self.assertRaisesRegex(
                RebalanceError, "上一轮再平衡在 swap 阶段失败"
            ):
                restarted.execute(
                    self._actions(events), rebalance_id="resume-failed"
                )

        self.assertEqual(events.count("execute_burn:False"), 1)
        self.assertEqual(events.count("execute_collect:False"), 1)

    def test_restart_skips_completed_stages_and_runs_only_remaining_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            journal = RebalanceJournal(Path(directory))
            journal.save(
                RebalanceProgress(
                    "resume-before-swap", completed=("burn", "collect")
                )
            )
            orchestrator = RebalanceOrchestrator(
                executor=RecordingExecutor(events), journal=journal,
                sleep=lambda _seconds: None,
            )

            progress = orchestrator.execute(
                self._actions(events), rebalance_id="resume-before-swap"
            )

        self.assertNotIn("build_burn", events)
        self.assertNotIn("build_collect", events)
        self.assertNotIn("execute_burn:False", events)
        self.assertNotIn("execute_collect:False", events)
        self.assertIn("execute_swap:False", events)
        self.assertIn("execute_mint:False", events)
        self.assertEqual(progress.completed, ("burn", "collect", "swap", "mint"))

    def test_successful_restart_does_not_call_any_stage_callback_again(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            journal = RebalanceJournal(Path(directory))
            orchestrator = RebalanceOrchestrator(
                executor=RecordingExecutor(events), journal=journal,
                sleep=lambda _seconds: None,
            )
            actions = self._actions(events)
            first = orchestrator.execute(actions, rebalance_id="resume-success")
            first_events = tuple(events)

            second = orchestrator.execute(actions, rebalance_id="resume-success")

        self.assertEqual(tuple(events), first_events)
        self.assertEqual(second, first)

    def test_deterministic_intent_id_is_stable_unique_and_valid(self):
        first = deterministic_intent_id("rebalance-1", "swap", 0)

        self.assertEqual(
            first, deterministic_intent_id("rebalance-1", "swap", 0)
        )
        self.assertNotEqual(
            first, deterministic_intent_id("rebalance-1", "mint", 0)
        )
        self.assertNotEqual(
            first, deterministic_intent_id("rebalance-1", "swap", 1)
        )
        self.assertIsNotNone(ID_PATTERN.fullmatch(first))

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

    def test_only_none_rebalance_id_starts_a_new_round(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            orchestrator = RebalanceOrchestrator(
                executor=RecordingExecutor(events),
                journal=RebalanceJournal(Path(directory)),
                sleep=lambda _seconds: None,
            )

            with self.assertRaisesRegex(ValueError, "rebalance_id"):
                orchestrator.execute(self._actions(events), rebalance_id="")

        self.assertEqual(events, [])

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
        def build(label, intent_id):
            events.append(f"build_{label}")
            return intent(label, intent_id)

        def read_balances():
            events.append("read_balances")
            return BalanceSnapshot(TOKEN0, TOKEN1, 10**18, 0, 18, 6, "100")

        def build_swap(requirement, intent_ids):
            events.append("build_swap")
            quote = SwapQuote(
                requirement.amount_in, 1, 1, 1, 0, 100000, requirement.amount_usd
            )
            return (
                ScheduledSwap(
                    intent("swap", intent_ids[0]), quote, delay_seconds=20
                ),
            )

        return RebalanceActions(
            burn=lambda intent_id: build("burn", intent_id),
            collect=lambda intent_id: build("collect", intent_id),
            read_balances=read_balances,
            build_swap=build_swap,
            mint=lambda intent_id: build("mint", intent_id),
        )


if __name__ == "__main__":
    unittest.main()
