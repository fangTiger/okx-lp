"""按上市地并集、财报与外汇窗口判定是否做市。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from okxlp.config import FxWindowConfig, PoolConfig, load_config


EVENT_BEFORE = timedelta(hours=4)
EVENT_AFTER = timedelta(hours=18)


@dataclass(frozen=True)
class EarningsEvent:
    """手工维护的单次财报发布时间。"""

    underlying: str
    published_at: datetime


class MarketSessions:
    """单池做市与撤出窗口的只读状态机。"""

    def __init__(
        self,
        pool: PoolConfig,
        fx_window: FxWindowConfig,
        events: tuple[EarningsEvent, ...],
        events_error: str | None = None,
    ) -> None:
        self.pool = pool
        self.fx_window = fx_window
        self.events = events
        self.events_error = events_error

    @classmethod
    def from_files(
        cls,
        config_path: Path = Path("config/pools.yaml"),
        events_path: Path = Path("config/events.yaml"),
        pool_id: str | None = None,
    ) -> MarketSessions:
        """加载池与事件；事件失败被保存为 fail-safe 状态。"""
        config = load_config(config_path)
        events, error = _load_events(events_path)
        return cls(config.find_pool(pool_id), config.fx_sunday_open, events, error)

    def should_make_market(self, now: datetime) -> tuple[bool, str]:
        """返回是否做市及中文判定依据。"""
        if now.tzinfo is None or now.utcoffset() is None:
            return False, "当前时间缺少时区，按风险优先撤出"
        if self.events_error is not None:
            return False, f"事件文件不可用（{self.events_error}），按有事件处理并撤出"
        event_reason = self._earnings_reason(now)
        if event_reason is not None:
            return False, event_reason
        if self._in_fx_window(now):
            return False, "处于外汇周日开盘保护窗口，强制撤出"
        for listing in self.pool.listings:
            local = now.astimezone(ZoneInfo(listing.timezone))
            local_time = local.replace(tzinfo=None).time()
            if local.weekday() < 5 and listing.open_time <= local_time < listing.close_time:
                return False, f"{listing.venue} 当地交易时段内，按上市地并集撤出"
        return True, "所有上市地均休市，且无财报或外汇开盘事件，允许做市"

    def _earnings_reason(self, now: datetime) -> str | None:
        current = now.astimezone(timezone.utc)
        for event in self.events:
            if event.underlying.casefold() != self.pool.underlying.casefold():
                continue
            start = event.published_at - EVENT_BEFORE
            end = event.published_at + EVENT_AFTER
            if start <= current <= end:
                return (
                    f"{self.pool.underlying} 财报窗口（发布前 4 小时至发布后 18 小时），"
                    "强制撤出"
                )
        return None

    def _in_fx_window(self, now: datetime) -> bool:
        zone = ZoneInfo(self.fx_window.timezone)
        local = now.astimezone(zone)
        if local.weekday() != 6:
            return False
        opened = datetime.combine(local.date(), self.fx_window.local_time, tzinfo=zone)
        return (
            opened - timedelta(minutes=self.fx_window.before_minutes)
            <= local
            <= opened + timedelta(minutes=self.fx_window.after_minutes)
        )


def _text(value: Any, path: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{path} 应为非空字符串")
    return value.strip()


def _event(raw: Any, index: int) -> EarningsEvent:
    path = f"events[{index}]"
    if type(raw) is not dict:
        raise ValueError(f"{path} 应为映射")
    for key in ("type", "underlying", "published_at"):
        if key not in raw:
            raise ValueError(f"{path}.{key} 缺少必填字段")
    if _text(raw["type"], f"{path}.type") != "earnings":
        raise ValueError(f"{path}.type 仅支持 earnings")
    published_text = _text(raw["published_at"], f"{path}.published_at")
    try:
        published = datetime.fromisoformat(published_text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{path}.published_at 不是有效 ISO 8601 时间") from error
    if published.tzinfo is None or published.utcoffset() is None:
        raise ValueError(f"{path}.published_at 必须包含时区")
    return EarningsEvent(
        _text(raw["underlying"], f"{path}.underlying"), published.astimezone(timezone.utc)
    )


def _load_events(path: Path) -> tuple[tuple[EarningsEvent, ...], str | None]:
    try:
        root = yaml.safe_load(path.read_text(encoding="utf-8"))
        if type(root) is not dict or type(root.get("events")) is not list:
            raise ValueError("events 应为列表")
        return tuple(_event(raw, index) for index, raw in enumerate(root["events"])), None
    except OSError:
        return (), "无法读取事件文件"
    except yaml.YAMLError:
        return (), "事件文件 YAML 解析失败"
    except ValueError as error:
        return (), f"事件字段校验失败：{error}"


def should_make_market(now: datetime) -> tuple[bool, str]:
    """使用默认配置判断当前是否做市。"""
    return MarketSessions.from_files().should_make_market(now)
