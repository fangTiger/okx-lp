"""按实盘否决与仓位限制分级的活动事实闸门。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml


LOGGER = logging.getLogger("okxlp.campaign.gate")
BLOCK_LEVELS = frozenset({"live", "size"})


class FactConfigError(ValueError):
    """表示事实清单无法安全使用。"""


@dataclass(frozen=True)
class Fact:
    """单条活动事实的核实状态与阻断级别。"""

    fact_id: str
    name: str
    verified: bool | str
    blocks: str | None
    note: str


@dataclass(frozen=True)
class FactGate:
    """只让 live 事实否决写链，让 size 事实限制仓位。"""

    facts: tuple[Fact, ...]

    @property
    def unverified(self) -> tuple[Fact, ...]:
        """返回仍未核实的事实。"""
        return tuple(fact for fact in self.facts if fact.verified is False)

    @property
    def live_blockers(self) -> tuple[Fact, ...]:
        """返回禁止一切写链的事实。"""
        return tuple(fact for fact in self.unverified if fact.blocks == "live")

    @property
    def size_blockers(self) -> tuple[Fact, ...]:
        """返回仅把仓位压到探针规模的事实。"""
        return tuple(fact for fact in self.unverified if fact.blocks == "size")

    @property
    def forced_dry_run(self) -> bool:
        """说明事实层是否必须覆盖实盘请求为 dry-run。"""
        return bool(self.live_blockers)

    def log_startup(self) -> None:
        """按否决级别记录未核实事实，不混淆实盘与规模限制。"""
        if self.live_blockers:
            detail = _detail(self.live_blockers)
            LOGGER.warning("存在实盘阻断事实，强制 dry-run 并拒绝写链：\n%s", detail)
        if self.size_blockers:
            detail = _detail(self.size_blockers)
            LOGGER.warning("存在规模未校准事实，允许写链但限制仓位到探针上限：\n%s", detail)
        if not self.unverified:
            LOGGER.info("事实清单已全部核实或标记为不适用")

    def ensure_write_allowed(self) -> None:
        """仅在存在 live 级事实时拒绝写链。"""
        if self.live_blockers:
            ids = "、".join(fact.fact_id for fact in self.live_blockers)
            raise PermissionError(f"拒绝写链：事实项 {ids} 尚未核实并阻断实盘")

    def max_position_usd(self, configured: Any, probe_capital_usd: Any) -> Decimal:
        """返回事实闸门允许的单池美元仓位上限。"""
        configured_value = _non_negative_decimal(configured, "configured")
        probe_value = _non_negative_decimal(probe_capital_usd, "probe_capital_usd")
        return min(configured_value, probe_value) if self.size_blockers else configured_value


def _detail(facts: tuple[Fact, ...]) -> str:
    return "\n".join(f"- {fact.fact_id}｜{fact.name}：{fact.note}" for fact in facts)


def _non_negative_decimal(value: Any, path: str) -> Decimal:
    if type(value) is bool:
        raise ValueError(f"{path} 必须是非负数值")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{path} 必须是非负数值") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"{path} 必须是非负数值")
    return result


def _text(value: Any, path: str) -> str:
    if type(value) is not str or not value.strip():
        raise FactConfigError(f"{path} 类型不符：应为非空字符串")
    return value.strip()


def load_fact_gate(path: Path = Path("config/facts.yaml")) -> FactGate:
    """加载事实清单；格式或分级不确定时拒绝启动。"""
    try:
        root = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise FactConfigError(f"无法读取事实清单 {path}：{error}") from error
    if type(root) is not dict or type(root.get("facts")) is not list:
        raise FactConfigError("facts 类型不符：应为事实列表")
    facts: list[Fact] = []
    seen: set[str] = set()
    for index, raw in enumerate(root["facts"]):
        fact = _load_fact(raw, index)
        if fact.fact_id in seen:
            raise FactConfigError(f"facts[{index}].id 重复：{fact.fact_id}")
        facts.append(fact)
        seen.add(fact.fact_id)
    if not facts:
        raise FactConfigError("facts 至少需要一条事实")
    return FactGate(tuple(facts))


def _load_fact(raw: Any, index: int) -> Fact:
    path = f"facts[{index}]"
    if type(raw) is not dict:
        raise FactConfigError(f"{path} 类型不符：应为映射")
    for key in ("id", "name", "verified"):
        if key not in raw:
            raise FactConfigError(f"{path}.{key} 缺少必填字段")
    verified = raw["verified"]
    if type(verified) is not bool and verified != "n/a":
        raise FactConfigError(f"{path}.verified 必须是布尔值或 n/a")
    blocks = raw.get("blocks")
    if verified is False and blocks not in BLOCK_LEVELS:
        raise FactConfigError(f"{path}.blocks 在未核实时必须是 live 或 size")
    if verified is not False and blocks is not None:
        raise FactConfigError(f"{path}.blocks 仅允许用于 verified: false")
    note = raw.get("note", "")
    if type(note) is not str:
        raise FactConfigError(f"{path}.note 类型不符：应为字符串")
    return Fact(
        _text(raw["id"], f"{path}.id"), _text(raw["name"], f"{path}.name"),
        verified, blocks, note,
    )
