"""只读参考价接口与 Yahoo 欧元股票换汇实现。"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

import certifi


LOGGER = logging.getLogger("okxlp.market.reference")
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
USER_AGENT = "Mozilla/5.0 (compatible; okx-lp/1.0)"


class ReferencePrice(Protocol):
    """可插拔参考价源的最小只读契约。"""

    def get_price(self, now: datetime | None = None) -> Decimal | None:
        """返回美元公允价；不可用时返回空值。"""


class NullReference:
    """明确禁用参考价时使用的空实现。"""

    def get_price(self, now: datetime | None = None) -> None:
        """始终表示参考价不可用。"""
        return None


class YahooFxAdrReference:
    """用 Yahoo 当地股票价乘即期汇率得到美元公允价。"""

    def __init__(
        self,
        local_symbol: str,
        fx_pair: str,
        *,
        cache_ttl_seconds: int = 60,
        max_staleness_seconds: int = 1800,
        timeout: float = 10.0,
        ssl_context: ssl.SSLContext | None = None,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not local_symbol or not fx_pair:
            raise ValueError("Yahoo 行情代码不得为空")
        if cache_ttl_seconds <= 0 or max_staleness_seconds <= 0 or timeout <= 0:
            raise ValueError("缓存、数据新鲜度与网络超时必须大于零")
        self.local_symbol = local_symbol
        self.fx_pair = fx_pair
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_staleness_seconds = max_staleness_seconds
        self.timeout = timeout
        self.ssl_context = ssl_context or ssl.create_default_context(cafile=certifi.where())
        self._urlopen = urlopen
        self._monotonic = monotonic
        self._cache_time = 0.0
        self._cache_value: Decimal | None = None
        self._cache_valid_until: float | None = None
        self._has_cache = False

    def get_price(self, now: datetime | None = None) -> Decimal | None:
        """读取并缓存公允价；任何网络或解析失败均返回空值。"""
        cache_now = self._monotonic()
        current = now or datetime.now(timezone.utc)
        fresh_cache = (
            self._cache_value is None
            or self._cache_valid_until is not None
            and current.timestamp() <= self._cache_valid_until
        )
        if (
            self._has_cache
            and cache_now - self._cache_time < self.cache_ttl_seconds
            and fresh_cache
        ):
            return self._cache_value
        try:
            local_price, local_time = self._fetch(self.local_symbol, current)
            fx_rate, fx_time = self._fetch(self.fx_pair, current)
            value = local_price * fx_rate
            if not value.is_finite() or value <= 0:
                raise ValueError("公允价不是有效正数")
            self._cache_valid_until = (
                min(local_time, fx_time) + self.max_staleness_seconds
            )
        except Exception as error:
            LOGGER.debug("Yahoo 参考价不可用：%s", error)
            value = None
            self._cache_valid_until = None
        self._cache_time = self._monotonic()
        self._cache_value = value
        self._has_cache = True
        return value

    def _fetch(self, symbol: str, now: datetime) -> tuple[Decimal, int]:
        encoded = urllib.parse.quote(symbol, safe=".=")
        request = urllib.request.Request(
            YAHOO_CHART_URL.format(symbol=encoded), headers={"User-Agent": USER_AGENT}
        )
        with self._urlopen(
            request, timeout=self.timeout, context=self.ssl_context
        ) as response:
            payload = json.loads(
                response.read().decode("utf-8"), parse_float=Decimal, parse_int=Decimal
            )
        meta = payload["chart"]["result"][0]["meta"]
        price = Decimal(meta["regularMarketPrice"])
        observed_at = int(meta["regularMarketTime"])
        if not price.is_finite() or price <= 0:
            raise ValueError(f"{symbol} 行情价格无效")
        age = now.timestamp() - observed_at
        if age > self.max_staleness_seconds:
            raise ValueError(f"{symbol} 行情已过期 {age:.0f} 秒")
        return price, observed_at
