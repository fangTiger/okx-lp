import json
import logging
import os
import secrets
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from eth_account import Account

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.chain.signer import KeystoreError, KeystoreSigner


class KeystoreSignerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.account = Account.create()
        self.password = secrets.token_urlsafe(24)
        self.env_name = "OKXLP_TEST_KEYSTORE_PASSWORD"
        self.path = Path(self.directory.name) / "test-keystore.json"
        self.path.write_text(
            json.dumps(Account.encrypt(self.account.key, self.password)), encoding="utf-8"
        )

    def _signer(self):
        with patch.dict(os.environ, {self.env_name: self.password}, clear=False):
            return KeystoreSigner(self.path, password_env=self.env_name)

    def test_loads_temporary_keystore_and_signs_transaction(self):
        signer = self._signer()
        transaction = {
            "chainId": 196,
            "nonce": 0,
            "to": Account.create().address,
            "value": 0,
            "gas": 21_000,
            "maxFeePerGas": 20_000_000,
            "maxPriorityFeePerGas": 0,
            "type": 2,
        }

        raw = signer.sign_transaction(transaction)

        self.assertIsInstance(raw, bytes)
        self.assertEqual(signer.address, self.account.address)
        self.assertEqual(Account.recover_transaction(raw), self.account.address)

    def test_missing_password_environment_variable_is_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(KeystoreError, "环境变量"):
                KeystoreSigner(self.path, password_env=self.env_name)

    def test_wrong_password_has_clear_chinese_error(self):
        wrong = secrets.token_urlsafe(24)
        with patch.dict(os.environ, {self.env_name: wrong}, clear=False):
            with self.assertRaisesRegex(KeystoreError, "口令错误") as caught:
                KeystoreSigner(self.path, password_env=self.env_name)

        message = str(caught.exception)
        self.assertNotIn(wrong, message)
        self.assertNotIn(self.account.key.hex(), message)

    def test_private_key_never_appears_in_log_exception_or_repr(self):
        private_hex = self.account.key.hex()
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("okxlp.chain.signer")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

        signer = self._signer()
        rendered = repr(signer) + str(signer) + stream.getvalue()

        self.assertNotIn(private_hex, rendered)
        self.assertNotIn(self.password, rendered)
        self.assertNotIn("_private", rendered)


if __name__ == "__main__":
    unittest.main()
