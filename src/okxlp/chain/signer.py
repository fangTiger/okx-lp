"""本地密钥签名实现；仅供签名子进程内部使用。

主进程必须使用 ``RemoteSigner``，不得直接构造本模块的签名器。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from eth_account import Account


class KeystoreError(RuntimeError):
    """表示 keystore 加载或解密失败。"""


class SigningError(RuntimeError):
    """表示交易签名失败。"""


def _build_signer(
    private_key: bytes,
) -> tuple[str, Callable[[Mapping[str, Any]], bytes]]:
    """把已校验的密钥封闭在本模块创建的签名闭包中。"""
    address = Account.from_key(private_key).address

    def sign(transaction: Mapping[str, Any]) -> bytes:
        try:
            signed = Account.sign_transaction(dict(transaction), private_key)
            return bytes(signed.raw_transaction)
        except Exception:
            raise SigningError("交易签名失败：交易字段无效") from None

    return address, sign


def _load_keystore(
    path: Path, password_env: str,
) -> tuple[str, Callable[[Mapping[str, Any]], bytes]]:
    """解密 keystore 并创建签名闭包。"""
    password = os.environ.get(password_env)
    if not password:
        raise KeystoreError(f"keystore 口令环境变量 {password_env} 未设置或为空")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise KeystoreError(f"无法读取 keystore 文件：{path}") from None
    except (json.JSONDecodeError, TypeError):
        raise KeystoreError(f"keystore 文件不是有效 JSON：{path}") from None
    try:
        private_key = Account.decrypt(payload, password)
    except Exception:
        raise KeystoreError("keystore 解密失败：口令错误或文件已损坏") from None
    return _build_signer(private_key)


class KeystoreSigner:
    """仅供签名子进程内部使用；主进程必须使用 ``RemoteSigner``。"""

    __slots__ = ("_address", "_signer")

    def __init__(
        self,
        keystore_path: str | Path,
        *,
        password_env: str = "OKXLP_KEYSTORE_PASSWORD",
    ) -> None:
        self._address, self._signer = _load_keystore(Path(keystore_path), password_env)

    @classmethod
    def from_private_key(cls, private_key: bytes) -> "KeystoreSigner":
        """从签名子进程已加载的 32 字节私钥创建同型签名器。"""
        if type(private_key) is not bytes or len(private_key) != 32:
            raise KeystoreError("私钥格式非法：应为 32 字节")
        instance = cls.__new__(cls)
        instance._address, instance._signer = _build_signer(private_key)
        return instance

    @property
    def address(self) -> str:
        """返回 keystore 对应的校验和地址。"""
        return self._address

    def sign_transaction(self, transaction: Mapping[str, Any]) -> bytes:
        """签名 EVM 交易并返回原始字节。"""
        return self._signer(transaction)

    def __repr__(self) -> str:
        """返回不包含密钥、口令或 keystore 内容的安全描述。"""
        return f"KeystoreSigner(address={self.address})"
