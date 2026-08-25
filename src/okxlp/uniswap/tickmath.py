"""Uniswap V3 的价格、tick 与流动性换算。"""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext


TICK_BASE = Decimal("1.0001")
Q96 = Decimal(2**96)


def _decimal_scale(token0_decimals: int, token1_decimals: int) -> Decimal:
    return Decimal(10) ** (token0_decimals - token1_decimals)


def tick_to_price(tick: int, token0_decimals: int, token1_decimals: int) -> Decimal:
    """把 tick 换算为每单位 token0 对应的 token1 人类价格。"""
    with localcontext() as context:
        context.prec = 80
        return (TICK_BASE**tick) * _decimal_scale(token0_decimals, token1_decimals)


def price_to_tick(price: Decimal, token0_decimals: int, token1_decimals: int) -> int:
    """把人类价格换算为不高于该价格的离散 tick。"""
    if price <= 0:
        raise ValueError("价格必须大于零")
    with localcontext() as context:
        context.prec = 80
        raw_price = price / _decimal_scale(token0_decimals, token1_decimals)
        tick = raw_price.ln() / TICK_BASE.ln()
        return int(tick.to_integral_value(rounding=ROUND_FLOOR))


def sqrt_price_x96_to_price(
    sqrt_price_x96: int, token0_decimals: int, token1_decimals: int
) -> Decimal:
    """按探针公式把 sqrtPriceX96 换算为人类价格。"""
    if sqrt_price_x96 <= 0:
        raise ValueError("sqrtPriceX96 必须大于零")
    with localcontext() as context:
        context.prec = 80
        raw_price = (Decimal(sqrt_price_x96) / Q96) ** 2
        return raw_price * _decimal_scale(token0_decimals, token1_decimals)


def price_to_sqrt_price_x96(
    price: Decimal, token0_decimals: int, token1_decimals: int
) -> int:
    """把人类价格换算为向下取整的 sqrtPriceX96。"""
    if price <= 0:
        raise ValueError("价格必须大于零")
    with localcontext() as context:
        context.prec = 80
        raw_price = price / _decimal_scale(token0_decimals, token1_decimals)
        encoded = raw_price.sqrt() * Q96
        return int(encoded.to_integral_value(rounding=ROUND_FLOOR))


def aligned_tick_range(current_tick: int, width: Decimal, tick_spacing: int) -> tuple[int, int]:
    """按相对价格宽度计算区间，并把两端向外对齐。"""
    if not Decimal(0) < width < Decimal(1):
        raise ValueError("区间宽度必须位于零和一之间")
    if tick_spacing <= 0:
        raise ValueError("tickSpacing 必须大于零")
    with localcontext() as context:
        context.prec = 80
        lower_offset = (Decimal(1) - width).ln() / TICK_BASE.ln()
        upper_offset = (Decimal(1) + width).ln() / TICK_BASE.ln()
        lower_steps = ((Decimal(current_tick) + lower_offset) / tick_spacing).to_integral_value(
            rounding=ROUND_FLOOR
        )
        upper_steps = ((Decimal(current_tick) + upper_offset) / tick_spacing).to_integral_value(
            rounding=ROUND_CEILING
        )
        return int(lower_steps) * tick_spacing, int(upper_steps) * tick_spacing


def _capital_coefficient(
    price: Decimal, width: Decimal, token0_decimals: int, token1_decimals: int
) -> Decimal:
    if price <= 0:
        raise ValueError("价格必须大于零")
    if not Decimal(0) < width < Decimal(1):
        raise ValueError("区间宽度必须位于零和一之间")
    raw_price = price / _decimal_scale(token0_decimals, token1_decimals)
    lower_price = raw_price * (Decimal(1) - width)
    upper_price = raw_price * (Decimal(1) + width)
    return (
        Decimal(2) * raw_price.sqrt()
        - raw_price / upper_price.sqrt()
        - lower_price.sqrt()
    )


def liquidity_to_capital(
    liquidity: Decimal | int,
    price: Decimal,
    width: Decimal,
    token0_decimals: int,
    token1_decimals: int,
) -> Decimal:
    """把原始流动性换算为给定区间内的 token1 等效本金。"""
    coefficient = _capital_coefficient(price, width, token0_decimals, token1_decimals)
    return Decimal(liquidity) * coefficient / (Decimal(10) ** token1_decimals)


def capital_to_liquidity(
    capital: Decimal,
    price: Decimal,
    width: Decimal,
    token0_decimals: int,
    token1_decimals: int,
) -> Decimal:
    """把 token1 本金换算为给定区间所能提供的原始流动性。"""
    if capital < 0:
        raise ValueError("本金不得小于零")
    coefficient = _capital_coefficient(price, width, token0_decimals, token1_decimals)
    return capital * (Decimal(10) ** token1_decimals) / coefficient


def liquidity_share(active_liquidity: Decimal | int, own_liquidity: Decimal | int) -> Decimal:
    """计算新增流动性加入后的 in-range 份额。"""
    active = Decimal(active_liquidity)
    own = Decimal(own_liquidity)
    if active < 0 or own < 0 or active + own == 0:
        raise ValueError("流动性必须非负且总和大于零")
    return own / (active + own)
