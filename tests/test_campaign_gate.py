import logging
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.campaign.gate import FactConfigError, load_fact_gate


class CampaignGateTest(unittest.TestCase):
    def test_current_facts_allow_writes_but_cap_position_at_probe_value(self):
        gate = load_fact_gate(Path("config/facts.yaml"))
        statuses = {fact.fact_id: (fact.verified, fact.blocks) for fact in gate.facts}

        for fact_id in ("F1", "F6", "F7", "F8"):
            self.assertEqual(statuses[fact_id], (True, None))
        for fact_id in ("F4", "F5", "F9", "F10"):
            self.assertEqual(statuses[fact_id], ("n/a", None))
        for fact_id in ("F2", "F3"):
            self.assertEqual(statuses[fact_id], (False, "size"))
        self.assertFalse(gate.forced_dry_run)
        gate.ensure_write_allowed()
        self.assertEqual(
            gate.max_position_usd(Decimal("10000"), Decimal("2000")),
            Decimal("2000"),
        )
        self.assertEqual(
            gate.max_position_usd(Decimal("1000"), Decimal("2000")),
            Decimal("1000"),
        )

    def test_startup_log_reports_size_limit_without_claiming_forced_dry_run(self):
        gate = load_fact_gate(Path("config/facts.yaml"))

        with self.assertLogs("okxlp.campaign.gate", logging.WARNING) as captured:
            gate.log_startup()

        output = "\n".join(captured.output)
        self.assertIn("F2", output)
        self.assertIn("F3", output)
        self.assertIn("限制仓位", output)
        self.assertNotIn("强制 dry-run", output)

    def test_live_blocker_rejects_writes(self):
        gate = self._load(
            "facts:\n"
            "  - id: F8\n"
            "    name: 地域合规\n"
            "    verified: false\n"
            "    blocks: live\n"
        )

        self.assertTrue(gate.forced_dry_run)
        with self.assertRaisesRegex(PermissionError, "拒绝写链.*F8"):
            gate.ensure_write_allowed()

    def test_unverified_fact_must_declare_block_level(self):
        with self.assertRaisesRegex(FactConfigError, "blocks.*live 或 size"):
            self._load(
                "facts:\n"
                "  - id: F1\n"
                "    name: 合格池清单\n"
                "    verified: false\n"
            )

    def test_invalid_status_reports_chinese_error(self):
        with self.assertRaisesRegex(FactConfigError, "verified.*布尔值或 n/a"):
            self._load(
                "facts:\n"
                "  - id: F1\n"
                "    name: 合格池清单\n"
                "    verified: unknown\n"
            )

    @staticmethod
    def _load(content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "facts.yaml"
            path.write_text(content, encoding="utf-8")
            return load_fact_gate(path)


if __name__ == "__main__":
    unittest.main()
