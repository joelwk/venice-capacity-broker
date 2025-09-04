from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from libs.agentkit_ext.web3_utils import get_contract, get_web3
from libs.agentkit_ext.agentkit_wallet import get_address, send_tx


Address = str


@dataclass
class Quote:
    provider: str
    amount_in: int
    amount_out: int
    path: List[Address]


class DexProvider:
    name: str

    def quote(self, amount_in: int, path: List[Address]) -> Optional[Quote]:
        raise NotImplementedError

    def trade(self, amount_in: int, min_amount_out: int, path: List[Address]) -> Dict[str, str]:
        raise NotImplementedError


class UniswapV2DexProvider(DexProvider):
    name = "uniswap_v2"

    def __init__(self, router_address: Address) -> None:
        from web3 import Web3  # type: ignore

        self.w3 = get_web3()
        self.router_addr = Web3.to_checksum_address(router_address)
        self.router = get_contract(self.w3, self.router_addr, "uniswap_v2_router.json")
        # Lazily resolve recipient during trade to avoid wallet requirement for quotes
        self.recipient: Optional[str] = None

    def quote(self, amount_in: int, path: List[Address]) -> Optional[Quote]:
        try:
            amounts = self.router.functions.getAmountsOut(amount_in, path).call()
            return Quote(provider=self.name, amount_in=amount_in, amount_out=int(amounts[-1]), path=path)
        except Exception:
            return None

    def _ensure_allowance(self, token: Address, owner: Address, spender: Address, required: int) -> Optional[str]:
        erc20 = get_contract(self.w3, token, "erc20.json")
        try:
            current = int(erc20.functions.allowance(owner, spender).call())
        except Exception:
            current = 0
        if current >= required:
            return None
        approve_data = erc20.encode_abi(fn_name="approve", args=[spender, required])
        return send_tx(token, bytes.fromhex(approve_data[2:]))

    def trade(self, amount_in: int, min_amount_out: int, path: List[Address]) -> Dict[str, str]:
        # Ensure allowance for input token to router
        token_in = path[0]
        # Resolve recipient lazily
        from web3 import Web3 as _Web3  # type: ignore
        recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = self._ensure_allowance(token_in, recipient, self.router_addr, amount_in) or ""

        deadline = int(time.time()) + 20 * 60
        fn = self.router.functions.swapExactTokensForTokens(amount_in, min_amount_out, path, recipient, deadline)
        built = fn.build_transaction({})  # only need data for AgentKit provider
        tx_hash = send_tx(self.router_addr, built["data"])
        return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}


class AerodromeDexProvider(DexProvider):
    name = "aerodrome"

    def __init__(self, router_address: Address, stable: bool = True) -> None:
        from web3 import Web3  # type: ignore

        self.w3 = get_web3()
        self.router_addr = Web3.to_checksum_address(router_address)
        self.router = get_contract(self.w3, self.router_addr, "aerodrome_router.json")
        self.recipient: Optional[str] = None
        self.stable = stable

    def _routes(self, path: List[Address]) -> List[Tuple[Address, Address, bool]]:
        # Single hop route only for now
        if len(path) != 2:
            raise ValueError("Aerodrome provider currently supports single-hop routes only")
        return [(Web3.to_checksum_address(path[0]), Web3.to_checksum_address(path[1]), bool(self.stable))]

    def quote(self, amount_in: int, path: List[Address]) -> Optional[Quote]:
        try:
            routes = self._routes(path)
            amounts = self.router.functions.getAmountsOut(amount_in, routes).call()
            return Quote(provider=self.name, amount_in=amount_in, amount_out=int(amounts[-1]), path=path)
        except Exception:
            return None

    def trade(self, amount_in: int, min_amount_out: int, path: List[Address]) -> Dict[str, str]:
        token_in = path[0]
        # Ensure allowance for input token to router
        from web3 import Web3 as _Web3  # type: ignore
        erc20_owner = self.recipient or _Web3.to_checksum_address(get_address())
        erc20_spender = self.router_addr
        try:
            erc20 = get_contract(self.w3, token_in, "erc20.json")
            current = int(erc20.functions.allowance(erc20_owner, erc20_spender).call())
        except Exception:
            current = 0
        approve_hash = ""
        if current < amount_in:
            approve_data = erc20.encode_abi(fn_name="approve", args=[self.router_addr, amount_in])
            approve_hash = send_tx(token_in, bytes.fromhex(approve_data[2:]))

        deadline = int(time.time()) + 20 * 60
        fn = self.router.functions.swapExactTokensForTokensSimple(
            amount_in,
            min_amount_out,
            Web3.to_checksum_address(path[0]),
            Web3.to_checksum_address(path[1]),
            bool(self.stable),
            erc20_owner,
            deadline,
        )
        built = fn.build_transaction({})
        tx_hash = send_tx(self.router_addr, built["data"])
        return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}


class DexAggregator:
    def __init__(self, providers: List[DexProvider]) -> None:
        self.providers = providers

    def quote_all(self, amount_in: int, path: List[Address]) -> List[Quote]:
        quotes: List[Quote] = []
        for p in self.providers:
            q = p.quote(amount_in, path)
            if q is not None:
                quotes.append(q)
        return quotes

    def best_quote(self, amount_in: int, path: List[Address]) -> Optional[Quote]:
        quotes = self.quote_all(amount_in, path)
        if not quotes:
            return None
        return max(quotes, key=lambda q: q.amount_out)

    def trade_best(self, amount_in: int, min_out_bps: int, path: List[Address]) -> Dict[str, str]:
        quote = self.best_quote(amount_in, path)
        if quote is None:
            raise RuntimeError("No quotes available from configured DEX providers")
        # Apply slippage tolerance in basis points
        min_out = quote.amount_out * (10_000 - min_out_bps) // 10_000
        # Find the provider instance by name
        provider = next(p for p in self.providers if p.name == quote.provider)
        return provider.trade(amount_in, min_out, path)


def build_aggregator_from_env() -> DexAggregator:
    providers_env = os.getenv("DEX_PROVIDERS", "uniswap_v2,aerodrome")
    provider_names = [p.strip().lower() for p in providers_env.split(",") if p.strip()]
    providers: List[DexProvider] = []

    # Uniswap V2
    if "uniswap_v2" in provider_names:
        uni_addr = os.getenv("UNISWAP_V2_ROUTER_ADDRESS") or os.getenv("ROUTER_ADDRESS")
        if uni_addr:
            providers.append(UniswapV2DexProvider(uni_addr))

    # Aerodrome
    if "aerodrome" in provider_names:
        aero_addr = os.getenv("AERODROME_ROUTER_ADDRESS")
        if aero_addr:
            stable = os.getenv("AERODROME_STABLE", "true").lower() in {"1", "true", "yes", "y"}
            providers.append(AerodromeDexProvider(aero_addr, stable=stable))

    if not providers:
        raise EnvironmentError("No DEX providers configured. Set DEX_PROVIDERS and router envs.")

    return DexAggregator(providers)
