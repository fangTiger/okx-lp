"""从配置加载交易目标与方法选择器双重白名单。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from okxlp.config_validation import ADDRESS_PATTERN


SELECTOR_PATTERN = re.compile(r"^0x[0-9a-fA-F]{8}$")
CALLDATA_PATTERN = re.compile(r"^0x(?:[0-9a-fA-F]{2}){4,}$")


class WhitelistError(ValueError):
    """表示交易目标或方法未获授权。"""


class TransactionWhitelist:
    """要求目标地址和该目标绑定的选择器同时匹配。"""

    def __init__(self, targets: dict[str, frozenset[str]]) -> None:
        self._targets = targets

    @classmethod
    def from_config(cls, path: Path = Path("config/execution.yaml")) -> "TransactionWhitelist":
        """从 execution.yaml 严格读取双重白名单。"""
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            entries = data["whitelist"]["targets"]
        except (OSError, yaml.YAMLError, KeyError, TypeError):
            raise WhitelistError(f"无法读取白名单配置：{path}") from None
        if type(entries) is not dict or not entries:
            raise WhitelistError("whitelist.targets 必须是非空映射")
        targets: dict[str, frozenset[str]] = {}
        for name, raw in entries.items():
            cls._load_target(targets, name, raw)
        return cls(targets)

    @staticmethod
    def _load_target(targets: dict[str, frozenset[str]], name: Any, raw: Any) -> None:
        if type(raw) is not dict:
            raise WhitelistError(f"白名单目标 {name} 必须是映射")
        address = raw.get("address")
        selectors = raw.get("selectors")
        if type(address) is not str or ADDRESS_PATTERN.fullmatch(address) is None:
            raise WhitelistError(f"白名单目标 {name} 的地址格式非法")
        if type(selectors) is not dict or not selectors:
            raise WhitelistError(f"白名单目标 {name} 至少需要一个方法选择器")
        normalized = []
        for method, selector in selectors.items():
            if type(selector) is not str or SELECTOR_PATTERN.fullmatch(selector) is None:
                raise WhitelistError(f"白名单方法 {name}.{method} 的选择器格式非法")
            normalized.append(selector.lower())
        targets[address.lower()] = frozenset(normalized)

    def validate(self, target: str, calldata: str) -> str:
        """校验目标及其方法选择器，返回规范化选择器。"""
        normalized = target.lower() if type(target) is str else ""
        if ADDRESS_PATTERN.fullmatch(normalized) is None or normalized not in self._targets:
            raise WhitelistError(f"目标地址不在白名单：{target}")
        if type(calldata) is not str or CALLDATA_PATTERN.fullmatch(calldata) is None:
            raise WhitelistError("calldata 格式非法：至少需要 4 字节方法选择器")
        selector = calldata[:10].lower()
        if selector not in self._targets[normalized]:
            raise WhitelistError(f"方法选择器不在该目标白名单：{selector}")
        return selector
