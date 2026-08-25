"""Uniswap V3 池的只读 ABI 调用与结构化快照。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from okxlp.uniswap.tickmath import sqrt_price_x96_to_price


SELECTORS = {
    "token0": "0x0dfe1681",
    "token1": "0xd21220a7",
    "fee": "0xddca3f43",
    "tick_spacing": "0xd0c93a7c",
    "liquidity": "0x1a686502",
    "slot0": "0x3850c7bd",
    "factory": "0xc45a0155",
    "symbol": "0x95d89b41",
    "decimals": "0x313ce567",
    "name": "0x06fdde03",
    "balance_of": "0x70a08231",
}


def _word(hex_value: str, index: int = 0) -> str:
    value = hex_value[2:] if hex_value.startswith("0x") else hex_value
    word = value[index * 64 : (index + 1) * 64]
    if len(word) != 64:
        raise ValueError(f"ABI 返回值缺少第 {index} 个字")
    return word


def decode_int(word: str, *, signed: bool = False, bits: int = 256) -> int:
    """解码 ABI 整数，支持符号扩展后的有符号值。"""
    value = int(word, 16)
    if signed and value >= 1 << (bits - 1):
        value -= 1 << bits
    return value


def _decode_address(hex_value: str) -> str:
    return "0x" + _word(hex_value)[-40:].lower()


def _decode_string(hex_value: str) -> str:
    value = hex_value[2:] if hex_value.startswith("0x") else hex_value
    if len(value) == 64:
        return bytes.fromhex(value).rstrip(b"\x00").decode("utf-8", "replace")
    if len(value) < 128:
        raise ValueError("ABI 字符串返回值过短")
    offset = int(value[:64], 16) * 2
    length = int(value[offset : offset + 64], 16) * 2
    start = offset + 64
    return bytes.fromhex(value[start : start + length]).decode("utf-8", "replace")


@dataclass(frozen=True)
class TokenMetadata:
    """代币元数据及池合约持有余额。"""

    address: str
    symbol: str
    name: str
    decimals: int
    balance_raw: int

    @property
    def balance(self) -> Decimal:
        """返回按 decimals 修正的人类单位余额。"""
        return Decimal(self.balance_raw) / (Decimal(10) ** self.decimals)


@dataclass(frozen=True)
class PoolSnapshot:
    """固定在同一区块读取的池状态。"""

    block: int
    address: str
    factory: str
    fee: int
    tick_spacing: int
    sqrt_price_x96: int
    tick: int
    active_liquidity: int
    token0: TokenMetadata
    token1: TokenMetadata

    @property
    def price(self) -> Decimal:
        """返回每单位 token0 对应的 token1 人类价格。"""
        return sqrt_price_x96_to_price(
            self.sqrt_price_x96, self.token0.decimals, self.token1.decimals
        )


class UniswapV3Pool:
    """在指定 RPC 客户端上读取一个 Uniswap V3 池。"""

    def __init__(self, rpc: Any, address: str) -> None:
        if len(address) != 42 or not address.startswith("0x"):
            raise ValueError("池地址格式无效")
        self.rpc = rpc
        self.address = address.lower()

    def _call(self, selector: str, block: str) -> str:
        return self.rpc.eth_call(self.address, selector, block)

    def _read_token(self, address: str, block: str) -> TokenMetadata:
        symbol = _decode_string(self.rpc.eth_call(address, SELECTORS["symbol"], block))
        name = _decode_string(self.rpc.eth_call(address, SELECTORS["name"], block))
        decimals = decode_int(_word(self.rpc.eth_call(address, SELECTORS["decimals"], block)))
        balance_data = SELECTORS["balance_of"] + self.address[2:].rjust(64, "0")
        balance_raw = decode_int(_word(self.rpc.eth_call(address, balance_data, block)))
        return TokenMetadata(address, symbol, name, decimals, balance_raw)

    def snapshot(self) -> PoolSnapshot:
        """在一个确定区块读取完整池快照。"""
        self.rpc.ensure_chain_id()
        block_number = self.rpc.block_number()
        block = hex(block_number)
        token0_address = _decode_address(self._call(SELECTORS["token0"], block))
        token1_address = _decode_address(self._call(SELECTORS["token1"], block))
        slot0 = self._call(SELECTORS["slot0"], block)
        tick = decode_int(_word(slot0, 1), signed=True, bits=256)
        if not -887272 <= tick <= 887272:
            raise ValueError(f"tick 越界：{tick}")
        return PoolSnapshot(
            block=block_number,
            address=self.address,
            factory=_decode_address(self._call(SELECTORS["factory"], block)),
            fee=decode_int(_word(self._call(SELECTORS["fee"], block))),
            tick_spacing=decode_int(
                _word(self._call(SELECTORS["tick_spacing"], block)), signed=True, bits=256
            ),
            sqrt_price_x96=decode_int(_word(slot0, 0)),
            tick=tick,
            active_liquidity=decode_int(_word(self._call(SELECTORS["liquidity"], block))),
            token0=self._read_token(token0_address, block),
            token1=self._read_token(token1_address, block),
        )
