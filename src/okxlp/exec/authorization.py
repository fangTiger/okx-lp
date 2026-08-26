"""执行层运行模式与广播权限校验。"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class AuthorizationError(RuntimeError):
    """表示运行模式无法确认或不允许执行目标操作。"""


class RunMode(str, Enum):
    """系统允许的运行模式。"""

    DRY_RUN = "dry_run"
    LIVE = "live"


def load_run_mode(path: Path = Path("config/risk.yaml")) -> RunMode:
    """严格读取顶层 mode；任何异常都拒绝授权。"""
    try:
        root = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise AuthorizationError(f"无法读取运行模式配置 {path}：{error}") from error
    if type(root) is not dict or "mode" not in root:
        raise AuthorizationError("运行模式配置缺少顶层 mode 字段")
    value = root["mode"]
    if type(value) is not str:
        raise AuthorizationError("运行模式 mode 必须是字符串")
    try:
        return RunMode(value.strip())
    except ValueError:
        raise AuthorizationError(f"运行模式 mode 取值非法：{value}") from None


def require_broadcast_flag(value: Any) -> bool:
    """只接受真正的布尔值，避免其他真值意外开启广播。"""
    if type(value) is not bool:
        raise TypeError("allow_broadcast 必须是布尔值，且只有 True 才允许广播")
    return value
