import tempfile
import textwrap
import sys
import unittest
from decimal import Decimal
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.config import ConfigError, load_config


VALID_CONFIG = """
chain:
  id: 196
  rpc: [https://rpc.xlayer.tech]
pools:
  - id: TEST_USDC
    enabled: true
    uniswap_version: v3
    address: "0x1111111111111111111111111111111111111111"
    quote_leg: token1
    token0:
      symbol: TEST
      address: "0x2222222222222222222222222222222222222222"
      decimals: 18
    token1:
      symbol: USDC
      address: "0x3333333333333333333333333333333333333333"
      decimals: 6
    fee_bps: 5
    tick_spacing: 10
    underlying: TEST
    listings:
      - venue: Euronext Amsterdam
        timezone: Europe/Amsterdam
        hours_local: "09:00-17:40"
session:
  fx_sunday_open:
    timezone: America/New_York
    local_time: "17:00"
    before_minutes: 30
    after_minutes: 30
"""


class ConfigTest(unittest.TestCase):
    def _load(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.yaml"
            path.write_text(textwrap.dedent(content), encoding="utf-8")
            return load_config(path)

    def test_loads_actual_config_into_frozen_dataclasses(self):
        config = load_config(Path("config/pools.yaml"))

        self.assertEqual(config.chain.chain_id, 196)
        self.assertEqual(config.pools[0].fee_bps, Decimal("5"))
        self.assertEqual(config.pools[0].listings[0].timezone, "Europe/Amsterdam")
        with self.assertRaises(AttributeError):
            config.chain.chain_id = 1

    def test_production_config_keeps_multiple_rpc_endpoints(self):
        config = load_config(Path("config/pools.yaml"))

        self.assertGreaterEqual(len(config.chain.rpc_urls), 2)

    def test_quote_leg_selects_quote_and_base_tokens(self):
        pool = self._load(VALID_CONFIG).find_pool()

        self.assertEqual(pool.quote_leg, "token1")
        self.assertEqual(pool.quote_token.symbol, "USDC")
        self.assertEqual(pool.base_token.symbol, "TEST")

    def test_quote_leg_is_required_and_rejects_invalid_values(self):
        variants = (
            VALID_CONFIG.replace("    quote_leg: token1\n", ""),
            VALID_CONFIG.replace("quote_leg: token1", "quote_leg: token2"),
            VALID_CONFIG.replace("quote_leg: token1", "quote_leg: 1"),
            VALID_CONFIG.replace("quote_leg: token1", "quote_leg: null"),
        )

        for content in variants:
            with self.subTest(content=content):
                with self.assertRaisesRegex(ConfigError, "quote_leg"):
                    self._load(content)

    def test_new_pool_can_be_selected_and_default_remains_first_pool(self):
        config = load_config(Path("config/pools.yaml"))

        self.assertEqual(config.find_pool().pool_id, "wASMLx_USDC")
        pool = config.find_pool("wMRNAx_USDG")
        self.assertEqual(
            pool.token0.address,
            "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8",
        )
        self.assertEqual(
            pool.token1.address,
            "0xce0fbc16e820ab7fd6d2936f1533c2654ad49ae9",
        )
        self.assertEqual(pool.quote_token.symbol, "USDG")
        self.assertEqual(pool.base_token.symbol, "wMRNAx")

    def test_actual_risk_config_contains_time_confirmation_thresholds(self):
        data = yaml.safe_load(Path("config/risk.yaml").read_text(encoding="utf-8"))

        outrange = data["outrange"]
        self.assertEqual(outrange["confirm_seconds"], 180)
        self.assertEqual(outrange["pin_timeout"], 600)
        self.assertEqual(set(outrange), {"confirm_seconds", "pin_timeout"})
        self.assertEqual(Decimal(str(data["swap"]["min_amount_usd"])), Decimal("1"))

    def test_example_risk_config_documents_simulation_value_floors(self):
        data = yaml.safe_load(
            Path("config/risk.example.yaml").read_text(encoding="utf-8")
        )

        self.assertEqual(data["limits"]["mint_min_deposit_bps"], 5000)
        self.assertEqual(data["limits"]["decrease_min_withdraw_bps"], 5000)

    def test_missing_field_reports_full_chinese_path(self):
        content = VALID_CONFIG.replace("      decimals: 18\n", "", 1)

        with self.assertRaisesRegex(ConfigError, "pools\\[0\\]\\.token0\\.decimals.*缺少"):
            self._load(content)

    def test_wrong_type_is_not_coerced(self):
        content = VALID_CONFIG.replace("  id: 196", "  id: true")

        with self.assertRaisesRegex(ConfigError, "chain.id.*整数"):
            self._load(content)

    def test_invalid_address_is_rejected(self):
        content = VALID_CONFIG.replace(
            "0x1111111111111111111111111111111111111111", "0x1234"
        )

        with self.assertRaisesRegex(ConfigError, "pools\\[0\\]\\.address.*地址格式非法"):
            self._load(content)

    def test_invalid_timezone_is_rejected(self):
        content = VALID_CONFIG.replace("Europe/Amsterdam", "Mars/Olympus")

        with self.assertRaisesRegex(ConfigError, "listings\\[0\\]\\.timezone.*时区"):
            self._load(content)


if __name__ == "__main__":
    unittest.main()
