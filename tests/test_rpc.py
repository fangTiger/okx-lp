import json
import ssl
import sys
import unittest
from pathlib import Path
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.chain.rpc import ChainIdMismatchError, JsonRpcClient, RpcError


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class RpcClientTest(unittest.TestCase):
    def test_uses_chain_defaults_and_certifi_context(self):
        seen = {}

        def opener(request, timeout, context):
            seen.update(url=request.full_url, timeout=timeout, context=context)
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": "0xc4"})

        client = JsonRpcClient(urlopen=opener, sleep=lambda _delay: None)

        self.assertEqual(client.ensure_chain_id(), 196)
        self.assertEqual(seen["url"], "https://rpc.xlayer.tech")
        self.assertEqual(seen["timeout"], 10.0)
        self.assertIsInstance(seen["context"], ssl.SSLContext)

    def test_fails_over_to_next_endpoint(self):
        calls = []

        def opener(request, **_kwargs):
            calls.append(request.full_url)
            if request.full_url == "https://bad.example":
                raise URLError("离线")
            body = json.loads(request.data)
            result = "0xc4" if body["method"] == "eth_chainId" else "0x10"
            return FakeResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})

        client = JsonRpcClient(
            ["https://bad.example", "https://good.example"],
            urlopen=opener,
            sleep=lambda _delay: None,
        )

        self.assertEqual(client.call("eth_blockNumber", []), "0x10")
        self.assertEqual(
            calls, ["https://bad.example", "https://good.example", "https://good.example"]
        )

    def test_keeps_healthy_endpoint_first_after_failover(self):
        calls = []

        def opener(request, **_kwargs):
            calls.append(request.full_url)
            if request.full_url == "https://bad.example":
                raise URLError("离线")
            body = json.loads(request.data)
            result = "0xc4" if body["method"] == "eth_chainId" else "0x10"
            return FakeResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})

        client = JsonRpcClient(
            ["https://bad.example", "https://good.example"],
            retries=0,
            urlopen=opener,
        )
        client.call("eth_blockNumber", [])
        client.call("eth_blockNumber", [])

        self.assertEqual(
            calls,
            [
                "https://bad.example",
                "https://good.example",
                "https://good.example",
                "https://good.example",
            ],
        )

    def test_chain_validation_fails_over_from_wrong_chain(self):
        calls = []

        def opener(request, **_kwargs):
            calls.append(request.full_url)
            body = json.loads(request.data)
            result = "0x1" if request.full_url == "https://wrong.example" else "0xc4"
            return FakeResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})

        client = JsonRpcClient(
            ["https://wrong.example", "https://right.example"], retries=0, urlopen=opener
        )

        self.assertEqual(client.ensure_chain_id(), 196)
        self.assertEqual(calls, ["https://wrong.example", "https://right.example"])

    def test_all_wrong_chains_fail_closed(self):
        def opener(request, **_kwargs):
            body = json.loads(request.data)
            return FakeResponse({"jsonrpc": "2.0", "id": body["id"], "result": "0x1"})

        client = JsonRpcClient(["https://wrong.example"], retries=0, urlopen=opener)
        with self.assertRaises(ChainIdMismatchError):
            client.ensure_chain_id()

    def test_runtime_failover_never_uses_known_wrong_chain(self):
        calls = []
        right_online = [True]

        def opener(request, **_kwargs):
            body = json.loads(request.data)
            calls.append((request.full_url, body["method"]))
            if request.full_url == "https://right.example" and not right_online[0]:
                raise URLError("正确节点离线")
            if body["method"] == "eth_chainId":
                result = "0x1" if request.full_url == "https://wrong.example" else "0xc4"
            else:
                result = "0x999"
            return FakeResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})

        client = JsonRpcClient(
            ["https://wrong.example", "https://right.example"], retries=0, urlopen=opener
        )
        self.assertEqual(client.ensure_chain_id(), 196)
        right_online[0] = False

        with self.assertRaises(RpcError):
            client.block_number()
        self.assertNotIn(("https://wrong.example", "eth_blockNumber"), calls)

    def test_retries_all_endpoints_with_exponential_backoff(self):
        sleeps = []
        calls = []

        def opener(request, **_kwargs):
            calls.append(request.full_url)
            raise URLError("离线")

        client = JsonRpcClient(
            ["https://one.example", "https://two.example"],
            retries=2,
            backoff=0.25,
            urlopen=opener,
            sleep=sleeps.append,
        )

        with self.assertRaises(RpcError):
            client.call("eth_blockNumber", [])

        self.assertEqual(len(calls), 6)
        self.assertEqual(sleeps, [0.25, 0.5])

    def test_rpc_error_is_reported_after_retries(self):
        def opener(request, **_kwargs):
            body = json.loads(request.data)
            return FakeResponse(
                {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -1, "message": "失败"}}
            )

        client = JsonRpcClient(retries=0, urlopen=opener, sleep=lambda _delay: None)
        with self.assertRaisesRegex(RpcError, "eth_call"):
            client.eth_call("0x" + "12" * 20, "0x1234")

    def test_rejects_write_rpc_method(self):
        client = JsonRpcClient(urlopen=lambda *_args, **_kwargs: None)
        with self.assertRaisesRegex(ValueError, "只读"):
            client.call("eth_sendRawTransaction", ["0x00"])

    def test_broadcast_is_disabled_without_explicit_permission(self):
        client = JsonRpcClient(urlopen=lambda *_args, **_kwargs: None)

        with self.assertRaisesRegex(ValueError, "广播默认禁用"):
            client.send_raw_transaction(b"\x01")

    def test_rejects_explicit_empty_endpoint_list(self):
        with self.assertRaisesRegex(ValueError, "至少"):
            JsonRpcClient([])

    def test_rejects_mismatched_response_id(self):
        def opener(_request, **_kwargs):
            return FakeResponse({"jsonrpc": "2.0", "id": 999, "result": "0x10"})

        client = JsonRpcClient(retries=0, urlopen=opener)
        with self.assertRaisesRegex(RpcError, "响应 ID"):
            client.call("eth_blockNumber", [])


if __name__ == "__main__":
    unittest.main()
