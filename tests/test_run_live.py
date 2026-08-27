import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from eth_account import Account

from okxlp.chain.calldata_policy import CalldataPolicy
from okxlp.exec.authorization import RunMode
from okxlp.strategy.machine_state import (
    MachineSnapshot, MachineState, MachineStateStore, PriceBand,
)
from okxlp.strategy.machine_types import RiskDecision
from okxlp.strategy.nav import NavSnapshot
from okxlp.uniswap.tickmath import tick_to_price
from tools.run_live import (
    IGNORE_SESSIONS_WARNING, LiveRuntime, RiskSettings, _approval_requirements,
    _banner, _ensure_startup_write_allowed, _market_sessions,
    _runtime_paths, _sync_machine_state, build_parser, main, parse_args,
)


OWNER = "0xb7394e865eb6f22df7aa199e59887e8aac0947a2"
OTHER_OWNER = "0x1111111111111111111111111111111111111111"
NPM = "0x315e413a11ab0df498ef83873012430ca36638ae"
ROUTER = "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca"
TOKEN0 = "0x9147b03c16b18fc4f686f610f189f91ddf4347b4"
TOKEN1 = "0xb6ceceab302e2e4948951ee7843fc24e92933061"


class FakeSigner:
    def __init__(self, address=OWNER, token_ids=None):
        self.address = address
        self.closed = False
        self.refreshes = []
        self.token_ids = None if token_ids is None else frozenset(token_ids)

    def close(self):
        self.closed = True

    def refresh_token_ids(self, token_ids):
        normalized = frozenset(token_ids)
        self.refreshes.append(normalized)
        self.token_ids = normalized


class FakeExecutor:
    def __init__(self, policy, *, failure=None):
        self.calldata_policy = policy
        self.failure = failure
        self.replacements = []

    def replace_calldata_policy(self, policy):
        self.replacements.append(policy)
        if self.failure is not None:
            raise self.failure
        self.calldata_policy = policy


class FakeMachine:
    def __init__(self, previous, current):
        self.state = previous
        self.current = current
        self.calls = []

    def run(self, *, allow_broadcast, max_iterations):
        self.calls.append((allow_broadcast, max_iterations))
        self.state = self.current


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


def runtime_policy(token_ids):
    return CalldataPolicy(
        executor_address=OWNER,
        npm_address=NPM,
        router_address=ROUTER,
        token0=TOKEN0,
        token1=TOKEN1,
        fee=500,
        allowed_token_ids=frozenset(token_ids),
        max_approval_raw={TOKEN0: 10**18, TOKEN1: 10**18},
    )


