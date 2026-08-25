"""池与交易时段配置的 dataclass 加载器。"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from okxlp.config_validation import (
    ConfigError,
    address as _address,
    clock as _clock,
    decimal_value as _decimal,
    integer as _integer,
    list_value as _list,
    mapping as _mapping,
    required as _required,
    string as _string,
    timezone_name as _timezone,
)

@dataclass(frozen=True)
class ChainConfig:
    """链标识与只读 RPC 列表。"""

    chain_id: int
    rpc_urls: tuple[str, ...]

@dataclass(frozen=True)
class TokenConfig:
    """链上代币身份配置。"""

    symbol: str
    address: str
    decimals: int
    name: str | None = None

@dataclass(frozen=True)
class ListingConfig:
    """单个上市地的当地交易时段。"""

    venue: str
    timezone: str
    open_time: time
    close_time: time

@dataclass(frozen=True)
class ReferenceConfig:
    """单池美元公允价的数据源配置。"""

    provider: str
    local_symbol: str
    fx_pair: str
    cache_ttl_seconds: int
    max_staleness_seconds: int

@dataclass(frozen=True)
class PoolConfig:
    """需要校验和调度的单池配置。"""

    pool_id: str
    enabled: bool
    uniswap_version: str
    address: str
    factory: str | None
    token0: TokenConfig
    token1: TokenConfig
    fee_bps: Decimal
    tick_spacing: int
    underlying: str
    reference: ReferenceConfig
    listings: tuple[ListingConfig, ...]


@dataclass(frozen=True)
class FxWindowConfig:
    """外汇周日开盘保护窗口。"""

    timezone: str
    local_time: time
    before_minutes: int
    after_minutes: int


@dataclass(frozen=True)
class AppConfig:
    """M2/M3 使用的完整不可变配置。"""

    chain: ChainConfig
    pools: tuple[PoolConfig, ...]
    fx_sunday_open: FxWindowConfig

    def find_pool(self, pool_id: str | None = None) -> PoolConfig:
        """按标识选择池；未指定时返回首个池。"""
        selected = next((pool for pool in self.pools if pool_id is None or pool.pool_id == pool_id), None)
        if selected is None:
            raise ConfigError(f"配置中找不到池：{pool_id or '首个条目'}")
        return selected


def _token(data: Any, path: str) -> TokenConfig:
    item = _mapping(data, path)
    decimals = _integer(_required(item, "decimals", path), f"{path}.decimals")
    if not 0 <= decimals <= 255:
        raise ConfigError(f"{path}.decimals 必须在 0 到 255 之间")
    name = item.get("name")
    return TokenConfig(
        _string(_required(item, "symbol", path), f"{path}.symbol"),
        _address(_required(item, "address", path), f"{path}.address"),
        decimals,
        None if name is None else _string(name, f"{path}.name"),
    )


def _listing(data: Any, path: str) -> ListingConfig:
    item = _mapping(data, path)
    hours = _string(_required(item, "hours_local", path), f"{path}.hours_local").split("-")
    if len(hours) != 2:
        raise ConfigError(f"{path}.hours_local 时间段格式非法：应为 HH:MM-HH:MM")
    opened, closed = _clock(hours[0], f"{path}.hours_local"), _clock(hours[1], f"{path}.hours_local")
    if opened >= closed:
        raise ConfigError(f"{path}.hours_local 收盘时间必须晚于开盘时间")
    return ListingConfig(
        _string(_required(item, "venue", path), f"{path}.venue"),
        _timezone(_required(item, "timezone", path), f"{path}.timezone"),
        opened,
        closed,
    )


def _reference(data: Any, path: str) -> ReferenceConfig:
    item = _mapping(data, path)
    result = ReferenceConfig(
        _string(_required(item, "provider", path), f"{path}.provider"),
        _string(_required(item, "local_symbol", path), f"{path}.local_symbol"),
        _string(_required(item, "fx_pair", path), f"{path}.fx_pair"),
        _integer(_required(item, "cache_ttl_seconds", path), f"{path}.cache_ttl_seconds"),
        _integer(_required(item, "max_staleness_seconds", path), f"{path}.max_staleness_seconds"),
    )
    if result.cache_ttl_seconds <= 0 or result.max_staleness_seconds <= 0:
        raise ConfigError(f"{path} 缓存与数据新鲜度阈值必须大于零")
    return result


def _pool(data: Any, index: int) -> PoolConfig:
    path, item = f"pools[{index}]", _mapping(data, f"pools[{index}]")
    listings = tuple(
        _listing(value, f"{path}.listings[{offset}]")
        for offset, value in enumerate(_list(_required(item, "listings", path), f"{path}.listings"))
    )
    if not listings:
        raise ConfigError(f"{path}.listings 至少需要一个上市地")
    enabled = _required(item, "enabled", path)
    if type(enabled) is not bool:
        raise ConfigError(f"{path}.enabled 类型不符：应为布尔值")
    factory = item.get("factory")
    return PoolConfig(
        _string(_required(item, "id", path), f"{path}.id"), enabled,
        _string(_required(item, "uniswap_version", path), f"{path}.uniswap_version"),
        _address(_required(item, "address", path), f"{path}.address"),
        None if factory is None else _address(factory, f"{path}.factory"),
        _token(_required(item, "token0", path), f"{path}.token0"),
        _token(_required(item, "token1", path), f"{path}.token1"),
        _decimal(_required(item, "fee_bps", path), f"{path}.fee_bps"),
        _integer(_required(item, "tick_spacing", path), f"{path}.tick_spacing"),
        _string(_required(item, "underlying", path), f"{path}.underlying"),
        _reference(_required(item, "reference", path), f"{path}.reference"), listings,
    )


def load_config(path: Path = Path("config/pools.yaml")) -> AppConfig:
    """加载 pools.yaml，任何不确定输入都以中文错误拒绝。"""
    try:
        data = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), "根配置")
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"无法读取配置文件 {path}：{error}") from error
    chain = _mapping(_required(data, "chain", "根配置"), "chain")
    chain_id = _integer(_required(chain, "id", "chain"), "chain.id")
    rpc_urls = tuple(
        _string(value, f"chain.rpc[{index}]")
        for index, value in enumerate(_list(_required(chain, "rpc", "chain"), "chain.rpc"))
    )
    if not rpc_urls:
        raise ConfigError("chain.rpc 至少需要一个 RPC 节点")
    pools = tuple(_pool(item, index) for index, item in enumerate(_list(_required(data, "pools", "根配置"), "pools")))
    if not pools:
        raise ConfigError("pools 至少需要一个池")
    session = _mapping(_required(data, "session", "根配置"), "session")
    fx = _mapping(_required(session, "fx_sunday_open", "session"), "session.fx_sunday_open")
    fx_path = "session.fx_sunday_open"
    return AppConfig(ChainConfig(chain_id, rpc_urls), pools, FxWindowConfig(
        _timezone(_required(fx, "timezone", fx_path), f"{fx_path}.timezone"),
        _clock(_required(fx, "local_time", fx_path), f"{fx_path}.local_time"),
        _integer(_required(fx, "before_minutes", fx_path), f"{fx_path}.before_minutes"),
        _integer(_required(fx, "after_minutes", fx_path), f"{fx_path}.after_minutes"),
    ))
