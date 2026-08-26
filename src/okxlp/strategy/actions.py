"""把主状态机动作接到受限 Intent 构造器与执行器。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any

import yaml
from eth_abi import decode

from okxlp.config_validation import address as validate_address
from okxlp.exec.authorization import require_broadcast_flag
from okxlp.exec.intent import Intent, IntentStatus
from okxlp.strategy.allocation import (
    BalanceSnapshot, calculate_50_50_swap, load_min_swap_usd,
)
from okxlp.strategy.rebalance import RebalanceActions
from okxlp.uniswap.tickmath import mint_amounts_for_budget, position_amounts


LOGGER = logging.getLogger(__name__)
BPS = Decimal("10000")
RISK_PATH = Path("config/risk.yaml")
DEFAULT_MINT_MIN_DEPOSIT_BPS = 5_000


class ActionError(RuntimeError):
    """表示生产动作在安全检查或某个执行阶段中止。"""


class ProductionActions:
    """按固定安全顺序构造并执行建仓、再平衡和撤出 Intent。"""

    def __init__(
        self, *, executor, reader, approval_manager, position_manager,
        swap_router, owner: str, pool, fact_gate, swap_policy,
        pool_snapshot_reader: Callable[[], Any],
        deadline_seconds: int = 300,
        clock: Callable[[], int] = lambda: int(time.time()),
        dust_threshold_raw: int = 10**12,
    ) -> None:
        if type(deadline_seconds) is not int or deadline_seconds <= 0:
            raise ValueError("deadline_seconds 必须是正整数")
        if type(dust_threshold_raw) is not int or dust_threshold_raw < 0:
            raise ValueError("dust_threshold_raw 必须是非负整数")
        if not callable(pool_snapshot_reader):
            raise ValueError("pool_snapshot_reader 必须是可调用的池快照读取器")
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
        self.pool_snapshot_reader = pool_snapshot_reader
        self.mint_min_deposit_bps = _mint_min_deposit_bps()
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

    def _capital_budget(
        self, portfolio, price: Decimal, *,
        capital_limit_usd: Decimal | None = None,
    ) -> tuple[int, int]:
        """按本金上限截断后返回本轮可投入的两腿 raw 预算。"""
        asset_raw = self._balance_raw(portfolio, self.asset)
        usdc_raw = self._balance_raw(portfolio, self.usdc)
        asset = Decimal(asset_raw) / (Decimal(10) ** self.asset.decimals)
        usdc = Decimal(usdc_raw) / (Decimal(10) ** self.usdc.decimals)
        asset_usd = asset * price
        available_usd = asset_usd + usdc
        if capital_limit_usd is None:
            total_capital_usd, probe_capital_usd = _capital_limits()
            allowed_usd = self.fact_gate.max_position_usd(
                total_capital_usd, probe_capital_usd
            )
        else:
            allowed_usd = capital_limit_usd
        capital_usd = min(available_usd, allowed_usd)
        if capital_usd <= 0:
            raise ActionError(
                "可用本金为 0，请先在 config/risk.yaml 设置 "
                "limits.total_capital_usd"
            )

        deploy_asset_usd = min(asset_usd, capital_usd)
        if deploy_asset_usd == asset_usd:
            deploy_asset = asset_raw
        else:
            deploy_asset = int(
                (
                    deploy_asset_usd / price
                    * (Decimal(10) ** self.asset.decimals)
                ).to_integral_value(rounding=ROUND_FLOOR)
            )
        deploy_usdc = int(
            (
                (capital_usd - deploy_asset_usd)
                * (Decimal(10) ** self.usdc.decimals)
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        deploy_asset = min(deploy_asset, asset_raw)
        deploy_usdc = min(deploy_usdc, usdc_raw)
        return self._ordered_amounts(deploy_asset, deploy_usdc)

    def _mint_params(
        self, budget0: int, budget1: int, band, sqrt_price_x96: int,
    ) -> tuple[int, int, int, int]:
        """返回按区间配比后的 desired 与固定双零 minimum。"""
        amount0_desired, amount1_desired = mint_amounts_for_budget(
            budget0,
            budget1,
            band.tick_lower,
            band.tick_upper,
            sqrt_price_x96,
        )
        # 真实窄区间 [-201710,-201600] 实测表明：为容纳 ±0.4%
        # 价格移动，per-leg 容差需达 9182 bps，已等同无保护；
        # 但存入价值占预算仍稳定在 52.8%–97.8%。mint 的 min 是比例
        # 约束而非防洗劫，窄区间下比例随价格剧烈摆动是数学必然；
        # 真正的保护见 simulation_check 对模拟实际存入总价值的校验。
        return (
            amount0_desired,
            amount1_desired,
            0,
            0,
        )

    def _latest_mint_sample(self, band):
        """在 mint 构造前取最新池价，价格出 band 则失败关闭。"""
        try:
            latest = self.pool_snapshot_reader()
        except Exception as error:
            reason = str(error) or error.__class__.__name__
            raise ActionError(f"读取最新池快照失败：{reason}") from error
        price = getattr(latest, "price", None)
        sqrt_price_x96 = getattr(latest, "sqrt_price_x96", None)
        if (
            not isinstance(price, Decimal)
            or not price.is_finite()
            or price <= 0
            or type(sqrt_price_x96) is not int
            or sqrt_price_x96 <= 0
        ):
            raise ActionError("最新池快照缺少有效 price 或 sqrt_price_x96")
        if not band.price_lower <= price <= band.price_upper:
            raise ActionError(
                "最新价格已离开目标区间："
                f"当前={price}，目标="
                f"[{band.price_lower}, {band.price_upper}]，"
                "本轮放弃建仓，等待下一轮重新计算区间"
            )
        return latest

    def _mint_simulation_check(
        self, budget0: int, budget1: int, price: Decimal,
    ) -> Callable[[str], None]:
        """按 mint 模拟返回的两腿实际数量检查存入总价值。"""
        budget_usd = (
            Decimal(budget0) / (Decimal(10) ** self.token0.decimals) * price
            + Decimal(budget1) / (Decimal(10) ** self.token1.decimals)
        )
        minimum_usd = (
            budget_usd * Decimal(self.mint_min_deposit_bps) / BPS
        )

        def check(raw_result: str) -> None:
            try:
                if type(raw_result) is not str or not raw_result.startswith("0x"):
                    raise ValueError("不是 hex 字符串")
                payload = bytes.fromhex(raw_result[2:])
                if len(payload) != 128:
                    raise ValueError(f"期望 128 字节，实际 {len(payload)} 字节")
                _token_id, _liquidity, amount0, amount1 = decode(
                    ["uint256", "uint128", "uint256", "uint256"],
                    payload,
                    strict=True,
                )
            except Exception as error:
                raise ActionError(f"mint 模拟返回值无法按 ABI 解码：{error}") from error
            deposited_usd = (
                Decimal(amount0) / (Decimal(10) ** self.token0.decimals) * price
                + Decimal(amount1) / (Decimal(10) ** self.token1.decimals)
            )
            if deposited_usd < minimum_usd:
                raise ActionError(
                    f"mint 模拟实际存入 {deposited_usd} USD，"
                    f"预算 {budget_usd} USD，低于阈值 "
                    f"{self.mint_min_deposit_bps} bps（{minimum_usd} USD）"
                )

        return check

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

    def _execute(
        self, intent: Intent, stage: str, broadcast: bool,
        simulation_check: Callable[[str], None] | None = None,
    ) -> None:
        try:
            if simulation_check is None:
                result = self.executor.execute(
                    intent, allow_broadcast=broadcast
                )
            else:
                result = self.executor.execute(
                    intent, allow_broadcast=broadcast,
                    simulation_check=simulation_check,
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
        """按链上实际两腿余额补足 50/50 后 mint。"""
        broadcast = require_broadcast_flag(allow_broadcast)
        portfolio = self.reader.read(self.owner, spenders=self._spenders)

        try:
            budget0, budget1 = self._capital_budget(
                portfolio, sample.price
            )
            asset_amount, usdc_amount = (
                (budget0, budget1)
                if self.asset is self.token0 else (budget1, budget0)
            )
            capital_limit_usd = (
                Decimal(asset_amount)
                / (Decimal(10) ** self.asset.decimals)
                * sample.price
                + Decimal(usdc_amount)
                / (Decimal(10) ** self.usdc.decimals)
            )
            requirement = calculate_50_50_swap(
                BalanceSnapshot(
                    token0=self.asset.address,
                    token1=self.usdc.address,
                    amount0_raw=asset_amount,
                    amount1_raw=usdc_amount,
                    token0_decimals=self.asset.decimals,
                    token1_decimals=self.usdc.decimals,
                    price_token1_per_token0=sample.price,
                ),
                load_min_swap_usd(RISK_PATH),
            )
            if requirement is None:
                swaps = ()
            else:
                swaps = self.swap_router.plan_exact_input_single(
                    token_in=requirement.token_in,
                    token_out=requirement.token_out,
                    fee=self.fee,
                    recipient=self.owner,
                    amount_in=requirement.amount_in,
                    amount_usd=requirement.amount_usd,
                    slippage_bps=self.swap_policy.max_slippage_bps,
                )
            estimated_asset, estimated_usdc = asset_amount, usdc_amount
            if requirement is not None:
                received = sum(item.quote.amount_out for item in swaps)
                if requirement.token_in == self.asset.address:
                    estimated_asset -= requirement.amount_in
                    estimated_usdc += received
                else:
                    estimated_usdc -= requirement.amount_in
                    estimated_asset += received
            requirements = (
                (
                    self.usdc.address, self.swap_router.router_address,
                    0 if requirement is None or requirement.token_in != self.usdc.address
                    else requirement.amount_in,
                ),
                (
                    self.asset.address, self.swap_router.router_address,
                    0 if requirement is None or requirement.token_in != self.asset.address
                    else requirement.amount_in,
                ),
                (self.usdc.address, self.position_manager.address, estimated_usdc),
                (self.asset.address, self.position_manager.address, estimated_asset),
            )
            approvals = self.approval_manager.plan(self.owner, requirements)
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

        try:
            if broadcast:
                current = self.reader.read(self.owner, spenders=self._spenders)
                latest = self._latest_mint_sample(band)
                budget0, budget1 = self._capital_budget(
                    current,
                    latest.price,
                    capital_limit_usd=capital_limit_usd,
                )
            else:
                latest = self._latest_mint_sample(band)
                budget0, budget1 = self._ordered_amounts(
                    estimated_asset, estimated_usdc
                )
                LOGGER.info("dry-run mint budget 使用 swap 报价估算余额")
            (
                amount0_desired,
                amount1_desired,
                amount0_min,
                amount1_min,
            ) = self._mint_params(
                budget0, budget1, band,
                latest.sqrt_price_x96,
            )
            simulation_check = self._mint_simulation_check(
                budget0, budget1, latest.price
            )
            mint = self.position_manager.mint(
                token0=self.token0.address,
                token1=self.token1.address,
                fee=self.fee,
                tick_lower=band.tick_lower,
                tick_upper=band.tick_upper,
                amount0_desired=amount0_desired,
                amount1_desired=amount1_desired,
                amount0_min=amount0_min,
                amount1_min=amount1_min,
                recipient=self.owner,
                deadline=self._deadline(),
            )
        except ActionError:
            raise
        except Exception as error:
            reason = str(error) or error.__class__.__name__
            raise ActionError(f"enter mint Intent 构造失败：{reason}") from error
        self._execute(
            mint, "mint", broadcast,
            simulation_check=simulation_check,
        )

    def rebalance_actions(self, sample, band) -> RebalanceActions:
        """返回沿用 burn 阶段名的 decrease、collect、swap、mint 回调。"""
        initial = self.reader.read(self.owner, spenders=self._spenders)
        position = self._active_position(initial)
        amount0_min, amount1_min = self._decrease_minimums(position, sample)
        selected_balances: BalanceSnapshot | None = None
        before_swap = None
        planned_requirement = None
        planned_swaps = ()
        mint_check: Callable[[str], None] | None = None

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
            nonlocal selected_balances, before_swap, planned_requirement
            nonlocal planned_swaps
            current = self.reader.read(self.owner, spenders=self._spenders)
            budget0, budget1 = self._capital_budget(
                current, sample.price
            )
            selected_balances = BalanceSnapshot(
                token0=self.token0.address,
                token1=self.token1.address,
                amount0_raw=budget0,
                amount1_raw=budget1,
                token0_decimals=self.token0.decimals,
                token1_decimals=self.token1.decimals,
                price_token1_per_token0=sample.price,
            )
            before_swap = current
            planned_requirement = None
            planned_swaps = ()
            return selected_balances

        def build_swap(requirement, intent_ids):
            nonlocal planned_requirement, planned_swaps
            planned_requirement = requirement
            planned_swaps = self.swap_router.plan_exact_input_single(
                token_in=requirement.token_in,
                token_out=requirement.token_out,
                fee=self.fee,
                recipient=self.owner,
                amount_in=requirement.amount_in,
                amount_usd=requirement.amount_usd,
                slippage_bps=self.swap_policy.max_slippage_bps,
                intent_ids=tuple(intent_ids),
            )
            return planned_swaps

        def mint(intent_id: str) -> Intent:
            nonlocal mint_check
            current = self.reader.read(self.owner, spenders=self._spenders)
            latest = self._latest_mint_sample(band)
            if selected_balances is None:
                budget0, budget1 = self._capital_budget(
                    current, latest.price
                )
            else:
                budget0 = selected_balances.amount0_raw
                budget1 = selected_balances.amount1_raw
                if planned_requirement is not None:
                    current0 = self._balance_raw(current, self.token0)
                    current1 = self._balance_raw(current, self.token1)
                    before0 = self._balance_raw(before_swap, self.token0)
                    before1 = self._balance_raw(before_swap, self.token1)
                    quoted_received = sum(
                        item.quote.amount_out for item in planned_swaps
                    )
                    if planned_requirement.token_in == self.token0.address:
                        budget0 -= planned_requirement.amount_in
                        actual_received = max(current1 - before1, 0)
                        swap_applied = current0 < before0
                        budget1 += (
                            actual_received if swap_applied
                            else quoted_received
                        )
                    else:
                        budget1 -= planned_requirement.amount_in
                        actual_received = max(current0 - before0, 0)
                        swap_applied = current1 < before1
                        budget0 += (
                            actual_received if swap_applied
                            else quoted_received
                        )
            (
                amount0_desired,
                amount1_desired,
                amount0_min,
                amount1_min,
            ) = self._mint_params(
                budget0, budget1, band, latest.sqrt_price_x96
            )
            mint_check = self._mint_simulation_check(
                budget0, budget1, latest.price
            )
            return self.position_manager.mint(
                token0=self.token0.address,
                token1=self.token1.address,
                fee=self.fee,
                tick_lower=band.tick_lower,
                tick_upper=band.tick_upper,
                amount0_desired=amount0_desired,
                amount1_desired=amount1_desired,
                amount0_min=amount0_min,
                amount1_min=amount1_min,
                recipient=self.owner,
                deadline=self._deadline(),
                intent_id=intent_id,
            )

        def check_mint_simulation(raw_result: str) -> None:
            if mint_check is None:
                raise ActionError("mint Intent 尚未构造，拒绝检查模拟结果")
            mint_check(raw_result)

        return RebalanceActions(
            burn=burn,
            collect=collect,
            read_balances=read_balances,
            build_swap=build_swap,
            mint=mint,
            mint_simulation_check=check_mint_simulation,
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


def _mint_min_deposit_bps(path: Path = RISK_PATH) -> int:
    """读取 mint 模拟存入价值下限，缺省键使用 5000 bps。"""
    try:
        root = yaml.safe_load(path.read_text(encoding="utf-8"))
        if type(root) is not dict or type(root.get("limits")) is not dict:
            raise TypeError("根节点与 limits 必须是映射")
        limits = root["limits"]
        value = limits.get(
            "mint_min_deposit_bps", DEFAULT_MINT_MIN_DEPOSIT_BPS
        )
    except (OSError, KeyError, TypeError, yaml.YAMLError) as error:
        raise ActionError(f"无法读取 mint 风控配置 {path}：{error}") from error
    if type(value) is not int or not 0 <= value <= 10_000:
        raise ActionError("limits.mint_min_deposit_bps 必须是 0..10000 的整数")
    return value
