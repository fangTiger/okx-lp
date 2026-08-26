"""keystore 签名实现；仅供签名子进程内部使用。

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


def _load_keystore(path: Path, password_env: str) -> tuple[str, Callable[[Mapping[str, Any]], bytes]]:
    """解密密钥并把它封闭在本模块创建的签名闭包中。"""
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
        address = Account.from_key(private_key).address
    except Exception:
        raise KeystoreError("keystore 解密失败：口令错误或文件已损坏") from None

    def sign(transaction: Mapping[str, Any]) -> bytes:
        try:
            signed = Account.sign_transaction(dict(transaction), private_key)
            return bytes(signed.raw_transaction)
        except Exception:
            raise SigningError("交易签名失败：交易字段无效") from None

    return address, sign


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
