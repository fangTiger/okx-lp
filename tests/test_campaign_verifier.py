import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.campaign.verifier import (
    VerificationError,
    VerificationReport,
    main,
    verify_campaign,
)
from okxlp.campaign.gate import load_fact_gate
from okxlp.config import load_config
from okxlp.exec.authorization import AuthorizationError, RunMode, load_run_mode
from okxlp.uniswap.pool import PoolSnapshot, TokenMetadata


def make_snapshot(config, *, pool_index=0, fee=500, token1_decimals=None):
    pool = config.pools[pool_index]
    selected_token1_decimals = (
        pool.token1.decimals
        if token1_decimals is None else token1_decimals
    )
    return PoolSnapshot(
        block=68886709,
        address=pool.address,
        factory=pool.factory,
        fee=fee,
        tick_spacing=10,
        sqrt_price_x96=3333962123355733730549486,
        tick=-201526,
        active_liquidity=14942241291635132,
        token0=TokenMetadata(
            pool.token0.address, pool.token0.symbol,
            pool.token0.name or pool.token0.symbol, pool.token0.decimals, 0,
        ),
        token1=TokenMetadata(
            pool.token1.address, pool.token1.symbol,
            pool.token1.name or pool.token1.symbol,
            selected_token1_decimals, 0,
        ),
    )


class FakeRpc:
    def __init__(self, no_code=()):
        self.no_code = {address.lower() for address in no_code}

    def ensure_chain_id(self):
        return 196

    def call(self, method, params):
        if method != "eth_getCode":
            raise AssertionError(f"非预期 RPC 方法：{method}")
        return "0x" if params[0].lower() in self.no_code else "0x6000"


class FakePool:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self):
        return self._snapshot


class CampaignVerifierTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config(Path("config/pools.yaml"))

    def _verify(self, config, snapshot, rpc=None):
        snapshots = {
            pool.address: make_snapshot(config, pool_index=index)
            for index, pool in enumerate(config.pools)
        }
        if isinstance(snapshot, dict):
            snapshots.update(snapshot)
        else:
            snapshots[config.pools[0].address] = snapshot
        return verify_campaign(
            config,
            rpc or FakeRpc(),
            pool_factory=lambda _rpc, address: FakePool(snapshots[address]),
        )

    def test_matching_chain_values_pass(self):
        report = self._verify(self.config, make_snapshot(self.config))

        self.assertEqual(
            report.verified_pool_ids, ("wASMLx_USDC", "wMRNAx_USDG")
        )
        self.assertEqual(report.block, 68886709)

    def test_wrong_fee_lists_configuration_and_chain_values(self):
        wrong_pool = replace(self.config.pools[0], fee_bps=Decimal("30"))
        wrong_config = replace(self.config, pools=(wrong_pool,))

        with self.assertRaises(VerificationError) as captured:
            self._verify(wrong_config, make_snapshot(self.config))

        message = str(captured.exception)
        self.assertIn("fee_bps", message)
        self.assertIn("配置值=30", message)
        self.assertIn("链上值=5", message)
        self.assertIn("拒绝启动", message)

    def test_token_code_and_decimals_differences_are_aggregated(self):
        pool = self.config.pools[0]
        snapshot = make_snapshot(self.config, token1_decimals=18)

        with self.assertRaises(VerificationError) as captured:
            self._verify(self.config, snapshot, FakeRpc(no_code=(pool.token0.address,)))

        message = str(captured.exception)
        self.assertIn("token0.has_code", message)
        self.assertIn("配置值=存在", message)
        self.assertIn("链上值=不存在", message)
        self.assertIn("token1.decimals", message)
        self.assertIn("配置值=6", message)
        self.assertIn("链上值=18", message)

    def test_main_reports_real_dry_run_mode(self):
        startup_result = (
            VerificationReport(("wASMLx_USDC",), 68886709),
            SimpleNamespace(forced_dry_run=False),
        )
        with (
            patch("sys.argv", ["verifier"]),
            patch("okxlp.campaign.verifier.run_startup", return_value=startup_result),
            patch(
                "okxlp.campaign.verifier.load_run_mode",
                return_value=RunMode.DRY_RUN,
            ),
            self.assertLogs("okxlp.campaign.verifier", level="INFO") as logs,
        ):
            result = main()

        self.assertEqual(result, 0)
        self.assertIn("模式=dry_run（禁止广播）", "\n".join(logs.output))

    def test_main_reports_real_live_mode(self):
        startup_result = (
            VerificationReport(("wASMLx_USDC",), 68886709),
            SimpleNamespace(forced_dry_run=False),
        )
        with (
            patch("sys.argv", ["verifier"]),
            patch("okxlp.campaign.verifier.run_startup", return_value=startup_result),
            patch(
                "okxlp.campaign.verifier.load_run_mode",
                return_value=RunMode.LIVE,
            ),
            self.assertLogs("okxlp.campaign.verifier", level="INFO") as logs,
        ):
            result = main()

        self.assertEqual(result, 0)
        self.assertIn("模式=live（可请求实盘）", "\n".join(logs.output))

    def test_live_mode_with_fact_blocker_never_reports_live_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            risk_path = root / "risk.yaml"
            facts_path = root / "facts.yaml"
            risk_path.write_text("mode: live\n", encoding="utf-8")
            facts_path.write_text(
                "facts:\n"
                "  - id: F8\n"
                "    name: 地域合规\n"
                "    verified: false\n"
                "    blocks: live\n",
                encoding="utf-8",
            )
            run_mode = load_run_mode(risk_path)
            gate = load_fact_gate(facts_path)
            startup_result = (
                VerificationReport(("wASMLx_USDC",), 68886709), gate,
            )
            with (
                patch("sys.argv", ["verifier"]),
                patch(
                    "okxlp.campaign.verifier.run_startup",
                    return_value=startup_result,
                ),
                patch(
                    "okxlp.campaign.verifier.load_run_mode",
                    return_value=run_mode,
                ),
                self.assertLogs(
                    "okxlp.campaign.verifier", level="INFO"
                ) as logs,
            ):
                result = main()

        output = "\n".join(logs.output)
        self.assertEqual(result, 0)
        self.assertIn(
            "模式=dry_run（事实闸门强制：存在 live 级未核实事实）",
            output,
        )
        self.assertNotIn("可请求实盘", output)

    def test_main_fails_before_rpc_when_run_mode_cannot_be_loaded(self):
        with (
            patch("sys.argv", ["verifier"]),
            patch("okxlp.campaign.verifier.run_startup") as startup,
            patch(
                "okxlp.campaign.verifier.load_run_mode",
                side_effect=AuthorizationError("模式配置损坏"),
            ),
        ):
            result = main()

        self.assertEqual(result, 2)
        startup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
