import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.chain.whitelist import TransactionWhitelist, WhitelistError


NPM = "0x315e413a11ab0df498ef83873012430ca36638ae"
ROUTER = "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca"
QUOTER = "0xd1b797d92d87b688193a2b976efc8d577d204343"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
TOKEN = "0x9147b03c16b18fc4f686f610f189f91ddf4347b4"
TOKEN1 = "0xb6ceceab302e2e4948951ee7843fc24e92933061"


class TransactionWhitelistTest(unittest.TestCase):
    def _load(self):
        content = f"""
        whitelist:
          targets:
            npm:
              address: "{NPM}"
              selectors:
                mint: "0x88316456"
            token:
              address: "{TOKEN}"
              selectors:
                approve: "0x095ea7b3"
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "execution.yaml"
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        return TransactionWhitelist.from_config(path)

    def test_allows_only_selector_bound_to_matching_target(self):
        whitelist = self._load()

        self.assertEqual(whitelist.validate(NPM.upper().replace("0X", "0x"), "0x88316456"), "0x88316456")
        with self.assertRaisesRegex(WhitelistError, "方法选择器"):
            whitelist.validate(TOKEN, "0x88316456")

    def test_rejects_unknown_target_before_signing(self):
        whitelist = self._load()

        with self.assertRaisesRegex(WhitelistError, "目标地址"):
            whitelist.validate("0x" + "99" * 20, "0x88316456")

    def test_rejects_short_or_malformed_calldata(self):
        whitelist = self._load()

        for calldata in ("0x", "0x1234", "88316456", "0xzz316456"):
            with self.subTest(calldata=calldata):
                with self.assertRaisesRegex(WhitelistError, "calldata"):
                    whitelist.validate(NPM, calldata)

    def test_actual_config_contains_all_confirmed_addresses(self):
        import yaml

        data = yaml.safe_load(Path("config/execution.yaml").read_text(encoding="utf-8"))

        self.assertEqual(
            data["addresses"],
            {
                "pool": "0xc3d659028117f1ae5db9b9c68239b4a71f03ef37",
                "factory": "0x4b2ab38dbf28d31d467aa8993f6c2585981d6804",
                "npm": NPM,
                "swap_router02": ROUTER,
                "quoter_v2": QUOTER,
                "permit2": PERMIT2,
                "weth9": "0xe538905cf8410324e03a5a23c1c177a474d59b2b",
                "wasmlx": TOKEN,
                "usdc": "0xb6ceceab302e2e4948951ee7843fc24e92933061",
            },
        )
        whitelist = TransactionWhitelist.from_config(Path("config/execution.yaml"))
        for selector in (
            "0x88316456", "0x0c49ccbe", "0xfc6f7865", "0x42966c68"
        ):
            self.assertEqual(whitelist.validate(NPM, selector), selector)
        with self.assertRaises(WhitelistError):
            whitelist.validate(NPM, "0x219f5d17")
        self.assertEqual(whitelist.validate(ROUTER, "0x04e45aaf"), "0x04e45aaf")
        self.assertEqual(whitelist.validate(TOKEN, "0x095ea7b3"), "0x095ea7b3")
        self.assertEqual(whitelist.validate(TOKEN1, "0x095ea7b3"), "0x095ea7b3")

        for name in ("wasmlx", "usdc"):
            self.assertEqual(
                data["whitelist"]["targets"][name]["selectors"],
                {"approve": "0x095ea7b3"},
            )


if __name__ == "__main__":
    unittest.main()
