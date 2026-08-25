"""配置加载器共用的严格标量校验函数。"""

from __future__ import annotations

import re
from datetime import time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class ConfigError(ValueError):
    """表示配置文件内容不符合运行契约。"""


def mapping(value: Any, path: str) -> dict[str, Any]:
    """要求值为映射。"""
    if type(value) is not dict:
        raise ConfigError(f"{path} 类型不符：应为映射")
    return value


def list_value(value: Any, path: str) -> list[Any]:
    """要求值为列表。"""
    if type(value) is not list:
        raise ConfigError(f"{path} 类型不符：应为列表")
    return value


def required(data: dict[str, Any], key: str, path: str) -> Any:
    """读取必填字段。"""
    if key not in data:
        raise ConfigError(f"{path}.{key} 缺少必填字段")
    return data[key]


def string(value: Any, path: str) -> str:
    """要求值为非空字符串。"""
    if type(value) is not str or not value.strip():
        raise ConfigError(f"{path} 类型不符：应为非空字符串")
    return value.strip()


def integer(value: Any, path: str) -> int:
    """要求值为整数，明确拒绝布尔值。"""
    if type(value) is not int:
        raise ConfigError(f"{path} 类型不符：应为整数")
    return value


def decimal_value(value: Any, path: str) -> Decimal:
    """把 YAML 数值无损转换为 Decimal。"""
    if type(value) not in (int, float):
        raise ConfigError(f"{path} 类型不符：应为数值")
    try:
        return Decimal(str(value))
    except InvalidOperation as error:
        raise ConfigError(f"{path} 不是有效数值") from error


def address(value: Any, path: str) -> str:
    """校验并规范化 EVM 地址。"""
    result = string(value, path)
    if ADDRESS_PATTERN.fullmatch(result) is None:
        raise ConfigError(f"{path} 地址格式非法：必须是 20 字节 EVM 地址")
    return result.lower()


def timezone_name(value: Any, path: str) -> str:
    """校验 IANA 时区名称。"""
    name = string(value, path)
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ConfigError(f"{path} 时区无效：{name}") from error
    return name


def clock(value: Any, path: str) -> time:
    """校验 HH:MM 当地时刻。"""
    text = string(value, path)
    if TIME_PATTERN.fullmatch(text) is None:
        raise ConfigError(f"{path} 时间格式非法：应为 HH:MM")
    return time.fromisoformat(text)
