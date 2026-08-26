import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from okxlp.exec.authorization import RunMode
from okxlp.strategy.machine_state import MachineState
from okxlp.strategy.machine_types import RiskDecision
from okxlp.strategy.nav import NavSnapshot
from tools.run_live import (
    LiveRuntime, RiskSettings, _approval_requirements,
    _ensure_startup_write_allowed, main,
)


OWNER = "0xb7394e865eb6f22df7aa199e59887e8aac0947a2"
OTHER_OWNER = "0x1111111111111111111111111111111111111111"


class FakeSigner:
    def __init__(self, address=OWNER):
        self.address = address
        self.closed = False
        self.refreshes = []

    def close(self):
        self.closed = True

    def refresh_token_ids(self, token_ids):
        self.refreshes.append(frozenset(token_ids))


class FakeRuntime:
    def __init__(self, signer):
        self.signer = signer
        self.calls = []

    def run(self, *, allow_broadcast, max_iterations):
        self.calls.append((allow_broadcast, max_iterations))

    def close(self):
        self.signer.close()


class FakeBootstrap:
    def __init__(self, signer=None):
        self.signer = signer or FakeSigner()
        self.current_price = Decimal("1771.4431701141646")
        self.finish_calls = []
        self.runtime = FakeRuntime(self.signer)

    def finish(self, *, allow_broadcast):
        self.finish_calls.append(allow_broadcast)
        return self.runtime

    def close(self):
        self.signer.close()


def settings():
    return RiskSettings(
        total_capital_usd=Decimal("49"),
        max_rebalances_per_day=30,
        halt_file=Path("log/HALT"),
        confirm_seconds=180,
        pin_timeout=600,
    )


