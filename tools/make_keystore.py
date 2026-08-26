"""把交互式输入的明文私钥转换为受口令保护的 keystore。"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from pathlib import Path

from eth_account import Account


PASSWORD_ENV = "OKXLP_KEYSTORE_PASSWORD"
PRIVATE_KEY_PATTERN = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")


class KeystoreCreationError(RuntimeError):
    """表示输入或安全落盘条件不符合要求。"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="交互式生成加密 keystore")
    parser.add_argument(
        "--out", type=Path, default=Path("secrets/keystore.json"),
        help="输出路径（默认 secrets/keystore.json）",
    )
    return parser


def _private_key(value: str) -> bytes:
    if PRIVATE_KEY_PATTERN.fullmatch(value) is None:
        raise KeystoreCreationError(
            "私钥格式非法：必须是带或不带 0x 前缀的 64 位十六进制"
        )
    try:
        return bytes.fromhex(value.removeprefix("0x"))
    except ValueError:
        raise KeystoreCreationError(
            "私钥格式非法：必须是带或不带 0x 前缀的 64 位十六进制"
        ) from None


def _write_new(path: Path, payload: dict) -> None:
    parent_existed = path.parent.exists()
    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not parent_existed:
            path.parent.chmod(0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise KeystoreCreationError(
            f"输出文件已存在，拒绝覆盖；请手动删除后重试：{path}"
        ) from None
    except OSError as error:
        raise KeystoreCreationError(f"无法创建 keystore 输出文件：{error}") from None
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise KeystoreCreationError("keystore 写入失败，未保留不完整文件") from None


def main(argv: list[str] | None = None) -> int:
    """交互式读取敏感值并创建新文件，任何敏感值均不回显。"""
    args = build_parser().parse_args(argv)
    try:
        if args.out.exists():
            raise KeystoreCreationError(
                f"输出文件已存在，拒绝覆盖；请手动删除后重试：{args.out}"
            )
        key = _private_key(getpass.getpass("请输入明文私钥："))
        password = getpass.getpass("请输入 keystore 密码：")
        repeated = getpass.getpass("请再次输入 keystore 密码：")
        if not password:
            raise KeystoreCreationError("keystore 密码不得为空")
        if password != repeated:
            raise KeystoreCreationError("两次输入的密码不一致")
        try:
            account = Account.from_key(key)
            payload = Account.encrypt(key, password)
        except Exception:
            raise KeystoreCreationError("keystore 加密失败") from None
        _write_new(args.out, payload)
    except KeystoreCreationError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    print(f"keystore 已写入：{args.out}")
    print(f"推导地址：{account.address}")
    print(f"运行前请设置环境变量：{PASSWORD_ENV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
