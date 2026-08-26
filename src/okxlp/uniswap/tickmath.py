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


def position_amounts(
    liquidity: int, tick_lower: int, tick_upper: int, sqrt_price_x96: int,
) -> tuple[int, int]:
    """按当前价格与区间返回头寸的 (amount0_raw, amount1_raw)。"""
    if type(liquidity) is not int or liquidity < 0:
        raise ValueError("liquidity 必须是非负整数")
    if (
        type(tick_lower) is not int
        or type(tick_upper) is not int
        or tick_lower >= tick_upper
    ):
        raise ValueError("tick_lower 必须小于 tick_upper")
    if type(sqrt_price_x96) is not int or sqrt_price_x96 <= 0:
        raise ValueError("sqrt_price_x96 必须是正整数")
    with localcontext() as context:
        context.prec = 80
        sa = (TICK_BASE**tick_lower).sqrt() * Q96
        sb = (TICK_BASE**tick_upper).sqrt() * Q96
        sp = Decimal(sqrt_price_x96)
        value = Decimal(liquidity)
        if sp <= sa:
            amount0 = value * (sb - sa) * Q96 / (sa * sb)
            amount1 = Decimal(0)
        elif sp >= sb:
            amount0 = Decimal(0)
            amount1 = value * (sb - sa) / Q96
        else:
            amount0 = value * (sb - sp) * Q96 / (sp * sb)
            amount1 = value * (sp - sa) / Q96
        return (
            int(amount0.to_integral_value(rounding=ROUND_FLOOR)),
            int(amount1.to_integral_value(rounding=ROUND_FLOOR)),
        )


def mint_amounts_for_budget(
    amount0_budget: int,
    amount1_budget: int,
    tick_lower: int,
    tick_upper: int,
    sqrt_price_x96: int,
) -> tuple[int, int]:
    """在两腿预算上限内，返回该区间实际可铸造的配比数量。"""
    if type(amount0_budget) is not int or type(amount1_budget) is not int:
        raise ValueError("两腿预算必须是非负整数")
    if amount0_budget < 0 or amount1_budget < 0:
        raise ValueError("两腿预算必须是非负整数")
    if (
        type(tick_lower) is not int
        or type(tick_upper) is not int
        or tick_lower >= tick_upper
    ):
        raise ValueError("tick_lower 必须小于 tick_upper")
    if type(sqrt_price_x96) is not int or sqrt_price_x96 <= 0:
        raise ValueError("sqrt_price_x96 必须是正整数")

    with localcontext() as context:
        context.prec = 80
        sa = (TICK_BASE**tick_lower).sqrt() * Q96
        sb = (TICK_BASE**tick_upper).sqrt() * Q96
        sp = Decimal(sqrt_price_x96)
        if sp <= sa:
            liquidity = (
                Decimal(amount0_budget) * sa * sb
                / ((sb - sa) * Q96)
            )
        elif sp >= sb:
            liquidity = Decimal(amount1_budget) * Q96 / (sb - sa)
        else:
            liquidity0 = (
                Decimal(amount0_budget) * sp * sb
                / ((sb - sp) * Q96)
            )
            liquidity1 = Decimal(amount1_budget) * Q96 / (sp - sa)
            liquidity = min(liquidity0, liquidity1)

        amount0, amount1 = position_amounts(
            int(liquidity), tick_lower, tick_upper, sqrt_price_x96,
        )
        return min(amount0, amount0_budget), min(amount1, amount1_budget)


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


def aligned_tick_range_from_price(
    price: Decimal,
    width: Decimal,
    tick_spacing: int,
    token0_decimals: int,
    token1_decimals: int,
) -> tuple[int, int]:
    """以精确池价为中心计算区间，并把两端分别向外对齐。"""
    if price <= 0:
        raise ValueError("价格必须大于零")
    if not Decimal(0) < width < Decimal(1):
        raise ValueError("区间宽度必须位于零和一之间")
    if tick_spacing <= 0:
        raise ValueError("tickSpacing 必须大于零")
    with localcontext() as context:
        context.prec = 80
        raw = price / _decimal_scale(token0_decimals, token1_decimals)
        lower_raw_tick = (raw * (Decimal(1) - width)).ln() / TICK_BASE.ln()
        upper_raw_tick = (raw * (Decimal(1) + width)).ln() / TICK_BASE.ln()
        lower_steps = (lower_raw_tick / tick_spacing).to_integral_value(
            rounding=ROUND_FLOOR
        )
        upper_steps = (upper_raw_tick / tick_spacing).to_integral_value(
            rounding=ROUND_CEILING
        )
        lower = int(lower_steps) * tick_spacing
        upper = int(upper_steps) * tick_spacing
        if lower >= upper:
            raise ValueError("向外对齐后的区间下沿必须小于上沿")
        return lower, upper


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
