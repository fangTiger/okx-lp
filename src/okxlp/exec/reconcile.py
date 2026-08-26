"""启动时读取链上账户状态，并以链上结果作为唯一事实来源。"""

from __future__ import annotations

from dataclasses import dataclass

from okxlp.uniswap.portfolio import OwnedPosition, PortfolioSnapshot


class ReconcileError(RuntimeError):
    """表示链上头寸处于无法自动选择的异常状态。"""


@dataclass(frozen=True)
class ReconcileResult:
    """启动时以链上为准的账户状态。"""

    snapshot: PortfolioSnapshot
    token_ids: frozenset[int]
    active_position: OwnedPosition | None
    warnings: tuple[str, ...]


def reconcile_on_startup(reader, owner, *, spenders) -> ReconcileResult:
    """读取同区块账户快照，并拒绝多个仍有流动性的本池头寸。"""
    snapshot = reader.read(owner, spenders=spenders)
    liquid_positions = tuple(
        position for position in snapshot.positions if position.liquidity > 0
    )
    if len(liquid_positions) > 1:
        raise ReconcileError(
            "本池流动性大于 0 的头寸数为 "
            f"{len(liquid_positions)}，必须人工处理后再启动"
        )

    warnings = []
    if len(snapshot.positions) > 1:
        warnings.append(
            f"本池头寸数为 {len(snapshot.positions)}，正常只应有一个"
        )
    if snapshot.other_pool_position_count > 0:
        warnings.append(
            f"owner 持有的其他池头寸数为 {snapshot.other_pool_position_count}"
        )
    active_position = (
        max(snapshot.positions, key=lambda item: item.liquidity)
        if snapshot.positions
        else None
    )
    if active_position is not None and active_position.liquidity == 0:
        active_position = None
    return ReconcileResult(
        snapshot=snapshot,
        token_ids=snapshot.token_ids,
        active_position=active_position,
        warnings=tuple(warnings),
    )
