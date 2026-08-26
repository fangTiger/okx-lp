import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from eth_abi import decode

from okxlp.chain.calldata_policy import CalldataPolicy, CalldataPolicyError
from okxlp.exec.approval import ApprovalError, ApprovalManager
from okxlp.uniswap.portfolio import PortfolioSnapshot


OWNER = "0xb7394e865eb6f22df7aa199e59887e8aac0947a2"
NPM = "0x315e413a11ab0df498ef83873012430ca36638ae"
ROUTER = "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca"
TOKEN0 = "0x9147b03c16b18fc4f686f610f189f91ddf4347b4"
TOKEN1 = "0xb6ceceab302e2e4948951ee7843fc24e92933061"
ATTACKER = "0x9999999999999999999999999999999999999999"
MAX_APPROVALS = {TOKEN0: 100 * 10**18, TOKEN1: 200_000 * 10**6}
TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "ensure_approvals.py"


class FakePortfolioReader:
    def __init__(self, allowances=None):
        self.calls = []
        self.snapshot = PortfolioSnapshot(
            block=68_886_709,
            owner=OWNER,
            positions=(),
            other_pool_position_count=0,
            balance0_raw=0,
            balance1_raw=0,
            allowances=allowances or {},
        )

    def read(self, owner, *, spenders=()):
        self.calls.append((owner, tuple(spenders)))
        return self.snapshot


class ApprovalManagerTest(unittest.TestCase):
    def policy(self):
        return CalldataPolicy(
            executor_address=OWNER,
            npm_address=NPM,
            router_address=ROUTER,
            token0=TOKEN0,
            token1=TOKEN1,
            fee=500,
            allowed_token_ids=frozenset(),
            max_approval_raw=MAX_APPROVALS,
        )

    def test_sufficient_allowance_returns_empty_plan(self):
        reader = FakePortfolioReader({(TOKEN0, NPM): 50})
        manager = ApprovalManager(reader=reader, policy=self.policy())

        plans = manager.plan(OWNER, [(TOKEN0, NPM, 50)])

        self.assertEqual(plans, ())
        self.assertEqual(reader.calls, [(OWNER, (NPM,))])

    def test_insufficient_allowance_plans_approve_to_token_limit(self):
        reader = FakePortfolioReader({(TOKEN0, NPM): 49})
        manager = ApprovalManager(reader=reader, policy=self.policy())
        intent_id = "1" * 32

        plans = manager.plan(
            OWNER,
            [(TOKEN0.upper().replace("0X", "0x"), NPM, 50)],
            intent_ids=[intent_id],
        )

        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(plan.token, TOKEN0)
        self.assertEqual(plan.spender, NPM)
        self.assertEqual(plan.current, 49)
        self.assertEqual(plan.target, MAX_APPROVALS[TOKEN0])
        self.assertEqual(plan.intent.intent_id, intent_id)
        self.assertEqual(plan.intent.target, TOKEN0)
        self.assertEqual(plan.intent.value, 0)
        self.assertEqual(plan.intent.calldata[:10], "0x095ea7b3")
        spender, amount = decode(
            ["address", "uint256"],
            bytes.fromhex(plan.intent.calldata[10:]),
        )
        self.assertEqual(spender.lower(), NPM)
        self.assertEqual(amount, MAX_APPROVALS[TOKEN0])

    def test_all_allowances_are_read_from_one_snapshot(self):
        reader = FakePortfolioReader()
        manager = ApprovalManager(reader=reader, policy=self.policy())

        plans = manager.plan(
            OWNER,
            [(TOKEN0, NPM, 1), (TOKEN1, ROUTER, 1)],
        )

        self.assertEqual(len(plans), 2)
        self.assertEqual(reader.calls, [(OWNER, (NPM, ROUTER))])

    def test_needed_above_limit_requires_manual_configuration_change(self):
        reader = FakePortfolioReader()
        manager = ApprovalManager(reader=reader, policy=self.policy())

        with self.assertRaisesRegex(
            ApprovalError, "需求超过配置上限.*人工提高上限"
        ):
            manager.plan(
                OWNER, [(TOKEN1, ROUTER, MAX_APPROVALS[TOKEN1] + 1)]
            )

        self.assertEqual(reader.calls, [])

    def test_unknown_spender_is_rejected_before_allowance_read(self):
        reader = FakePortfolioReader()
        manager = ApprovalManager(reader=reader, policy=self.policy())

        with self.assertRaisesRegex(ApprovalError, "spender"):
            manager.plan(OWNER, [(TOKEN0, ATTACKER, 1)])

        self.assertEqual(reader.calls, [])

    def test_generated_intent_must_pass_policy_self_check(self):
        manager = ApprovalManager(
            reader=FakePortfolioReader(), policy=self.policy()
        )

        with (
            patch.object(
                CalldataPolicy,
                "validate",
                autospec=True,
                side_effect=CalldataPolicyError("自检阻断"),
            ),
            self.assertRaisesRegex(CalldataPolicyError, "自检阻断"),
        ):
            manager.plan(OWNER, [(TOKEN0, NPM, 1)])


def load_approval_tool():
    spec = importlib.util.spec_from_file_location("ensure_approvals", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ApprovalToolTest(unittest.TestCase):
    def test_owner_argument_is_required(self):
        tool = load_approval_tool()

        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            tool.build_parser().parse_args([])

    def test_broadcast_is_rejected_before_any_rpc_access(self):
        tool = load_approval_tool()
        error = io.StringIO()

        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as caught:
            tool.main(["--owner", OWNER, "--broadcast"])

        self.assertNotEqual(caught.exception.code, 0)
        self.assertIn("广播需在生产入口接线完成后启用", error.getvalue())

    def test_report_contains_status_and_complete_approve_intent(self):
        tool = load_approval_tool()
        reader = FakePortfolioReader({
            (TOKEN0, NPM): MAX_APPROVALS[TOKEN0],
            (TOKEN1, ROUTER): 7,
        })
        manager = ApprovalManager(reader=reader, policy=ApprovalManagerTest().policy())
        requirements = (
            (TOKEN0, NPM, MAX_APPROVALS[TOKEN0]),
            (TOKEN1, ROUTER, MAX_APPROVALS[TOKEN1]),
        )
        plans = manager.plan(
            OWNER, requirements, intent_ids=("1" * 32, "2" * 32)
        )
        pool = SimpleNamespace(
            token0=SimpleNamespace(symbol="wASMLx", address=TOKEN0),
            token1=SimpleNamespace(symbol="USDC", address=TOKEN1),
        )

        output = tool.render_report(
            reader.snapshot,
            requirements=requirements,
            plans=plans,
            pool_config=pool,
            npm_address=NPM,
            router_address=ROUTER,
        )

        for expected in (
            "区块 68886709",
            "wASMLx -> NPM",
            f"current={MAX_APPROVALS[TOKEN0]}",
            "是否充足=是",
            "USDC -> SwapRouter02",
            "current=7",
            "是否充足=否",
            '"to": "' + TOKEN1 + '"',
            '"data": "0x095ea7b3',
            '"value": 0',
            '"intent_id": "' + "2" * 32 + '"',
        ):
            self.assertIn(expected, output)

    def test_tool_has_no_signing_or_sending_code_path(self):
        source = TOOL_PATH.read_text(encoding="utf-8")

        self.assertNotIn("send_raw_transaction", source)
        self.assertNotIn("sign_transaction", source)
        self.assertNotIn("RemoteSigner", source)


if __name__ == "__main__":
    unittest.main()
