import contextlib
import importlib.util
import io
import unittest
from pathlib import Path

from okxlp.exec.intent import Intent, IntentStatus


OWNER = "0xb7394e865eb6f22df7aa199e59887e8aac0947a2"
TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "preview_actions.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("preview_actions", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PreviewActionsToolTest(unittest.TestCase):
    def test_owner_and_action_are_required(self):
        tool = load_tool()
        for arguments in ([], ["--owner", OWNER], ["--action", "exit"]):
            with self.subTest(arguments=arguments):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    tool.build_parser().parse_args(arguments)

    def test_broadcast_is_rejected_before_any_rpc_access(self):
        tool = load_tool()
        error = io.StringIO()

        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit):
            tool.main([
                "--owner", OWNER,
                "--action", "exit",
                "--broadcast",
            ])

        self.assertIn("生产入口在批次 8", error.getvalue())

    def test_preview_executor_records_complete_transaction_and_gas(self):
        tool = load_tool()
        output = io.StringIO()
        executor = tool.PreviewExecutor(
            owner=OWNER,
            chain_id=196,
            printer=lambda value: print(value, file=output),
        )
        intent = Intent.create(
            "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca",
            "0x04e45aaf",
        )

        result = executor.execute(intent, allow_broadcast=False)

        self.assertIs(result.intent.status, IntentStatus.DRY_RUN)
        self.assertEqual(result.transaction["from"], OWNER)
        self.assertEqual(result.transaction["to"], intent.target)
        self.assertEqual(result.transaction["data"], intent.calldata)
        self.assertEqual(result.transaction["value"], 0)
        self.assertEqual(result.transaction["chainId"], 196)
        self.assertGreater(result.transaction["gas"], 0)
        self.assertEqual(executor.transaction_count, 1)
        self.assertEqual(executor.total_gas, result.transaction["gas"])
        rendered = output.getvalue()
        self.assertIn("交易 1", rendered)
        self.assertIn('"data": "0x04e45aaf"', rendered)

    def test_preview_executor_never_accepts_broadcast(self):
        tool = load_tool()
        executor = tool.PreviewExecutor(owner=OWNER, chain_id=196)
        intent = Intent.create(
            "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca",
            "0x04e45aaf",
        )

        with self.assertRaisesRegex(PermissionError, "生产入口在批次 8"):
            executor.execute(intent, allow_broadcast=True)

        self.assertEqual(executor.transaction_count, 0)

    def test_tool_source_has_no_signing_or_sending_path(self):
        source = TOOL_PATH.read_text(encoding="utf-8")

        self.assertNotIn("sign_transaction", source)
        self.assertNotIn("send_raw_transaction", source)
        self.assertNotIn("RemoteSigner", source)

    def test_enter_preview_marks_mint_balances_as_estimated(self):
        source = TOOL_PATH.read_text(encoding="utf-8")

        self.assertIn("dry-run mint 数量为 swap 报价估算", source)


if __name__ == "__main__":
    unittest.main()