class RunLiveGateTest(unittest.TestCase):
    def test_pool_id_is_optional_and_selectable(self):
        base = ["--owner", OWNER, "--keystore", "temporary-keystore.json"]

        self.assertIsNone(build_parser().parse_args(base).pool_id)
        selected = build_parser().parse_args(
            [*base, "--pool-id", "wMRNAx_USDG"]
        )
        self.assertEqual(selected.pool_id, "wMRNAx_USDG")

    def test_runtime_paths_are_isolated_by_pool(self):
        first = _runtime_paths("wASMLx_USDC")
        second = _runtime_paths("wMRNAx_USDG")

        for field in (
            "machine_state", "transition_journal", "rebalance_journal",
            "rebalance_counter", "nav_root",
        ):
            self.assertNotEqual(getattr(first, field), getattr(second, field))
        self.assertIn("wMRNAx_USDG", str(second.rebalance_journal))
        self.assertIn("wMRNAx_USDG", str(second.nav_root))

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
        base = SimpleNamespace(address="asset", symbol="wASMLx")
        quote = SimpleNamespace(address="usdc", symbol="USDC")
        pool = SimpleNamespace(
            token0=base,
            token1=quote,
            base_token=base,
            quote_token=quote,
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

    def test_ignore_sessions_warns_and_is_forwarded_to_market_sessions(self):
        args = build_parser().parse_args([
            "--owner", OWNER,
            "--keystore", "temporary-keystore.json",
            "--ignore-sessions",
        ])
        output = []

        with patch("tools.run_live.LOGGER.warning") as warning:
            _banner(args, RunMode.DRY_RUN, settings(), False, output.append)
        with patch("tools.run_live.MarketSessions.from_files") as factory:
            _market_sessions(args, SimpleNamespace(pool_id="pool-1"))

        self.assertIs(args.ignore_sessions, True)
        self.assertIn(IGNORE_SESSIONS_WARNING, "\n".join(output))
        warning.assert_called_once_with(IGNORE_SESSIONS_WARNING)
        factory.assert_called_once_with(
            pool_id="pool-1", ignore_listings=True
        )

    def test_default_sessions_has_no_warning_and_forwards_false(self):
        args = build_parser().parse_args([
            "--owner", OWNER,
            "--keystore", "temporary-keystore.json",
        ])
        output = []

        with patch("tools.run_live.LOGGER.warning") as warning:
            _banner(args, RunMode.DRY_RUN, settings(), False, output.append)
        with patch("tools.run_live.MarketSessions.from_files") as factory:
            _market_sessions(args, SimpleNamespace(pool_id="pool-1"))

        self.assertIs(args.ignore_sessions, False)
        self.assertNotIn(IGNORE_SESSIONS_WARNING, "\n".join(output))
        warning.assert_not_called()
        factory.assert_called_once_with(
            pool_id="pool-1", ignore_listings=False
        )

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
    def _runtime(
        self, *, previous=MachineState.REBALANCING,
        current=MachineState.IN_RANGE, policy=None, executor=None,
        signer=None, chain_token_ids=frozenset({18_761}),
    ):
        selected_policy = policy or runtime_policy({18_720})
        selected_executor = executor or FakeExecutor(selected_policy)
        selected_signer = signer or FakeSigner(
            token_ids=selected_policy.allowed_token_ids
        )
        machine = FakeMachine(previous, current)
        gate = Mock()
        gate.record_rebalance.return_value = 7
        recorder = Mock()
        recorder.record.return_value = True
        reader = Mock()
        portfolio = SimpleNamespace(token_ids=frozenset(chain_token_ids))
        reader.read.return_value = portfolio
        nav = NavSnapshot(
            ts="2026-08-26T00:00:00Z", block=1, price="1",
            position_value_usdc="2", idle0_raw=3, idle1_raw=4,
            total_usdc="5",
        )

        def snapshot(_rpc, selected_reader, _pool, owner, spenders):
            return nav, selected_reader.read(owner, spenders=spenders)

        output = []
        runtime = LiveRuntime(
            machine=machine, risk_gate=gate, signer=selected_signer,
            executor=selected_executor, policy=selected_policy,
            reader=reader, rpc=object(), pool=object(), owner=OWNER,
            spenders=(), nav_recorder=recorder, printer=output.append,
        )
        return (
            runtime, machine, gate, selected_executor, selected_signer,
            recorder, reader, nav, portfolio, snapshot, output,
        )

    def test_rebalance_refreshes_main_and_signer_from_same_chain_ids(self):
        values = self._runtime()
        (
            runtime, machine, gate, executor, signer, recorder,
            _reader, nav, portfolio, snapshot, output,
        ) = values
        calls = []
        original = CalldataPolicy.with_token_ids

        def recording_with_token_ids(policy, token_ids):
            calls.append(frozenset(token_ids))
            return original(policy, token_ids)

        with (
            patch("tools.run_live._nav_snapshot", side_effect=snapshot),
            patch.object(
                CalldataPolicy, "with_token_ids",
                new=recording_with_token_ids,
            ),
        ):
            runtime.run(allow_broadcast=False, max_iterations=1)

        self.assertEqual(machine.calls, [(False, 1)])
        self.assertEqual(gate.record_rebalance.call_count, 1)
        self.assertEqual(calls, [portfolio.token_ids])
        self.assertEqual(
            [item.allowed_token_ids for item in executor.replacements],
            [portfolio.token_ids],
        )
        self.assertEqual(signer.refreshes, [portfolio.token_ids])
        self.assertEqual(runtime.policy.allowed_token_ids, portfolio.token_ids)
        recorder.record.assert_called_once_with(nav)
        self.assertIn("第 7 次再平衡", "\n".join(output))
        self.assertIn("[18720] → [18761]", "\n".join(output))

    def test_policy_derivation_failure_propagates_without_partial_update(self):
        values = self._runtime()
        runtime, _machine, _gate, executor, signer = values[:5]
        original = runtime.policy

        with (
            patch("tools.run_live._nav_snapshot", side_effect=values[9]),
            patch.object(
                CalldataPolicy, "with_token_ids",
                side_effect=RuntimeError("策略派生失败"),
            ),
            self.assertRaisesRegex(RuntimeError, "策略派生失败"),
        ):
            runtime.run(allow_broadcast=False, max_iterations=1)

        self.assertIs(runtime.policy, original)
        self.assertIs(executor.calldata_policy, original)
        self.assertEqual(signer.token_ids, original.allowed_token_ids)

    def test_executor_replacement_failure_propagates_without_partial_update(self):
        policy = runtime_policy({18_720})
        executor = FakeExecutor(policy, failure=RuntimeError("执行器替换失败"))
        values = self._runtime(policy=policy, executor=executor)
        runtime, _machine, _gate, _executor, signer = values[:5]

        with (
            patch("tools.run_live._nav_snapshot", side_effect=values[9]),
            self.assertRaisesRegex(RuntimeError, "执行器替换失败"),
        ):
            runtime.run(allow_broadcast=False, max_iterations=1)

        self.assertIs(runtime.policy, policy)
        self.assertIs(executor.calldata_policy, policy)
        self.assertEqual(signer.token_ids, policy.allowed_token_ids)

    def test_signer_refresh_failure_rolls_back_main_policy_and_propagates(self):
        class FailingSigner(FakeSigner):
            def refresh_token_ids(self, _token_ids):
                raise RuntimeError("签名刷新失败")

        policy = runtime_policy({18_720})
        signer = FailingSigner(token_ids=policy.allowed_token_ids)
        values = self._runtime(policy=policy, signer=signer)
        runtime, _machine, _gate, executor = values[:4]

        with (
            patch("tools.run_live._nav_snapshot", side_effect=values[9]),
            self.assertRaisesRegex(RuntimeError, "签名刷新失败"),
        ):
            runtime.run(allow_broadcast=False, max_iterations=1)

        self.assertIs(runtime.policy, policy)
        self.assertIs(executor.calldata_policy, policy)
        self.assertEqual(signer.token_ids, policy.allowed_token_ids)

    def test_steady_state_skips_token_sync_and_extra_portfolio_read(self):
        values = self._runtime(
            previous=MachineState.IN_RANGE,
            current=MachineState.IN_RANGE,
        )
        runtime, _machine, _gate, executor, signer = values[:5]
        reader = values[6]

        with (
            patch("tools.run_live._nav_snapshot", side_effect=values[9]),
            patch.object(
                CalldataPolicy, "with_token_ids",
                side_effect=AssertionError("稳态不应派生策略"),
            ),
        ):
            runtime.run(allow_broadcast=False, max_iterations=1)

        reader.read.assert_called_once_with(OWNER, spenders=())
        self.assertEqual(executor.replacements, [])
        self.assertEqual(signer.refreshes, [])


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
