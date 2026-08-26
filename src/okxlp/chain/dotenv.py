"""为签名子进程读取权限受限的明文私钥文件。"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


class DotenvError(RuntimeError):
    """表示 dotenv 文件或私钥变量不符合安全要求。"""


PRIVATE_KEY_PATTERN = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")


def _is_git_tracked(path: Path) -> bool:
    """Git 不可用或检查失败时按未跟踪处理。"""
    absolute = path.absolute()
    try:
        result = subprocess.run(
            [
                "git", "ls-files", "--error-unmatch", "--",
                absolute.name,
            ],
            cwd=absolute.parent,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _check_file(path: Path, var_name: str) -> None:
    """在接触文件内容前完成全部安全前置检查。"""
    try:
        metadata = os.stat(path)
    except FileNotFoundError:
        raise DotenvError(
            f".env 文件不存在：请创建 .env 并写入变量 {var_name}"
        ) from None
    except OSError:
        raise DotenvError("无法检查 .env 文件") from None
    if metadata.st_mode & 0o077:
        raise DotenvError(".env 文件权限过宽，请执行 chmod 600 .env")
    if _is_git_tracked(path):
        raise DotenvError(
            ".env 文件已被 git 跟踪，存在泄露风险；"
            "必须先执行 git rm --cached"
        )


def load_private_key(
    path: Path, var_name: str = "OKXLP_PRIVATE_KEY"
) -> bytes:
    """极简解析 dotenv，并返回经过严格格式校验的 32 字节私钥。"""
    path = Path(path)
    _check_file(path, var_name)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise DotenvError("无法读取 .env 文件") from None

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != var_name:
            continue
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]
        if PRIVATE_KEY_PATTERN.fullmatch(value) is None:
            raise DotenvError("私钥格式非法：应为 64 位十六进制")
        return bytes.fromhex(value.removeprefix("0x"))

    raise DotenvError(f"未找到变量 {var_name}")