class RunLiveGateTest(unittest.TestCase):
    def test_startup_approve_obeys_halt_but_preserves_exit_permission(self):
        class Gate:
            def __init__(self, decision):
                self.decision = decision

            def check(self, _now):
                return self.decision

        halted = Gate(RiskDecision(False, "人工急停", allow_exit=False))
        exit_only = Gate(RiskDecision(False, "次数触顶", allow_exit=True))

        with self.assertRaisesRegex(PermissionError, "人工急停"):
            _ensure_startup_write_allowed(halted, True)
        _ensure_startup_write_allowed(exit_only, True)
        _ensure_startup_write_allowed(halted, False)

    def test_exit_only_startup_approval_is_scoped_to_asset_router(self):
        policy = SimpleNamespace(
            token0="asset", token1="usdc", npm_address="npm",
            router_address="router",
            max_approval_raw={"asset": 100, "usdc": 200},
        )
        pool = SimpleNamespace(
            token0=SimpleNamespace(symbol="wASMLx"),
            token1=SimpleNamespace(symbol="USDC"),
        )
        active = SimpleNamespace(active_position=object())
        empty = SimpleNamespace(active_position=None)
        exit_only = RiskDecision(False, "次数触顶", allow_exit=True)
        allowed = RiskDecision(True, "放行", allow_exit=False)

        self.assertEqual(
            _approval_requirements(policy, pool, active, exit_only),
            (("asset", "router", 100),),
        )
        self.assertEqual(
            _approval_requirements(policy, pool, empty, exit_only), ()
        )
        self.assertEqual(
            len(_approval_requirements(policy, pool, active, allowed)), 4
        )

    def invoke(
        self, argv, *, mode, bootstrap=None, input_value="不确认"
    ):
        output = []
        calls = []

        def factory(*_args, **_kwargs):
            calls.append("bootstrap")
            return bootstrap or FakeBootstrap()

        code = main(
            argv,
            run_mode_loader=lambda: mode,
            risk_loader=settings,
            bootstrap_factory=factory,
            input_fn=lambda _prompt: input_value,
            printer=output.append,
        )
        return code, "\n".join(output), calls

    def test_dry_run_mode_with_broadcast_exits_before_any_rpc_setup(self):
        code, output, calls = self.invoke(
            ["--owner", OWNER, "--broadcast"],
            mode=RunMode.DRY_RUN,
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertIn("mode: dry_run", output)
        self.assertIn("config/risk.yaml", output)

    def test_wrong_interactive_confirmation_exits_without_broadcast(self):
        bootstrap = FakeBootstrap()

        code, output, _calls = self.invoke(
            ["--owner", OWNER, "--broadcast"],
            mode=RunMode.LIVE,
            bootstrap=bootstrap,
            input_value="确认",
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(bootstrap.finish_calls, [])
        self.assertEqual(bootstrap.runtime.calls, [])
        self.assertTrue(bootstrap.signer.closed)
        self.assertIn("我确认实盘", output)

    def test_without_broadcast_passes_exact_false_to_runtime(self):
        bootstrap = FakeBootstrap()

        code, _output, _calls = self.invoke(
            ["--owner", OWNER, "--max-iterations", "1"],
            mode=RunMode.DRY_RUN,
            bootstrap=bootstrap,
        )

        self.assertEqual(code, 0)
        self.assertEqual(bootstrap.finish_calls, [False])
        self.assertEqual(bootstrap.runtime.calls, [(False, 1)])
        self.assertIs(bootstrap.runtime.calls[0][0], False)
        self.assertTrue(bootstrap.signer.closed)

    def test_signer_owner_mismatch_exits_before_loop(self):
        bootstrap = FakeBootstrap(FakeSigner(OTHER_OWNER))

        code, output, _calls = self.invoke(
            ["--owner", OWNER],
            mode=RunMode.DRY_RUN,
            bootstrap=bootstrap,
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(bootstrap.finish_calls, [])
        self.assertEqual(bootstrap.runtime.calls, [])
        self.assertTrue(bootstrap.signer.closed)
        self.assertIn("signer.address", output)
        self.assertIn("owner", output)

    def test_broadcast_with_exact_confirmation_passes_true(self):
        bootstrap = FakeBootstrap()

        code, _output, _calls = self.invoke(
            ["--owner", OWNER, "--broadcast", "--max-iterations", "1"],
            mode=RunMode.LIVE,
            bootstrap=bootstrap,
            input_value="我确认实盘",
        )

        self.assertEqual(code, 0)
        self.assertEqual(bootstrap.finish_calls, [True])
        self.assertEqual(bootstrap.runtime.calls, [(True, 1)])

    def test_yes_skips_prompt_only_when_broadcast_was_requested(self):
        live_bootstrap = FakeBootstrap()
        with patch("builtins.input", side_effect=AssertionError("不应读取输入")):
            code, _output, _calls = self.invoke(
                ["--owner", OWNER, "--broadcast", "--yes"],
                mode=RunMode.LIVE,
                bootstrap=live_bootstrap,
            )
        self.assertEqual(code, 0)
        self.assertEqual(live_bootstrap.runtime.calls, [(True, None)])

        dry_bootstrap = FakeBootstrap()
        code, _output, _calls = self.invoke(
            ["--owner", OWNER, "--yes"],
            mode=RunMode.DRY_RUN,
            bootstrap=dry_bootstrap,
        )
        self.assertEqual(code, 0)
        self.assertEqual(dry_bootstrap.runtime.calls, [(False, None)])


class LiveRuntimeLoopTest(unittest.TestCase):
    def test_post_round_records_rebalance_refreshes_ids_and_records_nav(self):
        class Machine:
            state = MachineState.REBALANCING

            def __init__(self):
                self.calls = []

            def run(self, *, allow_broadcast, max_iterations):
                self.calls.append((allow_broadcast, max_iterations))
                self.state = MachineState.IN_RANGE

        class RiskGate:
            def __init__(self):
                self.calls = []

            def record_rebalance(self, now):
                self.calls.append(now)
                return 7

        class Recorder:
            def __init__(self):
                self.snapshots = []

            def record(self, snapshot):
                self.snapshots.append(snapshot)
                return True

        machine = Machine()
        gate = RiskGate()
        signer = FakeSigner()
        recorder = Recorder()
        output = []
        nav = NavSnapshot(
            ts="2026-08-26T00:00:00Z", block=1, price="1",
            position_value_usdc="2", idle0_raw=3, idle1_raw=4,
            total_usdc="5",
        )
        portfolio = SimpleNamespace(token_ids=frozenset({15_857, 15_858}))
        runtime = LiveRuntime(
            machine=machine, risk_gate=gate, signer=signer,
            reader=object(), rpc=object(), pool=object(), owner=OWNER,
            spenders=(), nav_recorder=recorder, token_ids={15_857},
            printer=output.append,
        )

        with patch("tools.run_live._nav_snapshot", return_value=(nav, portfolio)):
            runtime.run(allow_broadcast=False, max_iterations=1)

        self.assertEqual(machine.calls, [(False, 1)])
        self.assertEqual(len(gate.calls), 1)
        self.assertEqual(signer.refreshes, [portfolio.token_ids])
        self.assertEqual(recorder.snapshots, [nav])
        self.assertIn("第 7 次再平衡", "\n".join(output))


if __name__ == "__main__":
    unittest.main()
