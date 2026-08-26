"""NPM 头寸、两腿余额与 ERC20 授权的同区块只读快照。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from okxlp.config_validation import address as validate_address
from okxlp.uniswap.pool import _decode_address, _word, decode_int


SELECTORS = {
    "balance_of": "0x70a08231",
    "token_of_owner_by_index": "0x2f745c59",
    "positions": "0x99fbab88",
    "allowance": "0xdd62ed3e",
    "owner_of": "0x6352211e",
}
MAX_POSITION_COUNT = 50


def _address_word(address: str) -> str:
    return address[2:].rjust(64, "0")


def _uint_word(value: int) -> str:
    return f"{value:064x}"


def _position_address(data: str, index: int) -> str:
    return _decode_address(_word(data, index))


@dataclass(frozen=True)
class OwnedPosition:
    """本地址持有的一个 NPM 头寸（已确认属于目标池）。"""

    token_id: int
    token0: str
    token1: str
    fee: int
    tick_lower: int
    tick_upper: int
    liquidity: int


@dataclass(frozen=True)
class PortfolioSnapshot:
    """固定在同一区块读取的账户链上状态。"""

    block: int
    owner: str
    positions: tuple[OwnedPosition, ...]
    other_pool_position_count: int
    balance0_raw: int
    balance1_raw: int
    allowances: dict[tuple[str, str], int]

    @property
    def token_ids(self) -> frozenset[int]:
        """返回属于目标池的全部 NPM tokenId。"""
        return frozenset(position.token_id for position in self.positions)

    def allowance_of(self, token: str, spender: str) -> int:
        """返回指定代币对 spender 的额度；未读取的组合按零处理。"""
        return self.allowances.get((token.lower(), spender.lower()), 0)

    def has_sufficient_allowance(
        self, token: str, spender: str, needed: int
    ) -> bool:
        """判断授权额度是否达到所需原始数量，恰好相等视为充足。"""
        return self.allowance_of(token, spender) >= needed


class PortfolioReader:
    """读取一个地址在目标 Uniswap V3 池的账户状态。"""

    def __init__(
        self,
        rpc: Any,
        *,
        npm_address: str,
        token0: str,
        token1: str,
        fee: int,
    ) -> None:
        self.rpc = rpc
        self.npm_address = validate_address(npm_address, "npm_address")
        self.token0 = validate_address(token0, "token0")
        self.token1 = validate_address(token1, "token1")
        if type(fee) is not int or not 0 <= fee < 1 << 24:
            raise ValueError("fee 必须是 uint24 整数")
        self.fee = fee

    def _call(self, to: str, data: str, block: str) -> str:
        return self.rpc.eth_call(to, data, block)

    def _read_uint(self, to: str, data: str, block: str) -> int:
        return decode_int(_word(self._call(to, data, block)))

    def _read_position(self, token_id: int, block: str) -> OwnedPosition:
        data = self._call(
            self.npm_address,
            SELECTORS["positions"] + _uint_word(token_id),
            block,
        )
        encoded = data[2:] if data.startswith("0x") else data
        if len(encoded) != 12 * 64:
            raise ValueError("positions 返回值必须恰好包含 12 个 ABI 字")
        return OwnedPosition(
            token_id=token_id,
            token0=_position_address(data, 2),
            token1=_position_address(data, 3),
            fee=decode_int(_word(data, 4)),
            tick_lower=decode_int(_word(data, 5), signed=True, bits=256),
            tick_upper=decode_int(_word(data, 6), signed=True, bits=256),
            liquidity=decode_int(_word(data, 7)),
        )

    def read(
        self, owner: str, *, spenders: Sequence[str] = ()
    ) -> PortfolioSnapshot:
        """在一个确定区块读取 owner 的头寸、余额与指定授权。"""
        owner = validate_address(owner, "owner")
        normalized_spenders = tuple(
            validate_address(spender, f"spenders[{index}]")
            for index, spender in enumerate(spenders)
        )
        self.rpc.ensure_chain_id()
        block_number = self.rpc.block_number()
        block = hex(block_number)
        owner_word = _address_word(owner)

        count = self._read_uint(
            self.npm_address, SELECTORS["balance_of"] + owner_word, block
        )
        if count > MAX_POSITION_COUNT:
            raise ValueError(
                "地址持有的 NPM 头寸数量超过 50，说明配置或地址可能错误"
            )

        token_ids = tuple(
            self._read_uint(
                self.npm_address,
                SELECTORS["token_of_owner_by_index"]
                + owner_word
                + _uint_word(index),
                block,
            )
            for index in range(count)
        )
        positions = []
        other_pool_position_count = 0
        for token_id in token_ids:
            position = self._read_position(token_id, block)
            if (
                position.token0 == self.token0
                and position.token1 == self.token1
                and position.fee == self.fee
            ):
                positions.append(position)
            else:
                other_pool_position_count += 1

        balance_data = SELECTORS["balance_of"] + owner_word
        balance0_raw = self._read_uint(self.token0, balance_data, block)
        balance1_raw = self._read_uint(self.token1, balance_data, block)
        allowances = {}
        for token in (self.token0, self.token1):
            for spender in normalized_spenders:
                allowance_data = (
                    SELECTORS["allowance"] + owner_word + _address_word(spender)
                )
                allowances[(token, spender)] = self._read_uint(
                    token, allowance_data, block
                )

        return PortfolioSnapshot(
            block=block_number,
            owner=owner,
            positions=tuple(positions),
            other_pool_position_count=other_pool_position_count,
            balance0_raw=balance0_raw,
            balance1_raw=balance1_raw,
            allowances=allowances,
        )
