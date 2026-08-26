import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from eth_account import Account

from okxlp.chain.dotenv import DotenvError, load_private_key


class DotenvTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / ".env"
        self.account = Account.create()
        self.private_hex = self.account.key.hex()

    def _write(self, content: str, mode: int = 0o600) -> None:
        self.path.write_text(content, encoding="utf-8")
        os.chmod(self.path, mode)

    def _assert_no_eight_character_fragment(
        self, message: str, value: str
    ) -> None:
        leaked = any(
            value[index:index + 8] in message
            for index in range(max(0, len(value) - 7))
        )
        self.assertFalse(leaked, "异常消息泄露了输入值片段")

    def test_parses_supported_line_forms(self):
        key = self.private_hex
        cases = (
            ("带 0x", f"OKXLP_PRIVATE_KEY=0x{key}\n"),
            ("无前缀", f"OKXLP_PRIVATE_KEY={key}\n"),
            ("export", f"export OKXLP_PRIVATE_KEY=0x{key}\n"),
            ("单引号", f"OKXLP_PRIVATE_KEY='0x{key}'\n"),
            ("双引号", f'OKXLP_PRIVATE_KEY="0x{key}"\n'),
            ("空白", f"  OKXLP_PRIVATE_KEY  =  0x{key}  \n"),
            (
                "注释与空行",
                f"\n# 临时测试配置\n\nOKXLP_PRIVATE_KEY=0x{key}\n",
            ),
        )

        for label, content in cases:
            with self.subTest(label=label):
                self._write(content)
                self.assertEqual(load_private_key(self.path), self.account.key)

    def test_missing_variable_has_value_free_error(self):
        self._write(f"OTHER_KEY=0x{self.private_hex}\n")

        with self.assertRaises(DotenvError) as caught:
            load_private_key(self.path)

        message = str(caught.exception)
        self.assertIn("OKXLP_PRIVATE_KEY", message)
        self._assert_no_eight_character_fragment(message, self.private_hex)

    def test_invalid_values_have_fixed_value_free_error(self):
        cases = (
            ("空值", ""),
            ("63 位", self.private_hex[:-1]),
            ("65 位", self.private_hex + "0"),
            ("非十六进制", self.private_hex[:32] + "z" + self.private_hex[33:]),
            ("全 g", "0x" + "g" * 64),
        )

        for label, value in cases:
            with self.subTest(label=label):
                self._write(f"OKXLP_PRIVATE_KEY={value}\n")
                with self.assertRaises(DotenvError) as caught:
                    load_private_key(self.path)
                message = str(caught.exception)
                self.assertEqual(
                    message, "私钥格式非法：应为 64 位十六进制"
                )
                self._assert_no_eight_character_fragment(message, value)

    def test_group_or_other_permissions_are_rejected(self):
        for mode in (0o644, 0o640, 0o604):
            with self.subTest(mode=oct(mode)):
                self._write(f"OKXLP_PRIVATE_KEY=0x{self.private_hex}\n", mode)
                with self.assertRaises(DotenvError) as caught:
                    load_private_key(self.path)
                self.assertIn("chmod 600", str(caught.exception))

    def test_owner_only_permissions_are_accepted(self):
        for mode in (0o600, 0o400):
            with self.subTest(mode=oct(mode)):
                self._write(f"OKXLP_PRIVATE_KEY=0x{self.private_hex}\n", mode)
                self.assertEqual(load_private_key(self.path), self.account.key)

    def test_missing_file_is_rejected_without_parsing(self):
        with self.assertRaises(DotenvError) as caught:
            load_private_key(self.path)

        self.assertIn(".env", str(caught.exception))
        self.assertIn("OKXLP_PRIVATE_KEY", str(caught.exception))

    def test_git_tracked_file_is_rejected_before_parsing(self):
        self._write(f"OKXLP_PRIVATE_KEY=0x{self.private_hex}\n")

        with patch(
            "okxlp.chain.dotenv.subprocess.run",
            return_value=SimpleNamespace(returncode=0),
        ):
            with self.assertRaises(DotenvError) as caught:
                load_private_key(self.path)

        message = str(caught.exception)
        self.assertIn("git rm --cached", message)
        self._assert_no_eight_character_fragment(message, self.private_hex)


if __name__ == "__main__":
    unittest.main()
