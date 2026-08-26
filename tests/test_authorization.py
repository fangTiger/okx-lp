import tempfile
import unittest
from pathlib import Path

from okxlp.exec.authorization import (
    AuthorizationError,
    RunMode,
    load_run_mode,
    require_broadcast_flag,
)


class AuthorizationTest(unittest.TestCase):
    def _write_config(self, content: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "risk.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_loads_dry_run_mode(self):
        self.assertIs(load_run_mode(self._write_config("mode: dry_run\n")), RunMode.DRY_RUN)

    def test_loads_live_mode(self):
        self.assertIs(load_run_mode(self._write_config("mode: live\n")), RunMode.LIVE)

    def test_missing_mode_is_rejected(self):
        with self.assertRaises(AuthorizationError):
            load_run_mode(self._write_config("limits: {}\n"))

    def test_non_string_mode_is_rejected(self):
        with self.assertRaises(AuthorizationError):
            load_run_mode(self._write_config("mode: 1\n"))

    def test_unknown_uppercase_mode_is_rejected(self):
        with self.assertRaises(AuthorizationError):
            load_run_mode(self._write_config('mode: "DRY_RUN"\n'))

    def test_missing_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(AuthorizationError):
                load_run_mode(Path(directory) / "missing.yaml")

    def test_broken_yaml_is_rejected(self):
        with self.assertRaises(AuthorizationError):
            load_run_mode(self._write_config("mode: [dry_run\n"))

    def test_real_booleans_are_accepted(self):
        self.assertIs(require_broadcast_flag(True), True)
        self.assertIs(require_broadcast_flag(False), False)

    def test_non_boolean_values_are_rejected(self):
        for value in (1, 0, "true", "false", None, object(), [1]):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    require_broadcast_flag(value)


if __name__ == "__main__":
    unittest.main()
