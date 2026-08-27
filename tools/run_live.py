"""具备三重广播门控的生产状态机唯一入口。"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.campaign.gate import load_fact_gate
from okxlp.campaign.verifier import verify_campaign
from okxlp.chain.calldata_policy import CalldataPolicy
from okxlp.chain.gas import GasEstimator, load_gas_policy
from okxlp.chain.nonce import NonceManager
from okxlp.chain.rpc import JsonRpcClient
from okxlp.chain.signer_process import RemoteSigner
from okxlp.chain.whitelist import TransactionWhitelist
from okxlp.config import load_config
from okxlp.config_validation import address as validate_address
from okxlp.exec.approval import ApprovalManager
from okxlp.exec.authorization import (
    RunMode, load_run_mode, require_broadcast_flag,
)
from okxlp.exec.executor import TransactionExecutor
from okxlp.exec.intent import IntentStatus, IntentStore
from okxlp.exec.reconcile import reconcile_on_startup
from okxlp.market.sessions import MarketSessions
from okxlp.strategy.actions import ProductionActions
from okxlp.strategy.allocation import quote_value
from okxlp.strategy.machine import MainStateMachine
from okxlp.strategy.machine_journal import TransitionJournal
from okxlp.strategy.machine_state import (
    MachineSnapshot, MachineState, MachineStateStore, PriceBand,
)
from okxlp.strategy.machine_types import MarketSample, RiskDecision
from okxlp.strategy.nav import NavRecorder, NavSnapshot
from okxlp.strategy.outrange import OutrangeDetector
from okxlp.strategy.rebalance import RebalanceJournal, RebalanceOrchestrator
from okxlp.strategy.risk_gate import ProductionRiskGate, RebalanceCounter
from okxlp.uniswap.pool import SELECTORS as POOL_SELECTORS
from okxlp.uniswap.pool import UniswapV3Pool, _word, decode_int
from okxlp.uniswap.portfolio import PortfolioReader
from okxlp.uniswap.position import PositionManager
from okxlp.uniswap.swap import SwapPolicy, SwapRouter
from okxlp.uniswap.tickmath import (
    position_amounts, sqrt_price_x96_to_price, tick_to_price,
)


POOLS_PATH = Path("config/pools.yaml")
RISK_PATH = Path("config/risk.yaml")
EXECUTION_PATH = Path("config/execution.yaml")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOTENV_PATH = Path(".env")
PASSWORD_ENV = "OKXLP_KEYSTORE_PASSWORD"
WIDTH_TEXT = "±0.5%"
IGNORE_SESSIONS_WARNING = (
    "⚠ 时段闸门已停用：将在标的交易时段内继续做市"
    "（违反定稿 D4，由用户显式要求）"
)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskSettings:
    """生产入口实际使用的简版 M9 配置。"""

    total_capital_usd: Decimal
    max_rebalances_per_day: int
    halt_file: Path
    confirm_seconds: int
    pin_timeout: int


@dataclass(frozen=True)
class ExecutionAddresses:
    npm: str
    router: str
    quoter: str


@dataclass(frozen=True)
class RuntimePaths:
    """单池生产运行的全部持久化路径。"""

    machine_state: Path
    transition_journal: Path
    rebalance_journal: Path
    rebalance_counter: Path
    nav_root: Path


def _runtime_paths(pool_id: str, root: Path = Path("log")) -> RuntimePaths:
    """按池 ID 隔离状态、再平衡记录、计数与 NAV。"""
    return RuntimePaths(
        machine_state=root / f"machine_state_{pool_id}.json",
        transition_journal=root / f"machine_{pool_id}.jsonl",
        rebalance_journal=root / "rebalances" / pool_id,
        rebalance_counter=root / f"rebalance_count_{pool_id}.json",
        nav_root=root / "nav" / pool_id,
    )


def load_risk_settings(path: Path = RISK_PATH) -> RiskSettings:
    """严格读取生产入口所需风控字段，异常时失败关闭。"""
    try:
        root = yaml.safe_load(path.read_text(encoding="utf-8"))
        limits = root["limits"]
        breakers = root["circuit_breakers"]
        outrange = root["outrange"]
        capital = Decimal(str(limits["total_capital_usd"]))
        maximum = limits["max_rebalances_per_day"]
        halt = breakers["halt_file"]
        confirm = outrange["confirm_seconds"]
        timeout = outrange["pin_timeout"]
    except (
        OSError, KeyError, TypeError, InvalidOperation, yaml.YAMLError,
    ) as error:
        raise ValueError(f"无法读取生产风控配置 {path}：{error}") from None
    if not capital.is_finite() or capital < 0:
        raise ValueError("limits.total_capital_usd 必须是有限非负数")
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("limits.max_rebalances_per_day 必须是正整数")
    if type(halt) is not str or not halt.strip():
        raise ValueError("circuit_breakers.halt_file 必须是非空路径")
    if any(type(value) is not int or value <= 0 for value in (confirm, timeout)):
        raise ValueError("outrange 确认时间必须是正整数")
    return RiskSettings(capital, maximum, Path(halt), confirm, timeout)


def _execution_addresses(path: Path = EXECUTION_PATH) -> ExecutionAddresses:
    try:
        root = yaml.safe_load(path.read_text(encoding="utf-8"))
        addresses = root["addresses"]
        values = tuple(
            validate_address(addresses[name], f"addresses.{name}")
            for name in ("npm", "swap_router02", "quoter_v2")
        )
    except (OSError, KeyError, TypeError, yaml.YAMLError) as error:
        raise ValueError(f"无法读取执行地址配置 {path}：{error}") from None
    return ExecutionAddresses(*values)


def _sample_at_block(rpc, pool, block: int) -> MarketSample:
    slot0 = rpc.eth_call(pool.address, POOL_SELECTORS["slot0"], hex(block))
    sqrt_price_x96 = decode_int(_word(slot0, 0))
    tick = decode_int(_word(slot0, 1), signed=True, bits=256)
    price = sqrt_price_x96_to_price(
        sqrt_price_x96, pool.token0.decimals, pool.token1.decimals
    )
    return MarketSample(price, tick, sqrt_price_x96)


class PoolMarket:
    """每轮返回完整同区块池快照，供决策与撤流动性共同使用。"""

    def __init__(self, reader: UniswapV3Pool) -> None:
        self.reader = reader

    def snapshot(self, _now: datetime) -> MarketSample:
        snapshot = self.reader.snapshot()
        return MarketSample(
            snapshot.price, snapshot.tick, snapshot.sqrt_price_x96
        )


def _pool_quote_value(
    pool, amount0: int, amount1: int, price: Decimal
) -> Decimal:
    """把池的两腿金额统一折算为显式计价腿单位。"""
    return quote_value(
        amount0,
        amount1,
        price,
        pool.token0.decimals,
        pool.token1.decimals,
        pool.quote_leg == "token1",
    )


def _nav_snapshot(rpc, reader, pool, owner, spenders) -> tuple[NavSnapshot, Any]:
    portfolio = reader.read(owner, spenders=spenders)
    sample = _sample_at_block(rpc, pool, portfolio.block)
    position0 = 0
    position1 = 0
    for position in portfolio.positions:
        amount0, amount1 = position_amounts(
            position.liquidity, position.tick_lower,
            position.tick_upper, sample.sqrt_price_x96,
        )
        position0 += amount0
        position1 += amount1
    position_value = _pool_quote_value(
        pool, position0, position1, sample.price
    )
    idle_value = _pool_quote_value(
        pool, portfolio.balance0_raw, portfolio.balance1_raw, sample.price
    )
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return NavSnapshot(
        ts=now, block=portfolio.block, price=str(sample.price),
        position_value_usdc=str(position_value),
        idle0_raw=portfolio.balance0_raw, idle1_raw=portfolio.balance1_raw,
        total_usdc=str(position_value + idle_value),
    ), portfolio


class LiveRuntime:
    """逐次调用状态机单轮运行，并在每轮后完成生产观察动作。"""

    def __init__(
        self, *, machine, risk_gate, signer, executor, policy, reader, rpc,
        pool, owner, spenders, nav_recorder, printer,
    ) -> None:
        self.machine = machine
        self.risk_gate = risk_gate
        self.signer = signer
        self.executor = executor
        self.policy = policy
        self.reader, self.rpc, self.pool = reader, rpc, pool
        self.owner, self.spenders = owner, spenders
        self.nav_recorder = nav_recorder
        self.printer = printer

    def _synchronize_token_ids(self, token_ids: frozenset[int]) -> None:
        """按链上集合同步主进程与签名子进程的 tokenId 策略。"""
        old_policy = self.policy
        normalized = frozenset(token_ids)
        if normalized == old_policy.allowed_token_ids:
            return
        new_policy = old_policy.with_token_ids(normalized)
        self.executor.replace_calldata_policy(new_policy)
        try:
            self.signer.refresh_token_ids(normalized)
        except BaseException:
            # RemoteSigner 对响应不确定的通信失败会直接关闭子进程；若子进程
            # 明确拒绝则不会更新。此处回滚主进程，避免留下单边已更新状态。
            self.executor.replace_calldata_policy(old_policy)
            raise
        self.policy = new_policy
        self.printer(
            "主进程与签名策略 tokenId 已同步："
            f"{sorted(old_policy.allowed_token_ids)} → {sorted(normalized)}"
        )

    def run(self, *, allow_broadcast: bool, max_iterations: int | None) -> None:
        broadcast = require_broadcast_flag(allow_broadcast)
        completed = 0
        while max_iterations is None or completed < max_iterations:
            previous = self.machine.state
            self.machine.run(allow_broadcast=broadcast, max_iterations=1)
            current = self.machine.state
            now = datetime.now(timezone.utc)
            if previous is MachineState.REBALANCING and current is MachineState.IN_RANGE:
                count = self.risk_gate.record_rebalance(now)
                self.printer(f"已记录当日第 {count} 次再平衡")
            nav, portfolio = _nav_snapshot(
                self.rpc, self.reader, self.pool, self.owner, self.spenders
            )
            # 同轮 NAV 已通过 PortfolioReader 取得链上事实，直接复用该快照；
            # 只有状态发生转移才同步，稳态 IN_RANGE → IN_RANGE 不增加 RPC。
            if previous is not current:
                self._synchronize_token_ids(portfolio.token_ids)
            written = self.nav_recorder.record(nav)
            self.printer(
                f"本轮状态：{previous.value} → {current.value}；"
                f"NAV {'已记录' if written else '因节流跳过'}"
            )
            completed += 1

    def close(self) -> None:
        self.signer.close()


def _ensure_startup_write_allowed(
    risk_gate, allow_broadcast: bool,
) -> RiskDecision | None:
    """广播 approve 前复用生产闸门；撤出专用权限仍可补授权。"""
    broadcast = require_broadcast_flag(allow_broadcast)
    if not broadcast:
        return None
    decision = risk_gate.check(datetime.now(timezone.utc))
    if not decision.allowed and not decision.allow_exit:
        raise PermissionError(f"启动授权被风控闸门拒绝：{decision.reason}")
    return decision


def _approval_requirements(policy, pool, result, decision) -> tuple:
    """按风控结论收窄启动授权范围。"""
    if decision is None or decision.allowed:
        return tuple(
            (token, spender, policy.max_approval_raw[token])
            for token in (policy.token0, policy.token1)
            for spender in (policy.npm_address, policy.router_address)
        )
    if not decision.allow_exit or result.active_position is None:
        return ()
    base = pool.base_token.address
    return ((base, policy.router_address, policy.max_approval_raw[base]),)


class LiveBootstrap:
    """保存已完成签名校验的启动资源，确认后才执行 approve。"""

    def __init__(
        self, *, config, pool, rpc, reader, result, policy, signer,
        executor, fact_gate, settings, addresses, market, sessions, printer,
    ) -> None:
        self.config, self.pool, self.rpc = config, pool, rpc
        self.reader, self.result, self.policy = reader, result, policy
        self.signer, self.executor, self.fact_gate = signer, executor, fact_gate
        self.settings, self.addresses, self.market = settings, addresses, market
        self.sessions = sessions
        self.printer = printer
        self.current_sample = market.snapshot(datetime.now(timezone.utc))

    @property
    def current_price(self) -> Decimal:
        return self.current_sample.price

    def finish(self, *, allow_broadcast: bool) -> LiveRuntime:
        """执行受同一广播门控保护的授权，再组装主状态机。"""
        broadcast = require_broadcast_flag(allow_broadcast)
        paths = _runtime_paths(self.pool.pool_id)
        risk_gate = ProductionRiskGate(
            halt_file=self.settings.halt_file,
            fact_gate=self.fact_gate,
            counter=RebalanceCounter(paths.rebalance_counter),
            max_rebalances_per_day=self.settings.max_rebalances_per_day,
        )
        decision = _ensure_startup_write_allowed(risk_gate, broadcast)
        requirements = _approval_requirements(
            self.policy, self.pool, self.result, decision,
        )
        approvals = ApprovalManager(
            reader=self.reader, policy=self.policy
        )
        plans = approvals.plan(self.result.snapshot.owner, requirements)
        expected = IntentStatus.CONFIRMED if broadcast else IntentStatus.DRY_RUN
        if not plans:
            self.printer("授权检查：全部额度充足")
        for plan in plans:
            self.printer(
                f"授权不足：token={plan.token} spender={plan.spender} "
                f"current={plan.current} target={plan.target}"
            )
            outcome = self.executor.execute(
                plan.intent, allow_broadcast=broadcast
            )
            if outcome.intent.status is not expected:
                raise RuntimeError(
                    f"approve 返回状态 {outcome.intent.status.value}，"
                    f"期望 {expected.value}"
                )

        swap_policy = SwapPolicy.from_config(RISK_PATH)
        actions = ProductionActions(
            executor=self.executor, reader=self.reader,
            approval_manager=approvals,
            position_manager=PositionManager(self.policy.npm_address),
            swap_router=SwapRouter(
                rpc=self.rpc, router_address=self.addresses.router,
                quoter_address=self.addresses.quoter, policy=swap_policy,
            ),
            owner=self.result.snapshot.owner, pool=self.pool,
            fact_gate=self.fact_gate, swap_policy=swap_policy,
            pool_snapshot_reader=lambda: _sample_at_block(
                self.rpc, self.pool, self.rpc.block_number()
            ),
        )
        state_store = MachineStateStore(paths.machine_state)
        _sync_machine_state(
            state_store, self.result.active_position, self.pool
        )
        rebalancer = RebalanceOrchestrator(
            executor=self.executor,
            journal=RebalanceJournal(paths.rebalance_journal),
            quote_is_token1=self.pool.quote_leg == "token1",
        )
        machine = MainStateMachine(
            pool_id=self.pool.pool_id,
            sessions=self.sessions,
            risk_gate=risk_gate, market=self.market,
            actions=actions, rebalancer=rebalancer,
            detector=OutrangeDetector(
                confirm_seconds=self.settings.confirm_seconds,
                pin_timeout=self.settings.pin_timeout,
            ),
            state_store=state_store,
            transition_journal=TransitionJournal(paths.transition_journal),
            tick_spacing=self.pool.tick_spacing,
            token0_decimals=self.pool.token0.decimals,
            token1_decimals=self.pool.token1.decimals,
        )
        return LiveRuntime(
            machine=machine, risk_gate=risk_gate, signer=self.signer,
            executor=self.executor, policy=self.policy,
            reader=self.reader, rpc=self.rpc, pool=self.pool,
            owner=self.result.snapshot.owner,
            spenders=(self.addresses.npm, self.addresses.router),
            nav_recorder=NavRecorder(paths.nav_root),
            printer=self.printer,
        )

    def close(self) -> None:
        self.signer.close()


def _sync_machine_state(state_store, active_position, pool) -> None:
    """以链上头寸修正可判定状态；再平衡阶段继续失败关闭。"""
    current = state_store.load()
    if current.failure is not None:
        raise RuntimeError(
            f"本地状态处于阶段锁停：{current.failure}，需人工处理"
        )
    if current.state is MachineState.REBALANCING:
        raise RuntimeError(
            f"本地状态停留在过渡阶段 {current.state.value}，需人工对账"
        )
    if current.state is MachineState.ENTERING:
        if active_position is None:
            LOGGER.warning(
                "本地 ENTERING 但链上无头寸，判定建仓未完成，"
                "按链上事实复位为 IDLE"
            )
            state_store.save(MachineSnapshot(MachineState.IDLE))
            return
        LOGGER.warning(
            "本地 ENTERING 且链上有流动性头寸，判定建仓已完成，"
            "按链上真实区间复位为 IN_RANGE"
        )
        state_store.save(MachineSnapshot(
            MachineState.IN_RANGE,
            _position_band(active_position, pool),
        ))
        return
    if current.state is MachineState.EXITING:
        if active_position is not None:
            LOGGER.warning(
                "本地 EXITING 且链上仍有流动性头寸，判定撤出未完成，"
                "保持 EXITING 由主循环继续撤出"
            )
            return
        LOGGER.warning(
            "本地 EXITING 但链上无头寸，判定撤出已完成，"
            "按链上事实复位为 IDLE"
        )
        state_store.save(MachineSnapshot(MachineState.IDLE))
        return
    if active_position is None:
        if current.state is not MachineState.IDLE:
            state_store.save(MachineSnapshot(MachineState.IDLE))
        return
    if current.state is MachineState.OUT_PENDING:
        return
    state_store.save(MachineSnapshot(
        MachineState.IN_RANGE,
        _position_band(active_position, pool),
    ))


def _position_band(active_position, pool) -> PriceBand:
    """只用链上头寸 tick 重建持久化价格区间。"""
    return PriceBand(
        active_position.tick_lower, active_position.tick_upper,
        tick_to_price(
            active_position.tick_lower,
            pool.token0.decimals, pool.token1.decimals,
        ),
        tick_to_price(
            active_position.tick_upper,
            pool.token0.decimals, pool.token1.decimals,
        ),
    )


def _render_reconcile(result, printer: Callable[[str], None]) -> None:
    active = result.active_position
    printer(
        "启动对账："
        f"block={result.snapshot.block} "
        f"token_ids={sorted(result.token_ids)} "
        "active=" + (
            "None" if active is None else
            f"tokenId={active.token_id},liquidity={active.liquidity}"
        )
    )
    if not result.warnings:
        printer("对账 warnings：无")
    for warning in result.warnings:
        printer(f"对账 warning：{warning}")


def _market_sessions(args, pool) -> MarketSessions:
    """把生产入口的显式停用参数传给时段状态机。"""
    return MarketSessions.from_files(
        pool_id=pool.pool_id,
        ignore_listings=args.ignore_sessions,
    )


def create_bootstrap(
    args, mode: RunMode, settings: RiskSettings,
    printer: Callable[[str], None],
) -> LiveBootstrap:
    """按校验、对账、策略、签名的固定顺序准备生产资源。"""
    config = load_config(POOLS_PATH)
    pool = config.find_pool(args.pool_id)
    sessions = _market_sessions(args, pool)
    rpc = JsonRpcClient(
        config.chain.rpc_urls,
        chain_id=config.chain.chain_id,
        run_mode=mode,
    )
    fact_gate = load_fact_gate()
    fact_gate.log_startup()
    report = verify_campaign(config, rpc)
    printer(
        f"活动启动校验通过：池={','.join(report.verified_pool_ids)}，"
        f"block={report.block}"
    )
    addresses = _execution_addresses()
    reader = PortfolioReader(
        rpc, npm_address=addresses.npm,
        token0=pool.token0.address, token1=pool.token1.address,
        fee=int(pool.fee_bps * 100),
    )
    result = reconcile_on_startup(
        reader, args.owner,
        spenders=(addresses.npm, addresses.router),
    )
    _render_reconcile(result, printer)
    policy = CalldataPolicy.from_config(
        EXECUTION_PATH, POOLS_PATH,
        executor_address=args.owner,
        allowed_token_ids=result.token_ids,
        pool_id=args.pool_id,
    )
    signer: RemoteSigner | None = None
    try:
        key_source = (
            {"dotenv_path": args.dotenv}
            if args.dotenv is not None
            else {
                "keystore_path": args.keystore,
                "password_env": PASSWORD_ENV,
            }
        )
        signer = RemoteSigner(
            chain_id=config.chain.chain_id,
            execution_path=EXECUTION_PATH,
            calldata_policy=policy,
            **key_source,
        )
        _ensure_owner(signer, args.owner)
        executor = TransactionExecutor(
            rpc=rpc, signer=signer,
            nonce_manager=NonceManager(rpc, signer.address),
            gas_estimator=GasEstimator(rpc, load_gas_policy(EXECUTION_PATH)),
            whitelist=TransactionWhitelist.from_config(EXECUTION_PATH),
            calldata_policy=policy, store=IntentStore(),
            chain_id=config.chain.chain_id, printer=printer,
        )
        market = PoolMarket(UniswapV3Pool(rpc, pool.address))
        return LiveBootstrap(
            config=config, pool=pool, rpc=rpc, reader=reader,
            result=result, policy=policy, signer=signer,
            executor=executor, fact_gate=fact_gate, settings=settings,
            addresses=addresses, market=market, sessions=sessions,
            printer=printer,
        )
    except BaseException:
        if signer is not None:
            signer.close()
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="X Layer LP 生产状态机入口（默认不广播）"
    )
    parser.add_argument("--owner", required=True, help="生产钱包地址")
    parser.add_argument(
        "--pool-id", help="目标池配置 ID；缺省使用首个池"
    )
    key_source = parser.add_mutually_exclusive_group()
    key_source.add_argument(
        "--keystore", type=Path,
        help="keystore 路径",
    )
    key_source.add_argument(
        "--dotenv", type=Path,
        help="明文私钥 .env 路径",
    )
    parser.add_argument(
        "--broadcast", action="store_true",
        help="显式请求广播；仍需 mode=live 与交互确认",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="仅与 --broadcast 同用时跳过交互确认",
    )
    parser.add_argument(
        "--ignore-sessions", action="store_true",
        help="显式停用上市地交易时段闸门",
    )
    parser.add_argument("--max-iterations", type=int)
    return parser


def parse_args(
    argv: list[str] | None = None, *, project_root: Path = PROJECT_ROOT,
):
    """解析互斥密钥来源，并处理项目根 `.env` 的唯一默认值。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.keystore is None and args.dotenv is None:
        default_dotenv = project_root / DEFAULT_DOTENV_PATH
        if default_dotenv.exists():
            args.dotenv = default_dotenv.resolve()
        else:
            parser.error(
                "必须显式指定 --keystore 或 --dotenv；项目根不存在 .env"
            )
    return args


