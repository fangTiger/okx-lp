"""主状态机可注入输入与单轮结果类型。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from okxlp.strategy.machine_state import MachineState, PriceBand
from okxlp.uniswap.tickmath import aligned_tick_range_from_price, tick_to_price


WIDTH = Decimal("0.005")


@dataclass(frozen=True)
class MarketSample:
    """主状态机一次决策所用的链上池价与 tick。"""

    price: Decimal
    tick: int


@dataclass(frozen=True)
class RiskDecision:
    """风控闸门对本轮策略的放行结论。"""

    allowed: bool
    reason: str
    allow_exit: bool = False

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool or type(self.allow_exit) is not bool:
            raise TypeError("风控结论与撤出权限必须是布尔值")
        if type(self.reason) is not str or not self.reason.strip():
            raise ValueError("风控原因不得为空")


@dataclass(frozen=True)
class StepResult:
    """单轮结束后的状态与可打印决策依据。"""

    state: MachineState
    reason: str
    should_make_market: bool
    risk_allowed: bool


def build_price_band(
    price: Decimal,
    tick_spacing: int,
    token0_decimals: int,
    token1_decimals: int,
) -> PriceBand:
    """以精确池价构造向外对齐的固定 ±0.5% 区间。"""
    lower, upper = aligned_tick_range_from_price(
        price, WIDTH, tick_spacing, token0_decimals, token1_decimals,
    )
    return PriceBand(
        lower, upper,
        tick_to_price(lower, token0_decimals, token1_decimals),
        tick_to_price(upper, token0_decimals, token1_decimals),
    )


class RiskGate(Protocol):
    """每轮必须调用一次的风控闸门契约。"""

    def check(self, now: datetime) -> RiskDecision:
        """返回是否允许继续持仓或开仓。"""


class MarketFeed(Protocol):
    """策略检查阶段使用的链上池价契约。"""

    def snapshot(self, now: datetime) -> MarketSample:
        """返回同一轮决策使用的市场样本。"""


class MachineActions(Protocol):
    """由 M5/M6 Intent 接线实现的建仓、再平衡和撤出动作。"""

    def enter(
        self, sample: MarketSample, band: PriceBand, *, allow_broadcast: bool = False,
    ) -> None:
        """先用一半 USDC 买标的，再 mint 指定区间。"""

    def rebalance_actions(self, sample: MarketSample, band: PriceBand) -> Any:
        """构造交给 M6 的 burn、collect、swap、mint 动作。"""

    def exit(self, sample: MarketSample, *, allow_broadcast: bool = False) -> None:
        """依次 burn、collect，并把全部标的换回 USDC。"""
