"""通过 spawn 独立子进程提供二次校验后的交易签名。"""

from __future__ import annotations

import json
import multiprocessing
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from eth_utils import to_checksum_address

from okxlp.chain.calldata_policy import CalldataPolicy
from okxlp.chain.signer import KeystoreSigner
from okxlp.chain.whitelist import TransactionWhitelist


class RemoteSignerError(RuntimeError):
    """表示签名子进程启动、校验或通信失败。"""


MAX_IPC_MESSAGE_BYTES = 4096
TRANSACTION_FIELDS = frozenset(
    {
        "chainId", "nonce", "to", "data", "value", "gas",
        "maxFeePerGas", "maxPriorityFeePerGas", "type",
    }
)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"IPC JSON 包含非标准常量 {value}")


def _encode_message(payload: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("IPC 消息必须只包含 JSON 基本类型") from None
    if len(encoded) > MAX_IPC_MESSAGE_BYTES:
        raise ValueError(
            f"IPC 消息超过 {MAX_IPC_MESSAGE_BYTES} 字节上限"
        )
    return encoded


def _send_message(connection: Any, payload: dict[str, Any]) -> None:
    connection.send_bytes(_encode_message(payload))


def _receive_message(connection: Any) -> dict[str, Any]:
    raw = connection.recv_bytes(MAX_IPC_MESSAGE_BYTES)
    try:
        payload = json.loads(
            raw.decode("utf-8"), parse_constant=_reject_json_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("IPC 消息不是合法 JSON") from None
    if type(payload) is not dict:
        raise ValueError("IPC JSON 根节点必须是映射")
    return payload


def _required(transaction: dict[str, Any], name: str) -> Any:
    if name not in transaction:
        raise ValueError(f"交易缺少必填字段 {name}")
    return transaction[name]


def _validate_transaction(
    transaction: Any, *, chain_id: int, execution_path: str,
    policy: CalldataPolicy,
) -> dict[str, Any]:
    """在子进程签名边界重做完整安全校验。"""
    if type(transaction) is not dict:
        raise ValueError("交易必须是映射")
    if set(transaction) != TRANSACTION_FIELDS:
        missing = sorted(TRANSACTION_FIELDS - set(transaction))
        extra = sorted(set(transaction) - TRANSACTION_FIELDS)
        raise ValueError(f"交易字段集合非法：缺少={missing}，多余={extra}")
    actual_chain_id = _required(transaction, "chainId")
    if type(actual_chain_id) is not int or actual_chain_id != chain_id:
        raise ValueError(
            f"chainId 不匹配：期望={chain_id}，实际={actual_chain_id}"
        )
    target = _required(transaction, "to")
    calldata = _required(transaction, "data")
    TransactionWhitelist.from_config(Path(execution_path)).validate(
        target, calldata
    )
    policy.validate(
        target=target,
        calldata=calldata,
        value=_required(transaction, "value"),
        now_ts=int(time.time()),
    )
    transaction_type = _required(transaction, "type")
    if type(transaction_type) is not int or transaction_type != 2:
        raise ValueError(f"交易 type 必须为 2，实际={transaction_type}")
    for name in ("nonce", "gas", "maxFeePerGas", "maxPriorityFeePerGas"):
        value = _required(transaction, name)
        if type(value) is not int or value < 0:
            raise ValueError(f"交易字段 {name} 必须是非负整数")
    normalized = dict(transaction)
    normalized["to"] = to_checksum_address(target)
    return normalized


def _signer_worker(
    connection: Any, keystore_path: str | None, password_env: str | None,
    dotenv_path: str | None, dotenv_var: str | None, chain_id: int,
    execution_path: str, policy_payload: dict,
) -> None:
    """在全新解释器中持有私钥，且仅返回地址或签名结果。"""
    try:
        has_keystore = bool(keystore_path)
        has_password = bool(password_env)
        has_dotenv = bool(dotenv_path)
        if has_keystore != has_password or has_keystore == has_dotenv:
            raise ValueError("keystore 与 dotenv 密钥来源必须恰好提供一组")
        if has_keystore:
            signer = KeystoreSigner(
                keystore_path, password_env=password_env or ""
            )
        else:
            if type(dotenv_var) is not str or not dotenv_var:
                raise ValueError("dotenv 变量名必须是非空字符串")
            from okxlp.chain.dotenv import load_private_key

            private_key = load_private_key(Path(dotenv_path or ""), dotenv_var)
            try:
                signer = KeystoreSigner.from_private_key(private_key)
            finally:
                del private_key
        policy = CalldataPolicy(**policy_payload)
        _send_message(connection, {"ok": True, "address": signer.address})
    except Exception as error:
        try:
            _send_message(
                connection,
                {"ok": False, "error": f"签名子进程启动失败：{error}"}
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
        connection.close()
        return
    try:
        while True:
            try:
                request = _receive_message(connection)
            except (EOFError, OSError, ValueError):
                return
            if request == {"shutdown": True}:
                return
            try:
                if set(request) == {"refresh_token_ids"}:
                    token_ids = request["refresh_token_ids"]
                    if type(token_ids) is not list:
                        raise ValueError("refresh_token_ids 必须是列表")
                    if len(token_ids) > 50:
                        raise ValueError("refresh_token_ids 数量不得超过 50")
                    if any(
                        type(token_id) is not int or token_id < 0
                        for token_id in token_ids
                    ):
                        raise ValueError(
                            "refresh_token_ids 只能包含非负整数"
                        )
                    # tokenId 属于正确性检查而非资金安全检查 —— NPM 合约自身会
                    # 校验 NFT 归属，对不属于本地址的 tokenId 调用 collect/burn
                    # 只会 revert，无法转移资金。真正防止资金外流的是 recipient、
                    # 币对、fee、value=0、chainId、spender 这些锁，它们在子进程中
                    # 保持不可变，不提供任何刷新入口。
                    policy = replace(
                        policy, allowed_token_ids=frozenset(token_ids)
                    )
                    _send_message(connection, {"ok": True})
                    continue
                if set(request) != {"transaction"}:
                    raise ValueError("签名请求字段集合非法")
                transaction = _validate_transaction(
                    request["transaction"],
                    chain_id=chain_id,
                    execution_path=execution_path,
                    policy=policy,
                )
                raw = signer.sign_transaction(transaction)
                _send_message(connection, {"ok": True, "raw": raw.hex()})
            except Exception as error:
                _send_message(
                    connection,
                    {"ok": False, "error": f"签名请求被拒绝：{error}"}
                )
    finally:
        connection.close()


class RemoteSigner:
    """主进程中不持有私钥的 IPC 签名器。"""

    __slots__ = (
        "_address", "_closed", "_connection", "_process", "_timeout_seconds"
    )

    def __init__(
        self, *, keystore_path: str | Path | None = None,
        password_env: str | None = None,
        dotenv_path: str | Path | None = None,
        dotenv_var: str = "OKXLP_PRIVATE_KEY", chain_id: int,
        execution_path: str | Path, calldata_policy: CalldataPolicy,
        timeout_seconds: float = 30.0,
    ) -> None:
        has_keystore = keystore_path is not None
        has_dotenv = dotenv_path is not None
        if (
            has_keystore == has_dotenv
            or (has_keystore and (
                type(password_env) is not str or not password_env
            ))
            or (has_dotenv and password_env is not None)
            or (has_dotenv and (
                type(dotenv_var) is not str or not dotenv_var
            ))
            or type(chain_id) is not int
            or type(timeout_seconds) not in (int, float)
            or type(timeout_seconds) is bool
            or timeout_seconds <= 0
            or not isinstance(calldata_policy, CalldataPolicy)
        ):
            raise RemoteSignerError("签名子进程启动参数非法")
        payload = {
            "executor_address": calldata_policy.executor_address,
            "npm_address": calldata_policy.npm_address,
            "router_address": calldata_policy.router_address,
            "token0": calldata_policy.token0,
            "token1": calldata_policy.token1,
            "fee": calldata_policy.fee,
            "allowed_token_ids": sorted(calldata_policy.allowed_token_ids),
            "max_approval_raw": dict(calldata_policy.max_approval_raw),
            "max_deadline_seconds": calldata_policy.max_deadline_seconds,
        }
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe(duplex=True)
        self._address = ""
        self._closed = False
        self._connection = parent_connection
        self._timeout_seconds = float(timeout_seconds)
        self._process = context.Process(
            target=_signer_worker,
            args=(
                child_connection,
                None if keystore_path is None else str(keystore_path),
                password_env,
                None if dotenv_path is None else str(dotenv_path),
                dotenv_var,
                chain_id, str(execution_path), payload,
            ),
            daemon=True,
        )
        try:
            self._process.start()
            child_connection.close()
            response = self._receive("启动握手")
            if (
                set(response) not in ({"ok", "address"}, {"ok", "error"})
                or response.get("ok") is not True
                or type(response.get("address")) is not str
            ):
                reason = response.get("error", "启动握手响应非法")
                raise RemoteSignerError(str(reason))
            self._address = response["address"]
        except Exception as error:
            child_connection.close()
            self._terminate()
            if isinstance(error, RemoteSignerError):
                raise
            raise RemoteSignerError(f"签名子进程启动失败：{error}") from None

    @property
    def address(self) -> str:
        """返回子进程手势证明的签名地址。"""
        return self._address

    def sign_transaction(self, transaction: Mapping[str, Any]) -> bytes:
        """在超时保护下请求子进程校验并签名。"""
        if self._closed:
            raise RemoteSignerError("签名子进程已关闭")
        try:
            payload = dict(transaction)
            _send_message(self._connection, {"transaction": payload})
            response = self._receive("签名请求")
        except RemoteSignerError:
            raise
        except Exception as error:
            self._terminate()
            raise RemoteSignerError(f"签名子进程通信失败：{error}") from None
        if response.get("ok") is not True:
            if set(response) != {"ok", "error"}:
                self._terminate()
                raise RemoteSignerError("签名子进程拒绝响应非法")
            raise RemoteSignerError(str(response.get("error", "签名请求被拒绝")))
        if set(response) != {"ok", "raw"}:
            self._terminate()
            raise RemoteSignerError("签名子进程成功响应非法")
        raw = response.get("raw")
        try:
            if type(raw) is not str:
                raise ValueError
            return bytes.fromhex(raw)
        except ValueError:
            raise RemoteSignerError("签名子进程返回的 raw 格式非法") from None

    def refresh_token_ids(self, token_ids) -> None:
        """只替换子进程的 NPM tokenId 正确性集合。"""
        if self._closed:
            raise RemoteSignerError("签名子进程已关闭")
        try:
            payload = list(token_ids)
            _send_message(
                self._connection, {"refresh_token_ids": payload}
            )
            response = self._receive("tokenId 刷新请求")
        except RemoteSignerError:
            raise
        except Exception as error:
            self._terminate()
            raise RemoteSignerError(
                f"签名子进程通信失败：{error}"
            ) from None
        if response.get("ok") is not True:
            if set(response) != {"ok", "error"}:
                self._terminate()
                raise RemoteSignerError("签名子进程拒绝响应非法")
            raise RemoteSignerError(
                str(response.get("error", "tokenId 刷新请求被拒绝"))
            )
        if set(response) != {"ok"}:
            self._terminate()
            raise RemoteSignerError("签名子进程刷新响应非法")

    def _receive(self, action: str) -> dict[str, Any]:
        try:
            ready = self._connection.poll(self._timeout_seconds)
            if ready:
                return _receive_message(self._connection)
        except (BrokenPipeError, EOFError, OSError, ValueError):
            self._terminate()
            raise RemoteSignerError(f"签名子进程在{action}时异常退出") from None
        if not self._process.is_alive():
            self._terminate()
            raise RemoteSignerError(f"签名子进程在{action}时异常退出")
        self._terminate()
        raise RemoteSignerError(f"签名子进程{action}超时")

    def close(self) -> None:
        """优雅结束子进程；超时则强制回收。"""
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.is_alive():
                _send_message(self._connection, {"shutdown": True})
        except (BrokenPipeError, EOFError, OSError):
            pass
        if self._process.pid is not None:
            self._process.join(timeout=min(self._timeout_seconds, 5.0))
            if self._process.is_alive():
                self._force_stop_process()
        self._connection.close()

    def _terminate(self) -> None:
        self._closed = True
        self._connection.close()
        self._force_stop_process()

    def _force_stop_process(self) -> None:
        if self._process.pid is None:
            return
        if self._process.is_alive():
            self._process.terminate()
        self._process.join(timeout=5.0)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=5.0)

    def __enter__(self) -> "RemoteSigner":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"RemoteSigner(address={self.address})"
