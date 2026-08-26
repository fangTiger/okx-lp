"""把主状态机动作接到受限 Intent 构造器与执行器。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any

import yaml

from okxlp.config_validation import address as validate_address
from okxlp.exec.authorization import require_broadcast_flag
from okxlp.exec.intent import Intent, IntentStatus
from okxlp.strategy.allocation import BalanceSnapshot
from okxlp.strategy.rebalance import RebalanceActions
from okxlp.uniswap.tickmath import position_amounts


LOGGER = logging.getLogger(__name__)
BPS = Decimal("10000")
RISK_PATH = Path("config/risk.yaml")


class ActionError(RuntimeError):
    """表示生产动作在安全检查或某个执行阶段中止。"""


class ProductionActions:
    """按固定安全顺序构造并执行建仓、再平衡和撤出 Intent。"""

    def __init__(
        self, *, executor, reader, approval_manager, position_manager,
        swap_router, owner: str, pool, fact_gate, swap_policy,
        deadline_seconds: int = 300,
        clock: Callable[[], int] = lambda: int(time.time()),
        dust_threshold_raw: int = 10**12,
    ) -> None:
        if type(deadline_seconds) is not int or deadline_seconds <= 0:
            raise ValueError("deadline_seconds 必须是正整数")
        if type(dust_threshold_raw) is not int or dust_threshold_raw < 0:
            raise ValueError("dust_threshold_raw 必须是非负整数")
        self.executor = executor
        self.reader = reader
        self.approval_manager = approval_manager
        self.position_manager = position_manager
        self.swap_router = swap_router
        self.owner = validate_address(owner, "owner")
        self.pool = pool
        self.fact_gate = fact_gate
        self.swap_policy = swap_policy
        self.deadline_seconds = deadline_seconds
        self.clock = clock
        self.dust_threshold_raw = dust_threshold_raw
        self.token0 = pool.token0
        self.token1 = pool.token1
        stable = tuple(
            token for token in (self.token0, self.token1)
            if token.symbol.upper() == "USDC"
        )
        if len(stable) != 1:
            raise ActionError("目标池必须恰好包含一腿 USDC")
        self.usdc = stable[0]
        self.asset = self.token1 if self.usdc is self.token0 else self.token0
        fee_units = Decimal(str(pool.fee_bps)) * Decimal(100)
        if fee_units != fee_units.to_integral_value():
            raise ActionError("pool.fee_bps 无法精确换算为 Uniswap fee")
        self.fee = int(fee_units)

    @property
    def _spenders(self) -> tuple[str, str]:
        return (self.position_manager.address, self.swap_router.router_address)

    def _deadline(self) -> int:
        value = self.clock()
        if type(value) is not int:
            raise ActionError("clock 必须返回整数时间戳")
        return value + self.deadline_seconds

    def _balance_raw(self, snapshot, token) -> int:
        return (
            snapshot.balance0_raw if token is self.token0
            else snapshot.balance1_raw
        )

    def _ordered_amounts(
        self, asset_amount: int, usdc_amount: int
    ) -> tuple[int, int]:
        if self.asset is self.token0:
            return asset_amount, usdc_amount
        return usdc_amount, asset_amount

    def _minimum(self, desired: int) -> int:
        minimum = int(
            (
                Decimal(desired)
                * (BPS - self.swap_policy.max_slippage_bps)
                / BPS
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        if minimum <= 0:
            raise ActionError("mint 最低数量为 0，拒绝无保护建仓")
        return minimum

    def _decrease_minimums(self, position, sample) -> tuple[int, int]:
        """按决策轮同区块池价计算 decreaseLiquidity 两腿下限。"""
        sqrt_price_x96 = getattr(sample, "sqrt_price_x96", None)
        if type(sqrt_price_x96) is not int or sqrt_price_x96 <= 0:
            raise ActionError("决策池快照缺少有效 sqrt_price_x96，拒绝撤流动性")
        expected0, expected1 = position_amounts(
            position.liquidity,
            position.tick_lower,
            position.tick_upper,
            sqrt_price_x96,
        )

        def protected(expected: int) -> int:
            return int(
                (
                    Decimal(expected)
                    * (BPS - self.swap_policy.max_slippage_bps)
                    / BPS
                ).to_integral_value(rounding=ROUND_FLOOR)
            )

        minimums = protected(expected0), protected(expected1)
        if position.liquidity > 0 and minimums == (0, 0):
            raise ActionError(
                "非零流动性头寸的两腿滑点下限均为 0，拒绝无保护撤出"
            )
        return minimums

    def _execute(self, intent: Intent, stage: str, broadcast: bool) -> None:
        try:
            result = self.executor.execute(
                intent, allow_broadcast=broadcast
            )
        except Exception as error:
            reason = str(error) or error.__class__.__name__
            raise ActionError(f"{stage} 阶段执行失败：{reason}") from error
        expected = (
            IntentStatus.CONFIRMED if broadcast else IntentStatus.DRY_RUN
        )
        if result.intent.status is not expected:
            raise ActionError(
                f"{stage} 阶段返回状态 {result.intent.status.value}，"
                f"期望 {expected.value}"
            )

    @staticmethod
    def _active_position(snapshot):
        active = tuple(
            item for item in snapshot.positions if item.liquidity > 0
        )
        if len(active) != 1:
            raise ActionError(
                f"本池流动性大于 0 的头寸数为 {len(active)}，无法自动执行"
            )
        return active[0]

    def enter(
        self, sample, band, *, allow_broadcast: bool = False
    ) -> None:
        """补足四组授权，再以 USDC 本金的一半买入并 mint。"""
        broadcast = require_broadcast_flag(allow_broadcast)
        portfolio = self.reader.read(self.owner, spenders=self._spenders)
        total_capital_usd, probe_capital_usd = _capital_limits()
        usdc_balance_usd = Decimal(
            self._balance_raw(portfolio, self.usdc)
        ) / (Decimal(10) ** self.usdc.decimals)
        allowed = self.fact_gate.max_position_usd(
            total_capital_usd, probe_capital_usd
        )
        capital_usd = min(usdc_balance_usd, allowed)
        if capital_usd <= 0:
            raise ActionError(
                "可用本金为 0，请先在 config/risk.yaml 设置 "
                "limits.total_capital_usd"
            )
        capital_raw = int(
            (capital_usd * (Decimal(10) ** self.usdc.decimals))
            .to_integral_value(rounding=ROUND_FLOOR)
        )
        swap_amount = capital_raw // 2
        remaining_usdc = capital_raw - swap_amount
        if swap_amount <= 0 or remaining_usdc <= 0:
            raise ActionError("可用本金过小，无法按 50/50 构造受保护建仓")

        try:
            swaps = self.swap_router.plan_exact_input_single(
                token_in=self.usdc.address,
                token_out=self.asset.address,
                fee=self.fee,
                recipient=self.owner,
                amount_in=swap_amount,
                amount_usd=Decimal(swap_amount)
                / (Decimal(10) ** self.usdc.decimals),
                slippage_bps=self.swap_policy.max_slippage_bps,
            )
            asset_amount = sum(item.quote.amount_out for item in swaps)
            amount0_desired, amount1_desired = self._ordered_amounts(
                asset_amount, remaining_usdc
            )
            requirements = (
                (self.usdc.address, self.swap_router.router_address, swap_amount),
                (self.asset.address, self.swap_router.router_address, asset_amount),
                (self.usdc.address, self.position_manager.address, remaining_usdc),
                (self.asset.address, self.position_manager.address, asset_amount),
            )
            approvals = self.approval_manager.plan(self.owner, requirements)
            mint = self.position_manager.mint(
                token0=self.token0.address,
                token1=self.token1.address,
                fee=self.fee,
                tick_lower=band.tick_lower,
                tick_upper=band.tick_upper,
                amount0_desired=amount0_desired,
                amount1_desired=amount1_desired,
                amount0_min=self._minimum(amount0_desired),
                amount1_min=self._minimum(amount1_desired),
                recipient=self.owner,
                deadline=self._deadline(),
            )
        except ActionError:
            raise
        except Exception as error:
            reason = str(error) or error.__class__.__name__
            raise ActionError(f"enter Intent 构造失败：{reason}") from error

        for plan in approvals:
            self._execute(plan.intent, "approve", broadcast)
        for scheduled in swaps:
            if scheduled.delay_seconds:
                time.sleep(scheduled.delay_seconds)
            self._execute(scheduled.intent, "swap", broadcast)
        self._execute(mint, "mint", broadcast)

    def rebalance_actions(self, sample, band) -> RebalanceActions:
        """返回沿用 burn 阶段名的 decrease、collect、swap、mint 回调。"""
        initial = self.reader.read(self.owner, spenders=self._spenders)
        position = self._active_position(initial)
        amount0_min, amount1_min = self._decrease_minimums(position, sample)

        def burn(intent_id: str) -> Intent:
            return self.position_manager.decrease_liquidity(
                token_id=position.token_id,
                liquidity=position.liquidity,
                amount0_min=amount0_min,
                amount1_min=amount1_min,
                deadline=self._deadline(),
                intent_id=intent_id,
            )

        def collect(intent_id: str) -> Intent:
            return self.position_manager.collect(
                token_id=position.token_id,
                recipient=self.owner,
                intent_id=intent_id,
            )

        def read_balances() -> BalanceSnapshot:
            current = self.reader.read(self.owner, spenders=self._spenders)
            return BalanceSnapshot(
                token0=self.token0.address,
                token1=self.token1.address,
                amount0_raw=current.balance0_raw,
                amount1_raw=current.balance1_raw,
                token0_decimals=self.token0.decimals,
                token1_decimals=self.token1.decimals,
                price_token1_per_token0=sample.price,
            )

        def build_swap(requirement, intent_ids):
            return self.swap_router.plan_exact_input_single(
                token_in=requirement.token_in,
                token_out=requirement.token_out,
                fee=self.fee,
                recipient=self.owner,
                amount_in=requirement.amount_in,
                amount_usd=requirement.amount_usd,
                slippage_bps=self.swap_policy.max_slippage_bps,
                intent_ids=tuple(intent_ids),
            )

        def mint(intent_id: str) -> Intent:
            current = self.reader.read(self.owner, spenders=self._spenders)
            return self.position_manager.mint(
                token0=self.token0.address,
                token1=self.token1.address,
                fee=self.fee,
                tick_lower=band.tick_lower,
                tick_upper=band.tick_upper,
                amount0_desired=current.balance0_raw,
                amount1_desired=current.balance1_raw,
                amount0_min=self._minimum(current.balance0_raw),
                amount1_min=self._minimum(current.balance1_raw),
                recipient=self.owner,
                deadline=self._deadline(),
                intent_id=intent_id,
            )

        return RebalanceActions(
            burn=burn,
            collect=collect,
            read_balances=read_balances,
            build_swap=build_swap,
            mint=mint,
        )

    def exit(self, sample, *, allow_broadcast: bool = False) -> None:
        """撤出全部流动性，领取资产，清成 USDC 后销毁 NFT。"""
        broadcast = require_broadcast_flag(allow_broadcast)
        initial = self.reader.read(self.owner, spenders=self._spenders)
        position = self._active_position(initial)
        amount0_min, amount1_min = self._decrease_minimums(position, sample)
        try:
            decrease = self.position_manager.decrease_liquidity(
                token_id=position.token_id,
                liquidity=position.liquidity,
                amount0_min=amount0_min,
                amount1_min=amount1_min,
                deadline=self._deadline(),
            )
        except Exception as error:
            raise ActionError(f"decreaseLiquidity Intent 构造失败：{error}") from error
        self._execute(decrease, "decreaseLiquidity", broadcast)

        try:
            collect = self.position_manager.collect(
                token_id=position.token_id,
                recipient=self.owner,
            )
        except Exception as error:
            raise ActionError(f"collect Intent 构造失败：{error}") from error
        self._execute(collect, "collect", broadcast)

        after_collect = self.reader.read(self.owner, spenders=self._spenders)
        asset_balance = self._balance_raw(after_collect, self.asset)
        if asset_balance > 0:
            human_asset = Decimal(asset_balance) / (
                Decimal(10) ** self.asset.decimals
            )
            asset_value_usd = (
                human_asset * sample.price
                if self.asset is self.token0
                else human_asset / sample.price
            )
            try:
                swaps = self.swap_router.plan_exact_input_single(
                    token_in=self.asset.address,
                    token_out=self.usdc.address,
                    fee=self.fee,
                    recipient=self.owner,
                    amount_in=asset_balance,
                    amount_usd=asset_value_usd,
                    slippage_bps=self.swap_policy.max_slippage_bps,
                )
            except Exception as error:
                raise ActionError(f"swap Intent 构造失败：{error}") from error
            for scheduled in swaps:
                if scheduled.delay_seconds:
                    time.sleep(scheduled.delay_seconds)
                self._execute(scheduled.intent, "swap", broadcast)

        try:
            burn = self.position_manager.burn(position.token_id)
        except Exception as error:
            raise ActionError(f"burn Intent 构造失败：{error}") from error
        self._execute(burn, "burn", broadcast)

        if not broadcast:
            return
        completed = self.reader.read(self.owner, spenders=self._spenders)
        remaining = self._balance_raw(completed, self.asset)
        if remaining == 0:
            return
        if remaining < self.dust_threshold_raw:
            LOGGER.warning(
                "撤出后仍有 wASMLx 粉尘：raw=%d，阈值=%d",
                remaining,
                self.dust_threshold_raw,
            )
            return
        raise ActionError(
            "撤出后剩余 wASMLx 敞口："
            f"raw={remaining}，dust_threshold_raw={self.dust_threshold_raw}"
        )


def _capital_limits(path: Path = RISK_PATH) -> tuple[Decimal, Decimal]:
    """只读取建仓所需的两项本金上限，异常时失败关闭。"""
    try:
        root = yaml.safe_load(path.read_text(encoding="utf-8"))
        limits = root["limits"]
        values = tuple(
            Decimal(str(limits[name]))
            for name in ("total_capital_usd", "probe_capital_usd")
        )
    except (OSError, KeyError, TypeError, InvalidOperation, yaml.YAMLError) as error:
        raise ActionError(f"无法读取本金风控配置 {path}：{error}") from error
    if any(not value.is_finite() or value < 0 for value in values):
        raise ActionError("本金风控配置必须是有限非负数")
    return values
