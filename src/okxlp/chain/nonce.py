"""基于链上 pending 状态对账的进程内 nonce 管理器。"""

from __future__ import annotations

from threading import Lock
from typing import Any, Protocol


class RpcLike(Protocol):
    """nonce 管理所需的最小 RPC 接口。"""

    def call(self, method: str, params: list[Any]) -> Any: ...


class NonceManager:
    """每次分配前对账 pending nonce，并在本地原子递增。"""

    def __init__(self, rpc: RpcLike, address: str) -> None:
        self.rpc = rpc
        self.address = address
        self._next_nonce: int | None = None
        self._lock = Lock()

    @property
    def next_nonce(self) -> int | None:
        """返回下一本地 nonce；首次对账前为空。"""
        with self._lock:
            return self._next_nonce

    def _chain_pending(self) -> int:
        return int(self.rpc.call("eth_getTransactionCount", [self.address, "pending"]), 16)

    def sync(self) -> int:
        """以链上 pending 值重置本地状态，供启动或显式恢复使用。"""
        with self._lock:
            self._next_nonce = self._chain_pending()
            return self._next_nonce

    def reserve(self) -> int:
        """对账后保留一个 nonce，并为下一笔在本地递增。"""
        with self._lock:
            pending = self._chain_pending()
            if self._next_nonce is None or pending > self._next_nonce:
                self._next_nonce = pending
            reserved = self._next_nonce
            self._next_nonce += 1
            return reserved
