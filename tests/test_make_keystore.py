import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eth_account import Account

from okxlp.chain.signer import KeystoreSigner
from tools import make_keystore


class MakeKeystoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.out = Path(self.temporary.name) / "secrets" / "keystore.json"

    def tearDown(self):
        self.temporary.cleanup()

    def run_main(self, answers):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("tools.make_keystore.getpass.getpass", side_effect=answers),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = make_keystore.main(["--out", str(self.out)])
        return code, stdout.getvalue() + stderr.getvalue()

    def test_valid_random_key_creates_decryptable_private_file(self):
        account = Account.create()
        private_key = account.key.hex()
        password = "一次性测试口令-不要泄漏"

        code, output = self.run_main(
            ["0x" + private_key, password, password]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.out.parent.stat().st_mode), 0o700)
        with patch.dict(
            os.environ, {"OKXLP_KEYSTORE_PASSWORD": password}, clear=False
        ):
            signer = KeystoreSigner(self.out)
        self.assertEqual(signer.address.lower(), account.address.lower())
        self.assertIn(account.address, output)
        self.assertIn("OKXLP_KEYSTORE_PASSWORD", output)
        self.assertNotIn(private_key, output)
        self.assertNotIn(password, output)

    def test_invalid_private_key_format_is_rejected_without_leak(self):
        invalid = "not-a-private-key"

        code, output = self.run_main([invalid])

        self.assertNotEqual(code, 0)
        self.assertFalse(self.out.exists())
        self.assertIn("64 位十六进制", output)
        self.assertNotIn(invalid, output)

    def test_existing_file_is_not_overwritten(self):
        self.out.parent.mkdir(parents=True)
        self.out.write_text("keep-me", encoding="utf-8")

        code, output = self.run_main([])

        self.assertNotEqual(code, 0)
        self.assertEqual(self.out.read_text(encoding="utf-8"), "keep-me")
        self.assertIn("拒绝覆盖", output)

    def test_password_mismatch_is_rejected_without_leak(self):
        private_key = Account.create().key.hex()
        first = "first-secret"
        second = "second-secret"

        code, output = self.run_main([private_key, first, second])

        self.assertNotEqual(code, 0)
        self.assertFalse(self.out.exists())
        self.assertIn("两次输入的密码不一致", output)
        for secret in (private_key, first, second):
            self.assertNotIn(secret, output)


if __name__ == "__main__":
    unittest.main()
