"""读取真实 X Layer 池状态并执行一次无广播的 M7 决策。"""

from __future__ import annotations

import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.campaign.gate import load_fact_gate
from okxlp.chain.rpc import JsonRpcClient
from okxlp.config import load_config
from okxlp.exec.intent import Intent
from okxlp.market.sessions import MarketSessions
from okxlp.strategy.machine import MainStateMachine, MarketSample, RiskDecision
from okxlp.strategy.machine_journal import TransitionJournal
from okxlp.strategy.machine_state import MachineStateStore
from okxlp.strategy.outrange import OutrangeDetector
from okxlp.strategy.rebalance import BalanceSnapshot, RebalanceActions
from okxlp.uniswap.pool import UniswapV3Pool


NPM = "0x315e413a11ab0df498ef83873012430ca36638ae"
TOKEN0 = "0x9147b03c16b18fc4f686f610f189f91ddf4347b4"
TOKEN1 = "0xb6ceceab302e2e4948951ee7843fc24e92933061"


class FixedMarket:
    """把同一区块的真实快照固定给本轮状态机。"""

    def __init__(self, sample: MarketSample) -> None:
        self.sample = sample

    def snapshot(self, _now: datetime) -> MarketSample:
        """返回已经读取的真实市场样本。"""
        return self.sample


class DryRunRiskGate:
    """组合现有事实闸门与紧急 HALT 文件。"""

    def __init__(self, halt_file: Path) -> None:
        self.fact_gate = load_fact_gate()
        self.halt_file = halt_file

    def check(self, _now: datetime) -> RiskDecision:
        """写链否决项冻结动作；规模项不阻止探针仓决策。"""
        if self.halt_file.exists():
            return RiskDecision(False, f"检测到紧急停止文件 {self.halt_file}")
        try:
            self.fact_gate.ensure_write_allowed()
        except PermissionError as error:
            return RiskDecision(False, str(error))
        if self.fact_gate.size_blockers:
            ids = "、".join(item.fact_id for item in self.fact_gate.size_blockers)
            return RiskDecision(True, f"事实闸门放行，{ids} 仅限制探针仓规模")
        return RiskDecision(True, "事实闸门与紧急停止检查均放行")


class NoBroadcastActions:
    """验收工具专用动作，任何阶段都只打印而不执行交易。"""

    def enter(self, _sample, band, *, allow_broadcast=False) -> None:
        """打印建仓意图并拒绝广播授权。"""
        _reject_broadcast(allow_broadcast)
        print(f"动作预览：买入一半标的后 mint [{band.tick_lower}, {band.tick_upper}]")

    def rebalance_actions(self, sample, _band):
        """返回签名与拆单回调均符合生产接口的预览动作。"""
        preview = lambda selector, intent_id: Intent.create(
            NPM, selector, intent_id=intent_id
        )
        return RebalanceActions(
            burn=lambda intent_id: preview("0x0c49ccbe", intent_id),
            collect=lambda intent_id: preview("0xfc6f7865", intent_id),
            read_balances=lambda: BalanceSnapshot(
                TOKEN0, TOKEN1, 0, 0, 18, 6, str(sample.price)
            ),
            build_swap=lambda _requirement, _intent_ids: (),
            mint=lambda intent_id: preview("0x88316456", intent_id),
        )

    def exit(self, _sample, *, allow_broadcast=False) -> None:
        """打印撤出意图并拒绝广播授权。"""
        _reject_broadcast(allow_broadcast)
        print("动作预览：burn → collect → 全部换成 USDC")


class NoBroadcastRebalancer:
    """只打印 M6 固定顺序，不构造或发送交易。"""

    def execute(self, _actions, *, allow_broadcast=False) -> None:
        """拒绝广播并打印固定再平衡顺序。"""
        _reject_broadcast(allow_broadcast)
        print("动作预览：burn → collect → swap → mint")


def _reject_broadcast(allow_broadcast: bool) -> None:
    if allow_broadcast:
        raise PermissionError("M7 dry-run 工具永久禁止广播")


def _risk_config(path: Path = Path("config/risk.yaml")) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if type(data) is not dict:
        raise ValueError("risk.yaml 根节点必须是映射")
    return data


def main() -> None:
    """运行一次真实链只读决策并打印全部依据。"""
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    now = datetime.now(timezone.utc)
    config = load_config()
    pool_config = config.find_pool()
    rpc = JsonRpcClient(config.chain.rpc_urls, chain_id=config.chain.chain_id)
    snapshot = UniswapV3Pool(rpc, pool_config.address).snapshot()
    sample = MarketSample(
        snapshot.price, snapshot.tick, snapshot.sqrt_price_x96
    )
    risk_config = _risk_config()
    outrange = risk_config["outrange"]
    halt_file = Path(risk_config["circuit_breakers"]["halt_file"])
    sessions = MarketSessions.from_files(pool_id=pool_config.pool_id)
    risk_gate = DryRunRiskGate(halt_file)
    with tempfile.TemporaryDirectory(prefix="okxlp-m7-dry-run-") as directory:
        root = Path(directory)
        machine = MainStateMachine(
            pool_id=pool_config.pool_id, sessions=sessions, risk_gate=risk_gate,
            market=FixedMarket(sample), actions=NoBroadcastActions(),
            rebalancer=NoBroadcastRebalancer(),
            detector=OutrangeDetector(
                confirm_seconds=int(outrange["confirm_seconds"]),
                pin_timeout=int(outrange["pin_timeout"]),
            ),
            state_store=MachineStateStore(root / "state.json"),
            transition_journal=TransitionJournal(root / "machine.log"),
            tick_spacing=snapshot.tick_spacing,
            token0_decimals=snapshot.token0.decimals,
            token1_decimals=snapshot.token1.decimals,
        )
        result = machine.step()
    print("模式：dry-run；allow_broadcast=False；广播交易数=0")
    print(f"链上区块：{snapshot.block}")
    print(f"池价：{snapshot.price} USDC；tick={snapshot.tick}")
    print(f"时段结论：{'做市' if result.should_make_market else '撤出'}")
    print(f"风控结论：{'放行' if result.risk_allowed else '否决'}")
    print(f"状态决策：IDLE → {result.state.value}")
    print(f"决策理由：{result.reason}")


if __name__ == "__main__":
    main()
