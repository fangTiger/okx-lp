"""带故障转移与重试的 X Layer 只读 JSON-RPC 客户端。"""

from __future__ import annotations

import json
import ssl
import time
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any

import certifi


DEFAULT_CHAIN_ID = 196
DEFAULT_RPC_URLS = ("https://rpc.xlayer.tech",)
READ_ONLY_METHODS = frozenset(
    {
        "eth_chainId", "eth_blockNumber", "eth_getBalance", "eth_getCode", "eth_call",
        "eth_getTransactionCount", "eth_estimateGas", "eth_getBlockByNumber",
        "eth_maxPriorityFeePerGas", "eth_getTransactionReceipt", "eth_getTransactionByHash",
    }
)


class RpcError(RuntimeError):
    """表示所有 RPC 尝试均失败。"""


class ChainIdMismatchError(RpcError):
    """表示可访问节点不属于预期链。"""


class JsonRpcClient:
    """仅允许只读方法的同步 JSON-RPC 客户端。"""

    def __init__(
        self,
        endpoints: Sequence[str] | None = None,
        *,
        chain_id: int = DEFAULT_CHAIN_ID,
        timeout: float = 10.0,
        retries: int = 2,
        backoff: float = 0.5,
        ssl_context: ssl.SSLContext | None = None,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.endpoints = tuple(DEFAULT_RPC_URLS if endpoints is None else endpoints)
        if not self.endpoints:
            raise ValueError("至少需要一个 RPC 节点")
        if timeout <= 0 or retries < 0 or backoff < 0:
            raise ValueError("超时必须为正数，重试和退避不得为负数")
        self.chain_id = chain_id
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.ssl_context = ssl_context or ssl.create_default_context(cafile=certifi.where())
        self._urlopen = urlopen
        self._sleep = sleep
        self._next_id = 1
        self._preferred_index = 0
        self._verified_indexes: set[int] = set()
        self._rejected_indexes: set[int] = set()

    def call(self, method: str, params: list[Any]) -> Any:
        """调用只读 RPC 方法，并在节点间故障转移。"""
        if method not in READ_ONLY_METHODS:
            raise ValueError(f"拒绝非只读 RPC 方法：{method}")
        return self._execute(method, params)

    def _execute(
        self, method: str, params: list[Any], validator: Callable[[Any], Any] | None = None
    ) -> Any:
        request_id = self._next_id
        self._next_id += 1
        errors: list[tuple[str, Exception]] = []
        for attempt in range(self.retries + 1):
            indexes = [
                (self._preferred_index + offset) % len(self.endpoints)
                for offset in range(len(self.endpoints))
                if (self._preferred_index + offset) % len(self.endpoints)
                not in self._rejected_indexes
            ]
            if not indexes:
                raise ChainIdMismatchError(f"所有 RPC 节点均不属于 chainId={self.chain_id}")
            for index in indexes:
                endpoint = self.endpoints[index]
                try:
                    if method != "eth_chainId" and index not in self._verified_indexes:
                        self._verify_endpoint(index)
                    result = self._request(endpoint, request_id, method, params)
                    if validator is not None:
                        result = validator(result)
                    if method == "eth_chainId":
                        self._verified_indexes.add(index)
                    self._preferred_index = index
                    return result
                except Exception as error:
                    if isinstance(error, ChainIdMismatchError):
                        self._rejected_indexes.add(index)
                    errors.append((endpoint, error))
            if attempt < self.retries:
                self._sleep(self.backoff * (2**attempt))
        mismatch = next((error for _endpoint, error in errors if isinstance(error, ChainIdMismatchError)), None)
        if mismatch is not None:
            raise ChainIdMismatchError(f"没有可用的 chainId={self.chain_id} 节点：{mismatch}")
        detail = f"{errors[-1][0]}: {errors[-1][1]}" if errors else "未知错误"
        raise RpcError(f"RPC 调用 {method} 失败，共尝试 {len(errors)} 次；最后错误：{detail}")

    def _verify_endpoint(self, index: int) -> None:
        endpoint = self.endpoints[index]
        request_id = self._next_id
        self._next_id += 1
        actual = int(self._request(endpoint, request_id, "eth_chainId", []), 16)
        if actual != self.chain_id:
            raise ChainIdMismatchError(f"链 ID 不匹配：期望 {self.chain_id}，实际 {actual}")
        self._verified_indexes.add(index)

    def _request(self, endpoint: str, request_id: int, method: str, params: list[Any]) -> Any:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            separators=(",", ":"),
        ).encode()
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with self._urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
            output = json.loads(response.read())
        if output.get("jsonrpc") != "2.0":
            raise RpcError(f"{method} 响应的 JSON-RPC 版本无效")
        if output.get("id") != request_id:
            raise RpcError(f"{method} 响应 ID 不匹配")
        if "error" in output:
            raise RpcError(f"{method} 返回错误：{output['error']}")
        if "result" not in output:
            raise RpcError(f"{method} 响应缺少 result")
        return output["result"]

    def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        """执行不会改变链上状态的 eth_call。"""
        return self.call("eth_call", [{"to": to, "data": data}, block])

    def block_number(self) -> int:
        """读取最新区块高度。"""
        return int(self.call("eth_blockNumber", []), 16)

    def send_raw_transaction(
        self, raw_transaction: bytes, *, allow_broadcast: bool = False
    ) -> str:
        """仅在显式授权时广播原始交易；默认永远拒绝。"""
        if not allow_broadcast:
            raise ValueError("交易广播默认禁用，必须显式传入 allow_broadcast=True")
        if not isinstance(raw_transaction, bytes) or not raw_transaction:
            raise ValueError("原始交易必须是非空 bytes")
        return self._execute("eth_sendRawTransaction", ["0x" + raw_transaction.hex()])

    def ensure_chain_id(self) -> int:
        """验证节点连接到预期链。"""
        def validate(result: Any) -> int:
            actual = int(result, 16)
            if actual != self.chain_id:
                raise ChainIdMismatchError(f"链 ID 不匹配：期望 {self.chain_id}，实际 {actual}")
            return actual

        return self._execute("eth_chainId", [], validate)
