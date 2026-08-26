import json
import ssl
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from eth_abi import encode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import okxlp.chain.rpc as rpc_module
from okxlp.chain.rpc import ChainIdMismatchError, JsonRpcClient, RpcError
from okxlp.exec.authorization import AuthorizationError, RunMode


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

    def test_contract_revert_stops_after_first_transport_call(self):
        calls = []

        def opener(request, **_kwargs):
            calls.append(request.full_url)
            body = json.loads(request.data)
            return FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {
                        "code": 3,
                        "message": "execution reverted: Price slippage check",
                    },
                }
            )

        client = JsonRpcClient(
            ["https://one.example", "https://two.example"],
            retries=2,
            urlopen=opener,
            sleep=lambda _delay: None,
        )
        client._verified_indexes.update((0, 1))

        with self.assertRaises(RpcError) as raised:
            client.eth_call("0x" + "12" * 20, "0x1234")

        self.assertIsInstance(raised.exception, rpc_module.ContractRevertError)
        self.assertIn("Price slippage check", str(raised.exception))
        self.assertEqual(calls, ["https://one.example"])

    def test_contract_revert_decodes_error_string_data(self):
        encoded_reason = "0x08c379a0" + encode(
            ["string"], ["Price slippage check"]
        ).hex()

        def opener(request, **_kwargs):
            body = json.loads(request.data)
            return FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {
                        "code": -32000,
                        "message": "VM execution error",
                        "data": encoded_reason,
                    },
                }
            )

        client = JsonRpcClient(retries=2, urlopen=opener)
        client._verified_indexes.add(0)

        with self.assertRaises(RpcError) as raised:
            client.eth_call("0x" + "12" * 20, "0x1234")

        self.assertIsInstance(raised.exception, rpc_module.ContractRevertError)
        self.assertIn("Price slippage check", str(raised.exception))

    def test_contract_revert_code_and_message_conditions_work_independently(self):
        errors = (
            {"code": 3, "message": "VM execution error"},
            {
                "code": -32000,
                "message": "execution reverted: Price slippage check",
            },
        )
        for error in errors:
            with self.subTest(error=error):
                calls = []

                def opener(request, **_kwargs):
                    calls.append(request.full_url)
                    body = json.loads(request.data)
                    return FakeResponse(
                        {
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "error": error,
                        }
                    )

                client = JsonRpcClient(
                    ["https://one.example", "https://two.example"],
                    retries=2,
                    urlopen=opener,
                    sleep=lambda _delay: None,
                )
                client._verified_indexes.update((0, 1))

                with self.assertRaises(
                    rpc_module.ContractRevertError
                ):
                    client.eth_call("0x" + "12" * 20, "0x1234")

                self.assertEqual(calls, ["https://one.example"])

    def test_timeout_still_retries_across_all_endpoints(self):
        calls = []

        def opener(request, **_kwargs):
            calls.append(request.full_url)
            raise TimeoutError("超时")

        client = JsonRpcClient(
            ["https://one.example", "https://two.example"],
            retries=2,
            urlopen=opener,
            sleep=lambda _delay: None,
        )
        client._verified_indexes.update((0, 1))

        with self.assertRaises(RpcError):
            client.eth_call("0x" + "12" * 20, "0x1234")

        self.assertEqual(
            calls,
            ["https://one.example", "https://two.example"] * 3,
        )

    def test_first_endpoint_revert_is_not_hidden_by_later_success(self):
        calls = []

        def opener(request, **_kwargs):
            calls.append(request.full_url)
            body = json.loads(request.data)
            if request.full_url == "https://one.example":
                return FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {
                            "code": 3,
                            "message": "execution reverted: Price slippage check",
                        },
                    }
                )
            return FakeResponse(
                {"jsonrpc": "2.0", "id": body["id"], "result": "0x1234"}
            )

        client = JsonRpcClient(
            ["https://one.example", "https://two.example"],
            retries=0,
            urlopen=opener,
        )
        client._verified_indexes.update((0, 1))

        with self.assertRaises(RpcError) as raised:
            client.eth_call("0x" + "12" * 20, "0x1234")

        self.assertIsInstance(raised.exception, rpc_module.ContractRevertError)
        self.assertEqual(calls, ["https://one.example"])

    def test_rejects_write_rpc_method(self):
        client = JsonRpcClient(urlopen=lambda *_args, **_kwargs: None)
        with self.assertRaisesRegex(ValueError, "只读"):
            client.call("eth_sendRawTransaction", ["0x00"])

    def test_broadcast_is_disabled_without_explicit_permission(self):
        client = JsonRpcClient(urlopen=lambda *_args, **_kwargs: None)

        with self.assertRaisesRegex(ValueError, "广播默认禁用"):
            client.send_raw_transaction(b"\x01")

    def test_non_boolean_broadcast_permissions_are_rejected_before_transport(self):
        calls = []
        client = JsonRpcClient(
            run_mode=RunMode.LIVE,
            urlopen=lambda *_args, **_kwargs: calls.append("urlopen"),
        )

        for value in (1, "true", object()):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    client.send_raw_transaction(b"\x01", allow_broadcast=value)

        self.assertEqual(calls, [])

    def test_dry_run_mode_blocks_broadcast_before_transport(self):
        calls = []
        client = JsonRpcClient(
            run_mode=RunMode.DRY_RUN,
            urlopen=lambda *_args, **_kwargs: calls.append("urlopen"),
        )

        with self.assertRaisesRegex(AuthorizationError, "dry_run"):
            client.send_raw_transaction(b"\x01", allow_broadcast=True)

        self.assertEqual(calls, [])

    def test_run_mode_property_cannot_be_reassigned(self):
        client = JsonRpcClient(run_mode=RunMode.DRY_RUN)

        with self.assertRaises(AttributeError):
            client.run_mode = RunMode.LIVE

        self.assertIs(client.run_mode, RunMode.DRY_RUN)

    def test_malformed_eth_call_results_are_rejected(self):
        for malformed in ({"malformed": True}, 123, "nothex", "0xzz", None):
            with self.subTest(result=malformed):
                methods = []

                def opener(request, **_kwargs):
                    body = json.loads(request.data)
                    methods.append(body["method"])
                    result = "0xc4" if body["method"] == "eth_chainId" else malformed
                    return FakeResponse(
                        {"jsonrpc": "2.0", "id": body["id"], "result": result}
                    )

                client = JsonRpcClient(retries=0, urlopen=opener)
                with self.assertRaisesRegex(RpcError, "eth_call.*result 格式非法"):
                    client.eth_call("0x" + "12" * 20, "0x1234")
                self.assertNotIn("eth_sendRawTransaction", methods)

    def test_invalid_quantity_results_are_rejected(self):
        methods = (
            "eth_chainId", "eth_blockNumber", "eth_getBalance",
            "eth_getTransactionCount", "eth_estimateGas",
            "eth_maxPriorityFeePerGas",
        )
        for method in methods:
            with self.subTest(method=method):
                def opener(request, **_kwargs):
                    body = json.loads(request.data)
                    result = "0xc4" if body["method"] == "eth_chainId" else "0x00"
                    if method == "eth_chainId":
                        result = "0x00"
                    return FakeResponse(
                        {"jsonrpc": "2.0", "id": body["id"], "result": result}
                    )

                client = JsonRpcClient(retries=0, urlopen=opener)
                with self.assertRaisesRegex(RpcError, "result 格式非法"):
                    client.call(method, [])

    def test_invalid_data_object_and_transaction_hash_results_are_rejected(self):
        invalid_results = {
            "eth_getCode": "0x0",
            "eth_getTransactionReceipt": [],
            "eth_getTransactionByHash": [],
            "eth_getBlockByNumber": [],
        }
        for method, invalid in invalid_results.items():
            with self.subTest(method=method):
                def opener(request, **_kwargs):
                    body = json.loads(request.data)
                    result = "0xc4" if body["method"] == "eth_chainId" else invalid
                    return FakeResponse(
                        {"jsonrpc": "2.0", "id": body["id"], "result": result}
                    )

                client = JsonRpcClient(retries=0, urlopen=opener)
                with self.assertRaisesRegex(RpcError, "result 格式非法"):
                    client.call(method, [])

        def hash_opener(request, **_kwargs):
            body = json.loads(request.data)
            result = "0xc4" if body["method"] == "eth_chainId" else "0xab"
            return FakeResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})

        client = JsonRpcClient(
            retries=0, urlopen=hash_opener, run_mode=RunMode.LIVE
        )
        with self.assertRaisesRegex(RpcError, "result 格式非法"):
            client.send_raw_transaction(b"\x01", allow_broadcast=True)

    def test_validator_method_set_mismatch_is_rejected_before_transport(self):
        calls = []
        client = JsonRpcClient(
            retries=0,
            urlopen=lambda *_args, **_kwargs: calls.append("urlopen"),
        )

        with patch.dict(rpc_module._RESULT_VALIDATORS, {}, clear=True):
            with self.assertRaisesRegex(RpcError, "校验器集合"):
                client.call("eth_chainId", [])

        self.assertEqual(calls, [])

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
