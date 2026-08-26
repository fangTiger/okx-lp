"""按 burn、collect、swap、mint 固定顺序执行一次再平衡。"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from okxlp.exec.authorization import require_broadcast_flag
from okxlp.exec.intent import ID_PATTERN, Intent, IntentStatus
from okxlp.strategy.allocation import (
    BalanceSnapshot,
    SwapRequirement,
    calculate_50_50_swap,
    load_min_swap_usd,
    validate_min_swap_usd,
)
from okxlp.uniswap.swap import ScheduledSwap


RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LOGGER = logging.getLogger(__name__)
STAGES = ("burn", "collect", "swap", "mint")
SWAP_INTENT_ID_COUNT = 5


def deterministic_intent_id(rebalance_id: str, stage: str, index: int) -> str:
    """根据再平衡轮次、阶段和序号生成稳定 Intent ID。"""
    payload = f"{rebalance_id}|{stage}|{index}".encode()
    return hashlib.sha256(payload).hexdigest()[:32]


class RebalanceError(RuntimeError):
    """表示再平衡已安全中止。"""
@dataclass(frozen=True)
class RebalanceActions:
    """按阶段延迟构造 Intent，确保失败后不生成后续意图。"""

    burn: Callable[[str], Intent]
    collect: Callable[[str], Intent]
    read_balances: Callable[[], BalanceSnapshot]
    build_swap: Callable[
        [SwapRequirement, tuple[str, ...]], tuple[ScheduledSwap, ...]
    ]
    mint: Callable[[str], Intent]
@dataclass(frozen=True)
class RebalanceProgress:
    """可原子持久化的再平衡进度。"""

    rebalance_id: str
    completed: tuple[str, ...] = ()
    intent_ids: tuple[str, ...] = ()
    failed_stage: str | None = None
    error: str | None = None
class RebalanceJournal:
    """以每轮一个 JSON 文件记录已完成阶段。"""

    def __init__(self, root: Path = Path("log/rebalances")) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, rebalance_id: str) -> Path:
        """返回经过路径穿越防护的进度文件路径。"""
        if RUN_ID_PATTERN.fullmatch(rebalance_id) is None:
            raise ValueError("rebalance_id 格式非法")
        return self.root / f"{rebalance_id}.json"

    def save(self, progress: RebalanceProgress) -> RebalanceProgress:
        """原子写入进度，避免崩溃留下半截 JSON。"""
        path = self.path(progress.rebalance_id)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.root,
                prefix=".rebalance-", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(asdict(progress), handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as error:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise RebalanceError(f"再平衡进度落盘失败：{error}") from None
        return progress

    def load(self, rebalance_id: str) -> RebalanceProgress | None:
        """读取已有进度；损坏或非法内容一律失败关闭。"""
        path = self.path(rebalance_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise RebalanceError(f"再平衡进度记录已损坏：{rebalance_id}") from None
        try:
            if type(payload) is not dict or set(payload) != {
                "rebalance_id", "completed", "intent_ids", "failed_stage", "error"
            }:
                raise ValueError
            if payload["rebalance_id"] != rebalance_id:
                raise ValueError
            completed = payload["completed"]
            intent_ids = payload["intent_ids"]
            failed_stage = payload["failed_stage"]
            error = payload["error"]
            if (
                type(completed) is not list
                or tuple(completed) != STAGES[:len(completed)]
                or type(intent_ids) is not list
                or any(
                    type(item) is not str or ID_PATTERN.fullmatch(item) is None
                    for item in intent_ids
                )
                or failed_stage not in (*STAGES, None)
                or (error is not None and type(error) is not str)
                or (failed_stage is None and error is not None)
            ):
                raise ValueError
            if failed_stage is not None:
                if len(completed) >= len(STAGES) or failed_stage != STAGES[len(completed)]:
                    raise ValueError
                if type(error) is not str or not error:
                    raise ValueError
            return RebalanceProgress(
                rebalance_id=rebalance_id,
                completed=tuple(completed),
                intent_ids=tuple(intent_ids),
                failed_stage=failed_stage,
                error=error,
            )
        except (KeyError, TypeError, ValueError):
            raise RebalanceError(f"再平衡进度字段非法：{rebalance_id}") from None


class RebalanceOrchestrator:
    """逐阶段执行并在任何异常处记录后立即停止。"""

    def __init__(
        self, *, executor: Any, journal: RebalanceJournal,
        sleep: Callable[[float], None] = time.sleep,
        min_swap_usd: Decimal | str | None = None,
        risk_path: Path = Path("config/risk.yaml"),
    ) -> None:
        self.executor = executor
        self.journal = journal
        self.sleep = sleep
        self.min_swap_usd = (
            load_min_swap_usd(risk_path)
            if min_swap_usd is None else validate_min_swap_usd(min_swap_usd)
        )

    def execute(
        self, actions: RebalanceActions, *, allow_broadcast: bool = False,
        rebalance_id: str | None = None,
    ) -> RebalanceProgress:
        """强制四阶段顺序；默认仅 dry-run，不授予广播权限。"""
        broadcast = require_broadcast_flag(allow_broadcast)
        selected_id = uuid4().hex if rebalance_id is None else rebalance_id
        existing = self.journal.load(selected_id)
        progress = existing or self.journal.save(RebalanceProgress(selected_id))
        if progress.failed_stage is not None:
            raise RebalanceError(
                f"上一轮再平衡在 {progress.failed_stage} 阶段失败，"
                "需人工或链上对账后处理"
            )
        stage = "burn"
        try:
            if not self._completed(progress, stage):
                intent_id = deterministic_intent_id(selected_id, stage, 0)
                burn = actions.burn(intent_id)
                self._ensure_intent_id(burn, intent_id)
                progress = self._run_stage(
                    progress, stage, ((burn, 0),), broadcast
                )
            stage = "collect"
            if not self._completed(progress, stage):
                intent_id = deterministic_intent_id(selected_id, stage, 0)
                collect = actions.collect(intent_id)
                self._ensure_intent_id(collect, intent_id)
                progress = self._run_stage(
                    progress, stage, ((collect, 0),), broadcast
                )
            stage = "swap"
            if not self._completed(progress, stage):
                requirement = calculate_50_50_swap(
                    actions.read_balances(), self.min_swap_usd
                )
                intent_ids = tuple(
                    deterministic_intent_id(selected_id, stage, index)
                    for index in range(SWAP_INTENT_ID_COUNT)
                )
                swaps = (
                    () if requirement is None
                    else actions.build_swap(requirement, intent_ids)
                )
                if len(swaps) > len(intent_ids):
                    raise RebalanceError(
                        f"swap 拆单笔数 {len(swaps)} 超过预分配 Intent ID "
                        f"数量 {len(intent_ids)}"
                    )
                for index, item in enumerate(swaps):
                    self._ensure_intent_id(item.intent, intent_ids[index])
                scheduled = tuple(
                    (item.intent, item.delay_seconds) for item in swaps
                )
                progress = self._run_stage(
                    progress, stage, scheduled, broadcast
                )
            stage = "mint"
            if not self._completed(progress, stage):
                intent_id = deterministic_intent_id(selected_id, stage, 0)
                mint = actions.mint(intent_id)
                self._ensure_intent_id(mint, intent_id)
                progress = self._run_stage(
                    progress, stage, ((mint, 0),), broadcast
                )
            return progress
        except BaseException as error:
            reason = str(error) or error.__class__.__name__
            failed = replace(progress, failed_stage=stage, error=reason)
            self.journal.save(failed)
            raise RebalanceError(f"再平衡在 {stage} 阶段中止：{reason}") from error

    @staticmethod
    def _completed(progress: RebalanceProgress, stage: str) -> bool:
        if stage not in progress.completed:
            return False
        LOGGER.info(
            "再平衡 %s 阶段 %s 已完成，跳过", progress.rebalance_id, stage
        )
        return True

    @staticmethod
    def _ensure_intent_id(intent: Intent, expected: str) -> None:
        if intent.intent_id != expected:
            raise RebalanceError(
                f"Intent ID 与预分配值不一致：期望={expected}，"
                f"实际={intent.intent_id}"
            )

    def _run_stage(
        self, progress: RebalanceProgress, stage: str,
        scheduled: tuple[tuple[Intent, int], ...], allow_broadcast: bool,
    ) -> RebalanceProgress:
        broadcast = require_broadcast_flag(allow_broadcast)
        ids = []
        expected = IntentStatus.CONFIRMED if broadcast is True else IntentStatus.DRY_RUN
        for intent, delay in scheduled:
            if delay:
                self.sleep(delay)
            result = self.executor.execute(intent, allow_broadcast=broadcast)
            if result.intent.status != expected:
                raise RebalanceError(
                    f"Intent {intent.intent_id} 状态为 {result.intent.status.value}，期望 {expected.value}"
                )
            ids.append(intent.intent_id)
        updated = replace(
            progress, completed=progress.completed + (stage,),
            intent_ids=progress.intent_ids + tuple(ids),
        )
        return self.journal.save(updated)