def _banner(args, mode, settings, allow_broadcast, printer) -> None:
    printer("=== OKX LP 生产入口 ===")
    printer(f"owner：{args.owner}")
    printer(f"运行模式：{mode.value}")
    printer(f"是否允许广播：{allow_broadcast}")
    if args.ignore_sessions:
        printer(IGNORE_SESSIONS_WARNING)
        LOGGER.warning(IGNORE_SESSIONS_WARNING)
    printer(f"本金上限：{settings.total_capital_usd} USDC")
    printer(f"区间宽度：{WIDTH_TEXT}")
    printer(f"每日再平衡上限：{settings.max_rebalances_per_day}")
    printer(f"HALT 文件：{settings.halt_file}")
    if args.dotenv is not None:
        printer(f"签名来源：dotenv；路径：{args.dotenv}")
    else:
        printer(f"签名来源：keystore；路径：{args.keystore}")


def _ensure_owner(signer, owner: str) -> None:
    actual = validate_address(signer.address.lower(), "signer.address")
    expected = validate_address(owner.lower(), "owner")
    if actual != expected:
        raise RuntimeError(
            f"signer.address 与 --owner 不一致：signer={actual}，owner={expected}"
        )


def main(
    argv: list[str] | None = None, *,
    run_mode_loader: Callable[[], RunMode] | None = None,
    risk_loader: Callable[[], RiskSettings] | None = None,
    bootstrap_factory: Callable[..., Any] | None = None,
    input_fn: Callable[[str], str] = input,
    printer: Callable[[str], None] = print,
) -> int:
    """启动生产循环；任何退出路径都回收独立签名子进程。"""
    args = parse_args(argv)
    bootstrap = None
    runtime = None
    try:
        args.owner = validate_address(args.owner, "owner")
        if args.max_iterations is not None and args.max_iterations <= 0:
            raise ValueError("--max-iterations 必须是正整数")
        mode = (run_mode_loader or load_run_mode)()
        if args.broadcast and mode is RunMode.DRY_RUN:
            printer(
                "拒绝启动：config/risk.yaml 当前为 mode: dry_run，"
                "必须先由人工改为 mode: live 才能使用 --broadcast"
            )
            return 2
        allow_broadcast = bool(args.broadcast and mode is RunMode.LIVE)
        settings = (risk_loader or load_risk_settings)()
        _banner(args, mode, settings, allow_broadcast, printer)
        factory = bootstrap_factory or create_bootstrap
        bootstrap = factory(args, mode, settings, printer)
        _ensure_owner(bootstrap.signer, args.owner)
        if allow_broadcast and not args.yes:
            printer(
                "实盘确认信息："
                f"owner={args.owner}；本金上限={settings.total_capital_usd} USDC；"
                f"区间宽度={WIDTH_TEXT}；当前池价={bootstrap.current_price}"
            )
            printer("请输入“我确认实盘”四个字；其他输入将退出")
            if input_fn("实盘确认：") != "我确认实盘":
                printer("实盘确认不匹配，已退出；广播交易数=0")
                return 2
        runtime = bootstrap.finish(allow_broadcast=allow_broadcast)
        runtime.run(
            allow_broadcast=allow_broadcast,
            max_iterations=args.max_iterations,
        )
        return 0
    except KeyboardInterrupt:
        printer("收到 KeyboardInterrupt，正在关闭签名子进程")
        return 130
    except Exception as error:
        printer(f"启动或运行失败：{error}")
        return 2
    finally:
        if runtime is not None:
            runtime.close()
        elif bootstrap is not None:
            bootstrap.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    raise SystemExit(main())
