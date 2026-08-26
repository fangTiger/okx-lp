import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tools.dry_run_m7 import NoBroadcastActions


class DryRunM7ActionsTest(unittest.TestCase):
    def test_rebalance_action_callbacks_accept_preallocated_intent_ids(self):
        actions = NoBroadcastActions().rebalance_actions(
            SimpleNamespace(price=Decimal("1770")), SimpleNamespace()
        )
        ids = ("11" * 16, "22" * 16, "33" * 16)

        self.assertEqual(actions.burn(ids[0]).intent_id, ids[0])
        self.assertEqual(actions.collect(ids[1]).intent_id, ids[1])
        self.assertEqual(actions.mint(ids[2]).intent_id, ids[2])


if __name__ == "__main__":
    unittest.main()
