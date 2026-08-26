import json
import hashlib
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.exec.intent import (
    Intent,
    IntentIntegrityError,
    IntentStatus,
    IntentStore,
    IntentStoreError,
)


TARGET = "0x" + "12" * 20


class ReceiptRpc:
    def __init__(self, receipts):
        self.receipts = receipts
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return self.receipts.get(params[0])


class IntentStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    @staticmethod
    def _signed(store, intent):
        simulated = store.save(replace(intent, status=IntentStatus.SIMULATED))
        return store.save(
            replace(simulated, status=IntentStatus.SIGNED, tx_hash="0x" + "11" * 32)
        )

    @classmethod
    def _sent(cls, store, intent, tx_hash):
        signed = cls._signed(store, intent)
        return store.save(replace(signed, status=IntentStatus.SENT, tx_hash=tx_hash))

    def test_intent_has_unique_id_utc_time_and_round_trips(self):
        first = Intent.create(TARGET, "0x88316456", value=7)
        second = Intent.create(TARGET, "0x88316456")
        store = IntentStore(self.root)

        persisted = store.persist(first)
        loaded = IntentStore(self.root).load(first.intent_id)

        self.assertNotEqual(first.intent_id, second.intent_id)
        self.assertIsNotNone(first.created_at.tzinfo)
        self.assertEqual(persisted.status, IntentStatus.PERSISTED)
        self.assertEqual(loaded, persisted)
        data = json.loads(next(self.root.glob("*.json")).read_text())
        self.assertEqual(data["value"], 7)
        content_hash = data.pop("content_hash")
        canonical = json.dumps(
            data, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        self.assertEqual(content_hash, hashlib.sha256(canonical.encode()).hexdigest())

    def test_same_id_is_idempotent_but_conflicting_content_is_rejected(self):
        store = IntentStore(self.root)
        intent = Intent.create(TARGET, "0x88316456")
        persisted = store.persist(intent)

        self.assertEqual(store.persist(intent), persisted)
        with self.assertRaisesRegex(IntentStoreError, "内容冲突"):
            store.persist(replace(intent, target="0x" + "34" * 20))

    def test_same_deterministic_id_ignores_recreated_timestamp(self):
        store = IntentStore(self.root)
        intent = Intent.create(
            TARGET, "0x88316456", intent_id="ab" * 16
        )
        persisted = store.persist(intent)
        recreated = replace(
            intent, created_at=intent.created_at + timedelta(seconds=30)
        )

        self.assertEqual(store.persist(recreated), persisted)

    def test_explicit_intent_id_is_validated(self):
        intent_id = "ab" * 16

        intent = Intent.create(
            TARGET, "0x88316456", intent_id=intent_id
        )

        self.assertEqual(intent.intent_id, intent_id)
        for invalid in ("AB" * 16, "a" * 31, "g" * 32, ""):
            with self.subTest(intent_id=invalid):
                with self.assertRaisesRegex(ValueError, "Intent ID"):
                    Intent.create(
                        TARGET, "0x88316456", intent_id=invalid
                    )

    def test_restart_loads_pending_and_reconciles_receipts(self):
        store = IntentStore(self.root)
        successful = store.persist(Intent.create(TARGET, "0x88316456"))
        failed = store.persist(Intent.create(TARGET, "0x88316456"))
        unknown = store.persist(Intent.create(TARGET, "0x88316456"))
        successful = self._sent(store, successful, "0xaaa")
        failed = self._sent(store, failed, "0xbbb")
        self._signed(store, replace(unknown, tx_hash="0xccc"))

        restarted = IntentStore(self.root)
        rpc = ReceiptRpc(
            {"0xaaa": {"status": "0x1"}, "0xbbb": {"status": "0x0"}, "0xccc": None}
        )
        reconciled = {item.intent_id: item for item in restarted.reconcile_pending(rpc)}

        self.assertEqual(reconciled[successful.intent_id].status, IntentStatus.CONFIRMED)
        self.assertEqual(reconciled[failed.intent_id].status, IntentStatus.FAILED)
        self.assertIn("链上交易执行失败", reconciled[failed.intent_id].error)
        self.assertEqual(reconciled[unknown.intent_id].status, IntentStatus.SIGNED)
        self.assertEqual(len(restarted.load_pending()), 1)

    def test_load_rejects_tampered_content_hash(self):
        store = IntentStore(self.root)
        intent = store.persist(Intent.create(TARGET, "0x88316456"))
        path = self.root / f"{intent.intent_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["value"] = 123456789
        path.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaisesRegex(IntentStoreError, "落盘内容完整性校验失败"):
            IntentStore(self.root).load(intent.intent_id)

    def test_integrity_error_keeps_intent_store_error_compatibility(self):
        self.assertTrue(issubclass(IntentIntegrityError, IntentStoreError))

    def test_illegal_status_transitions_are_rejected(self):
        store = IntentStore(self.root)
        persisted = store.persist(Intent.create(TARGET, "0x88316456"))
        with self.assertRaises(IntentStoreError):
            store.save(replace(persisted, status=IntentStatus.CONFIRMED))
        self.assertEqual(store.load(persisted.intent_id).status, IntentStatus.PERSISTED)

        confirmed = self._sent(store, persisted, "0xaaa")
        confirmed = store.save(replace(confirmed, status=IntentStatus.CONFIRMED))
        with self.assertRaises(IntentStoreError):
            store.save(replace(confirmed, status=IntentStatus.SENT))

        dry = store.persist(Intent.create(TARGET, "0x88316456"))
        dry = self._signed(store, dry)
        dry = store.save(replace(dry, status=IntentStatus.DRY_RUN))
        with self.assertRaises(IntentStoreError):
            store.save(replace(dry, status=IntentStatus.SENT))

    def test_same_status_save_is_rejected(self):
        store = IntentStore(self.root)
        persisted = store.persist(Intent.create(TARGET, "0x88316456"))

        with self.assertRaises(IntentStoreError):
            store.save(persisted)


if __name__ == "__main__":
    unittest.main()
