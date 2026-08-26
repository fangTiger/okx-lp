import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from eth_account import Account

from okxlp.exec.authorization import RunMode
from okxlp.strategy.machine_state import (
    MachineSnapshot, MachineState, MachineStateStore, PriceBand,
)
from okxlp.strategy.machine_types import RiskDecision
from okxlp.strategy.nav import NavSnapshot
from okxlp.uniswap.tickmath import tick_to_price
from tools.run_live import (
    LiveRuntime, RiskSettings, _approval_requirements, build_parser,
    _ensure_startup_write_allowed, _sync_machine_state, main, parse_args,
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
        arguments = list(argv)
        if "--keystore" not in arguments and "--dotenv" not in arguments:
            arguments.extend(["--keystore", "secrets/test-keystore.json"])

        def factory(*_args, **_kwargs):
            calls.append("bootstrap")
            return bootstrap or FakeBootstrap()

        code = main(
            arguments,
            run_mode_loader=lambda: mode,
            risk_loader=settings,
            bootstrap_factory=factory,
            input_fn=lambda _prompt: input_value,
            printer=output.append,
        )
        return code, "\n".join(output), calls

    def test_key_source_flags_are_mutually_exclusive_at_argparse_layer(self):
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args([
                "--owner", OWNER,
                "--keystore", "temporary-keystore.json",
                "--dotenv", "temporary.env",
            ])

        self.assertNotEqual(caught.exception.code, 0)

    def test_missing_source_defaults_to_project_root_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dotenv = root / ".env"
            account = Account.create()
            dotenv.write_text(
                f"OKXLP_PRIVATE_KEY=0x{account.key.hex()}\n",
                encoding="utf-8",
            )
            dotenv.chmod(0o600)

            args = parse_args(["--owner", OWNER], project_root=root)

        self.assertIsNone(args.keystore)
        self.assertEqual(args.dotenv, dotenv.resolve())

    def test_missing_source_without_project_dotenv_is_argparse_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit) as caught:
                parse_args(["--owner", OWNER], project_root=Path(directory))

        self.assertNotEqual(caught.exception.code, 0)

    def test_dotenv_source_path_is_printed_in_banner(self):
        code, output, _calls = self.invoke(
            ["--owner", OWNER, "--dotenv", "temporary.env"],
            mode=RunMode.DRY_RUN,
        )

        self.assertEqual(code, 0)
        self.assertIn("dotenv", output.lower())
        self.assertIn("temporary.env", output)

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


class RunLiveStateSyncTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        path = Path(self.temporary.name) / "machine_state_pool-1.json"
        self.store = MachineStateStore(path)
        self.pool = SimpleNamespace(
            token0=SimpleNamespace(decimals=18),
            token1=SimpleNamespace(decimals=6),
        )
        self.old_band = PriceBand(
            -202_980, -202_870, Decimal("1729"), Decimal("1747")
        )
        self.position = SimpleNamespace(
            tick_lower=-201_760,
            tick_upper=-201_650,
            liquidity=123_456,
        )

    def save_transition(self, state):
        self.store.save(MachineSnapshot(state, self.old_band))

    def test_entering_without_position_resets_to_idle_with_warning(self):
        self.save_transition(MachineState.ENTERING)

        with self.assertLogs(level="WARNING") as captured:
            _sync_machine_state(self.store, None, self.pool)

        current = self.store.load()
        self.assertIs(current.state, MachineState.IDLE)
        self.assertIsNone(current.band)
        self.assertIn("本地 ENTERING 但链上无头寸", "\n".join(captured.output))

    def test_entering_with_position_resets_to_chain_band(self):
        self.save_transition(MachineState.ENTERING)

        with self.assertLogs(level="WARNING") as captured:
            _sync_machine_state(self.store, self.position, self.pool)

        current = self.store.load()
        self.assertIs(current.state, MachineState.IN_RANGE)
        self.assertEqual(current.band.tick_lower, self.position.tick_lower)
        self.assertEqual(current.band.tick_upper, self.position.tick_upper)
        self.assertEqual(
            current.band.price_lower,
            tick_to_price(self.position.tick_lower, 18, 6),
        )
        self.assertEqual(
            current.band.price_upper,
            tick_to_price(self.position.tick_upper, 18, 6),
        )
        self.assertNotEqual(current.band, self.old_band)
        self.assertIn("建仓已完成", "\n".join(captured.output))

    def test_exiting_with_position_stays_exiting(self):
        self.save_transition(MachineState.EXITING)
        before = self.store.path.read_bytes()

        with self.assertLogs(level="WARNING") as captured:
            _sync_machine_state(self.store, self.position, self.pool)

        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertIs(self.store.load().state, MachineState.EXITING)
        self.assertIn("撤出未完成", "\n".join(captured.output))

    def test_exiting_without_position_resets_to_idle(self):
        self.save_transition(MachineState.EXITING)

        with self.assertLogs(level="WARNING") as captured:
            _sync_machine_state(self.store, None, self.pool)

        self.assertIs(self.store.load().state, MachineState.IDLE)
        self.assertIn("撤出已完成", "\n".join(captured.output))

    def test_rebalancing_still_raises_original_error(self):
        self.save_transition(MachineState.REBALANCING)
        before = self.store.path.read_bytes()

        with self.assertRaisesRegex(
            RuntimeError,
            "本地状态停留在过渡阶段 REBALANCING，需人工对账",
        ):
            _sync_machine_state(self.store, None, self.pool)

        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
