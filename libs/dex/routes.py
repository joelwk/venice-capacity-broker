from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple, Union

Address = str


def _normalize_address(addr: Address) -> Address:
    if not isinstance(addr, str):
        raise TypeError("address must be string")
    stripped = addr.strip()
    if not stripped:
        raise ValueError("address must be non-empty")
    if not stripped.startswith("0x"):
        stripped = "0x" + stripped
    return "0x" + stripped[2:].lower()


@dataclass(frozen=True)
class RouteHop:
    token_in: Address
    token_out: Address
    fee: Optional[int] = None

    def normalized(self) -> "RouteHop":
        fee = None if self.fee is None else int(self.fee)
        if fee is not None and (fee < 0 or fee >= 1_000_000):
            raise ValueError("fee must be between 0 and 1,000,000 bps (exclusive)")
        return RouteHop(
            token_in=_normalize_address(self.token_in),
            token_out=_normalize_address(self.token_out),
            fee=fee,
        )


@dataclass(frozen=True)
class RoutePlan:
    hops: Tuple[RouteHop, ...]

    def __post_init__(self) -> None:
        hops_tuple = tuple(self.hops)
        if not hops_tuple:
            raise ValueError("route must contain at least one hop")
        normalized: List[RouteHop] = []
        previous_out: Optional[Address] = None
        for hop in hops_tuple:
            if not isinstance(hop, RouteHop):
                raise TypeError("hops must contain RouteHop instances")
            n = hop.normalized()
            if previous_out and n.token_in != previous_out:
                raise ValueError("route hops must be contiguous")
            normalized.append(n)
            previous_out = n.token_out
        object.__setattr__(self, "hops", tuple(normalized))

    @property
    def tokens(self) -> List[Address]:
        out: List[Address] = []
        for idx, hop in enumerate(self.hops):
            if idx == 0:
                out.append(hop.token_in)
            out.append(hop.token_out)
        return out

    def is_uniswap_v3(self) -> bool:
        return any(h.fee is not None for h in self.hops)

    def ensure_v2(self) -> None:
        if self.is_uniswap_v3():
            raise ValueError("route contains fee tiers and cannot be used as Uniswap V2 path")

    def ensure_v3(self) -> None:
        if not self.is_uniswap_v3():
            raise ValueError("route is missing fee tiers required for Uniswap V3 path")
        if any(h.fee is None for h in self.hops):
            raise ValueError("all hops must include fee for Uniswap V3 path")

    def with_default_fee(self, fee: int) -> "RoutePlan":
        filled: List[RouteHop] = []
        for hop in self.hops:
            filled.append(RouteHop(hop.token_in, hop.token_out, hop.fee if hop.fee is not None else fee))
        return RoutePlan(tuple(filled))

    def to_uniswap_v2_path(self, *, checksum: bool = False) -> List[Address]:
        self.ensure_v2()
        if not checksum:
            return list(self.tokens)
        from web3 import Web3  # type: ignore

        return [Web3.to_checksum_address(addr) for addr in self.tokens]

    def to_uniswap_v3_path_bytes(self, *, reverse: bool = False) -> bytes:
        self.ensure_v3()
        from web3 import Web3  # type: ignore  # noqa: F401 (kept for parity with V2 helper)

        def _addr_to_bytes(address: Address) -> bytes:
            return bytes.fromhex(address[2:])

        segments: List[bytes] = []
        hops = list(self.hops)
        if not reverse:
            segments.append(_addr_to_bytes(hops[0].token_in))
            iterable = hops
        else:
            segments.append(_addr_to_bytes(hops[-1].token_out))
            iterable = list(reversed(hops))
        for hop in iterable:
            fee = hop.fee
            if fee is None:
                raise ValueError("fee is required for Uniswap V3 path encoding")
            segments.append(int(fee).to_bytes(3, byteorder="big"))
            segments.append(_addr_to_bytes(hop.token_out if not reverse else hop.token_in))
        return b"".join(segments)

    def reversed(self) -> "RoutePlan":
        reversed_hops: List[RouteHop] = []
        for hop in reversed(self.hops):
            reversed_hops.append(RouteHop(hop.token_out, hop.token_in, hop.fee))
        return RoutePlan(tuple(reversed_hops))


RouteLike = Union[RoutePlan, Sequence[Address]]


def as_route_plan(route: RouteLike) -> RoutePlan:
    if isinstance(route, RoutePlan):
        return route
    tokens = [ _normalize_address(addr) for addr in route ]
    if len(tokens) < 2:
        raise ValueError("route must include at least two addresses")
    hops = [RouteHop(tokens[i], tokens[i + 1]) for i in range(len(tokens) - 1)]
    return RoutePlan(tuple(hops))


def make_route(tokens: Sequence[Address], fees: Optional[Sequence[Optional[int]]] = None) -> RoutePlan:
    if fees is None:
        fees = [None] * (len(tokens) - 1)
    if len(tokens) - 1 != len(fees):
        raise ValueError("fees length must match hops")
    hops = [
        RouteHop(_normalize_address(tokens[i]), _normalize_address(tokens[i + 1]), fees[i])
        for i in range(len(tokens) - 1)
    ]
    return RoutePlan(tuple(hops))
