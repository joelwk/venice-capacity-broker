from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

from libs.telemetry.logger import get_logger

# Optional: load .env automatically if python-dotenv is installed
try:  # pragma: no cover - optional convenience
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:
    pass

from db.session import get_session, create_db_and_tables
from db.models import AssetToken, TokenSnapshot


logger = get_logger("marketdata.token_watcher")

# Default to Etherscan V2 unified endpoint and pass chainid for Base (8453)
DEFAULT_ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"

# Known token metadata for Base mainnet (fallback when Web3 unavailable)
KNOWN_TOKENS = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": {"symbol": "USDC", "name": "USD Coin", "decimals": 6},
    "0xf4d97f2da56e8c3098f3a8d538db630a2606a024": {"symbol": "DIEM", "name": "Venice DIEM", "decimals": 18},
    "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf": {"symbol": "VVV", "name": "Venice Finance", "decimals": 18},
}

# Default quote token per chain (used if QUOTE_TOKEN_ADDRESS is not set)
# Currently only Base mainnet is defined; extend as needed.
DEFAULT_QUOTE_TOKEN_BY_CHAIN: Dict[int, str] = {
    # Base mainnet USDC (6 decimals)
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}

# Default bridge token per chain for multi-hop quotes when no direct pool exists
DEFAULT_BRIDGE_TOKEN_BY_CHAIN: Dict[int, str] = {
    # Base WETH
    8453: "0x4200000000000000000000000000000000000006",
}

# In-memory cache for successful pricing paths per token
# Keyed by "<token_addr.lower()>-><quote_addr.lower()>" → (RoutePlan, timestamp)
from libs.dex.routes import RoutePlan, make_route

_PRICE_PATH_CACHE: Dict[str, Tuple[RoutePlan, float]] = {}


def _cache_key(token: str, quote: str) -> str:
    return f"{token.lower()}->{quote.lower()}"


def _cache_get_path(token: str, quote: str) -> Optional[RoutePlan]:
    try:
        ttl = int(os.getenv("PRICE_PATH_CACHE_TTL_SECONDS") or "1800")
    except Exception:
        ttl = 1800
    key = _cache_key(token, quote)
    ent = _PRICE_PATH_CACHE.get(key)
    if not ent:
        return None
    route, ts = ent
    if (time.time() - ts) > ttl:
        _PRICE_PATH_CACHE.pop(key, None)
        return None
    return route


def _cache_set_path(token: str, quote: str, route: RoutePlan) -> None:
    key = _cache_key(token, quote)
    try:
        max_entries = int(os.getenv("PRICE_PATH_CACHE_MAX") or "256")
    except Exception:
        max_entries = 256
    # Evict oldest if over capacity
    if len(_PRICE_PATH_CACHE) >= max_entries:
        try:
            oldest_key = min(_PRICE_PATH_CACHE.items(), key=lambda kv: kv[1][1])[0]
            _PRICE_PATH_CACHE.pop(oldest_key, None)
        except Exception:
            _PRICE_PATH_CACHE.clear()
    _PRICE_PATH_CACHE[key] = (route, float(time.time()))


def _cache_delete(token: str, quote: str) -> None:
    _PRICE_PATH_CACHE.pop(_cache_key(token, quote), None)


@dataclass
class TokenMetrics:
    address: str
    symbol: Optional[str]
    name: Optional[str]
    decimals: Optional[int]
    price_usd: Optional[float]
    supply_total: Optional[int]
    supply_circulating: Optional[int]
    holders: Optional[int]
    transfers_24h: Optional[int]
    marketcap_usd: Optional[float]
    max_total_supply: Optional[int]
    raw: Dict[str, object]


class BaseScanClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_ETHERSCAN_V2_URL, chain_id: Optional[int] = None, timeout_s: int = 20) -> None:
        self.api_key = api_key
        self.base_url = base_url
        # Etherscan V2 requires a chainid parameter; default to Base mainnet (8453)
        if chain_id is None:
            try:
                chain_id = int(os.getenv("ETHERSCAN_CHAIN_ID") or os.getenv("BASE_CHAIN_ID") or 8453)
            except Exception:
                chain_id = 8453
        self.chain_id = chain_id
        self.timeout_s = timeout_s

    def _get(self, params: Dict[str, str]) -> Dict[str, object]:
        # Etherscan V2 style: base_url + module/action with chainid + apikey
        p = {**params, "apikey": self.api_key, "chainid": str(self.chain_id)}
        r = requests.get(self.base_url, params=p, timeout=self.timeout_s)
        r.raise_for_status()
        j = r.json()
        if not isinstance(j, dict):
            raise RuntimeError("unexpected response from BaseScan")
        status = str(j.get("status", ""))
        if status == "0" and j.get("message") not in ("OK", "No transactions found"):
            raise RuntimeError(str(j.get("result") or j))
        return j

    # Optional helper kept for future expansion; not relied upon for core fields
    def token_info(self, address: str) -> Optional[dict]:
        try:
            j = self._get({"module": "token", "action": "tokeninfo", "contractaddress": address})
            res = j.get("result")
            if isinstance(res, dict):
                ti = res.get("tokenInfo") if isinstance(res.get("tokenInfo"), dict) else None
                if ti:
                    out = dict(ti)
                    if isinstance(res.get("price"), dict):
                        out["price"] = res["price"]
                    return out
                return res
            if isinstance(res, list) and res:
                return res[0]
        except Exception:
            pass
        return None

    def total_supply(self, address: str) -> Optional[int]:
        try:
            j = self._get({"module": "stats", "action": "tokensupply", "contractaddress": address})
            res = j.get("result")
            if isinstance(res, str) and res.isdigit():
                return int(res)
            # some responses embed numeric in string
            return int(str(res))
        except Exception:
            return None

    def block_number_by_time(self, ts_epoch: int) -> Optional[int]:
        try:
            j = self._get({"module": "block", "action": "getblocknobytime", "timestamp": str(ts_epoch), "closest": "before"})
            res = j.get("result")
            if isinstance(res, str) and res.isdigit():
                return int(res)
            return int(str(res))
        except Exception:
            return None

    def recent_transfers_count(self, address: str, minutes: int = 1440, max_events: int = 1000) -> Optional[int]:
        """Count Transfer events in the last window via logs/getLogs (v2-compatible).

        Uses block.getblocknobytime to compute fromBlock.
        Supports pagination to count all events up to max_events.
        """
        try:
            cutoff_ts = int(time.time() - minutes * 60)
            from_block = self.block_number_by_time(cutoff_ts)
            if not from_block:
                return None
            # ERC-20 Transfer signature
            topic0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
            
            # Etherscan V2 API limits results to 1000 per page
            page_size = min(1000, max_events)
            total_count = 0
            page = 1
            
            while total_count < max_events:
                j = self._get(
                    {
                        "module": "logs",
                        "action": "getLogs",
                        "address": address,
                        "fromBlock": str(from_block),
                        "toBlock": "latest",
                        "topic0": topic0,
                        "page": str(page),
                        "offset": str(page_size),
                        "sort": "desc",
                    }
                )
                res = j.get("result")
                if not isinstance(res, list) or len(res) == 0:
                    # No more results
                    break
                    
                total_count += len(res)
                
                # If we got less than page_size results, we've reached the end
                if len(res) < page_size:
                    break
                    
                page += 1
                
                # Safety check to prevent infinite loops
                if page > 10:  # Max 10,000 events
                    break
            
            return min(total_count, max_events)
        except Exception as e:
            _debug(f"Error counting transfers: {type(e).__name__}: {e}")
            return None


def _erc20_metadata_via_web3(address: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    try:
        # Check if BASE_RPC_URL is set before attempting connection
        if not os.getenv("BASE_RPC_URL"):
            _debug("BASE_RPC_URL not set, skipping Web3 metadata")
            return None, None, None
            
        from web3 import Web3  # lazy import
        from libs.agentkit_ext.web3_utils import get_contract, get_web3

        _debug(f"Attempting Web3 metadata for {address}")
        
        w3 = get_web3()
        if not w3.is_connected():
            _debug("Web3 not connected")
            return None, None, None
            
        erc20 = get_contract(w3, Web3.to_checksum_address(address), "erc20.json")
        name = str(erc20.functions.name().call())
        symbol = str(erc20.functions.symbol().call())
        decimals = int(erc20.functions.decimals().call())
        
        _debug(f"Web3 metadata success: {symbol}, {name}, {decimals}")
        
        return symbol, name, decimals
    except Exception as e:
        _debug(f"Web3 metadata error for {address}: {type(e).__name__}: {e}")
        return None, None, None


def _truthy_env(name: str, default: str = "false") -> bool:
    return (os.getenv(name) or default).strip().lower() in {"1", "true", "yes", "on"}


def _debug(message: str, *args) -> None:
    if _truthy_env("TOKEN_WATCH_DEBUG"):
        if args:
            logger.debug(message, *args)
        else:
            logger.debug(message)


def _format_price(price: Optional[float]) -> str:
    """Format price in human-readable format, avoiding scientific notation."""
    if price is None:
        return "None"
    if price >= 1.0:
        # For prices >= $1, show 2-4 decimal places
        return f"{price:.4f}".rstrip('0').rstrip('.')
    elif price >= 0.01:
        # For prices >= $0.01, show up to 4 decimal places
        return f"{price:.4f}".rstrip('0').rstrip('.')
    elif price >= 0.0001:
        # For prices >= $0.0001, show up to 6 decimal places
        return f"{price:.6f}".rstrip('0').rstrip('.')
    else:
        # For very small prices, show up to 10 decimal places
        return f"{price:.10f}".rstrip('0').rstrip('.')


def _effective_quote_token_address() -> Optional[str]:
    """Return QUOTE_TOKEN_ADDRESS from env or a chain-specific default.

    The default helps local/dev runs where .env is incomplete. For Base (8453),
    default to USDC. If a default is used, emit a debug note.
    """
    env_qt = os.getenv("QUOTE_TOKEN_ADDRESS")
    if env_qt:
        return env_qt.strip()
    # Determine chain id from env (fall back to Base)
    try:
        chain_id = int(os.getenv("ETHERSCAN_CHAIN_ID") or os.getenv("BASE_CHAIN_ID") or 8453)
    except Exception:
        chain_id = 8453
    default_qt = DEFAULT_QUOTE_TOKEN_BY_CHAIN.get(chain_id)
    if default_qt:
        _debug(f"QUOTE_TOKEN_ADDRESS not set; using default for chain {chain_id}: {default_qt}")
    return default_qt


def _price_via_dex(address: str) -> Optional[float]:
    # Price token in QUOTE_TOKEN_ADDRESS (e.g., USDC) using the DEX aggregator.
    try:
        from services.marketdata.provider import MarketDataProvider

        _debug(f"Attempting DEX price for {address}")

        quote = _effective_quote_token_address()
        if not quote:
            _debug("QUOTE_TOKEN_ADDRESS not set and no default available")
            return None
        if address.lower() == quote.lower():
            return 1.0

        md = MarketDataProvider()

        tried: set[Tuple[Tuple[str, ...], Tuple[Optional[int], ...]]] = set()

        def _route_key(route: RoutePlan) -> Tuple[Tuple[str, ...], Tuple[Optional[int], ...]]:
            return (tuple(route.tokens), tuple(h.fee for h in route.hops))

        def _route_from_tokens(tokens: List[str], fees: Optional[List[Optional[int]]] = None) -> Optional[RoutePlan]:
            try:
                return make_route(tokens, fees)
            except Exception as exc:
                _debug(f"Unable to build route for {tokens} fees={fees}: {type(exc).__name__}: {exc}")
                return None

        def _try_route(route: RoutePlan, label: str) -> Optional[Tuple[float, str]]:
            key = _route_key(route)
            if key in tried:
                return None
            tried.add(key)
            try:
                best = md.best_price(route, amount_in_decimal=1.0)
                price_val = float(best.get("price") or 0.0)
                if price_val <= 0:
                    return None
                provider_val = str(best.get("provider") or "")
                _cache_set_path(address, quote, route)
                _debug(f"{label} route success via {provider_val}: {_format_price(price_val)}")
                return price_val, provider_val
            except Exception as exc:
                _debug(f"{label} route failed: {type(exc).__name__}: {exc}")
                return None

        # Try cached successful path first
        cached = _cache_get_path(address, quote)
        if cached:
            cached_res = _try_route(cached, "cached")
            if cached_res is not None:
                return cached_res[0]
            _cache_delete(address, quote)

        price: Optional[float] = None

        try:
            configured_routes = md._collect_trade_paths()
        except Exception:
            configured_routes = []
        manual_keys = {_route_key(route) for route in configured_routes}

        try:
            candidate_routes = md.route_candidates(address, quote)
        except Exception as exc:
            _debug(f"route_candidates error: {type(exc).__name__}: {exc}")
            candidate_routes = []

        if price is None:
            for route in candidate_routes:
                tokens = getattr(route, "tokens", [])
                if tokens and tokens[0].lower() != address.lower():
                    continue
                label = "configured" if _route_key(route) in manual_keys else "candidate"
                cand = _try_route(route, label)
                if cand is not None:
                    price, _ = cand
                    break

        # Try direct pair using common fee tiers
        if price is None:
            direct_fees: List[Optional[int]] = [None, 500, 1000, 3000, 10000]
            for fee in direct_fees:
                route = _route_from_tokens([address, quote], [fee] if fee is not None else None)
                if not route:
                    continue
                direct_res = _try_route(route, "direct")
                if direct_res is not None:
                    price, _ = direct_res
                    break

        # Try bridge token (e.g., WETH)
        if price is None:
            try:
                bridge_env = os.getenv("DEX_BRIDGE_TOKEN_ADDRESS")
                try:
                    chain_id = int(os.getenv("ETHERSCAN_CHAIN_ID") or os.getenv("BASE_CHAIN_ID") or 8453)
                except Exception:
                    chain_id = 8453
                bridge_default = DEFAULT_BRIDGE_TOKEN_BY_CHAIN.get(chain_id)
                bridge = bridge_env or bridge_default
            except Exception:
                bridge = None

            if bridge and bridge.lower() not in {address.lower(), quote.lower()}:
                for fee_in in (500, 1000, 3000):
                    for fee_out in (500, 1000, 3000):
                        route = _route_from_tokens([address, bridge, quote], [fee_in, fee_out])
                        if not route:
                            continue
                        bridge_res = _try_route(route, "bridge")
                        if bridge_res is not None:
                            price, _ = bridge_res
                            break
                    if price is not None:
                        break

        # Try VVV as alternate intermediate hop
        if price is None:
            vvv_addr = (
                os.getenv("VVV_TOKEN_ADDRESS")
                or next((addr for addr, meta in KNOWN_TOKENS.items() if str(meta.get("symbol", "")).upper() == "VVV"), None)
            )
            if vvv_addr and vvv_addr.lower() not in {address.lower(), quote.lower()}:
                # Shortest path first
                route = _route_from_tokens([address, vvv_addr, quote])
                if route:
                    vvv_res = _try_route(route, "vvv bridge")
                    if vvv_res is not None:
                        price, _ = vvv_res

                if price is None:
                    try:
                        bridge_env = os.getenv("DEX_BRIDGE_TOKEN_ADDRESS")
                        try:
                            chain_id = int(os.getenv("ETHERSCAN_CHAIN_ID") or os.getenv("BASE_CHAIN_ID") or 8453)
                        except Exception:
                            chain_id = 8453
                        bridge_default = DEFAULT_BRIDGE_TOKEN_BY_CHAIN.get(chain_id)
                        bridge = bridge_env or bridge_default
                    except Exception:
                        bridge = None

                    if bridge and bridge.lower() not in {address.lower(), vvv_addr.lower(), quote.lower()}:
                        fee_candidates = [500, 1000, 3000]
                        for fee_in in fee_candidates:
                            for fee_mid in fee_candidates:
                                for fee_out in fee_candidates:
                                    route = _route_from_tokens(
                                        [address, vvv_addr, bridge, quote],
                                        [fee_in, fee_mid, fee_out],
                                    )
                                    if not route:
                                        continue
                                    res = _try_route(route, "vvv bridge+")
                                    if res is not None:
                                        price, _ = res
                                        break
                                if price is not None:
                                    break
                            if price is not None:
                                break

        return price
    except Exception as e:
        _debug(f"DEX price error for {address}: {type(e).__name__}: {e}")
        return None


def collect_token_metrics(address: str, client: BaseScanClient) -> TokenMetrics:
    # Prefer on-chain metadata; keep token_info only for optional fields/raw
    info = client.token_info(address) or {}
    sym2, name2, dec2 = _erc20_metadata_via_web3(address)
    symbol = sym2
    name = name2
    decimals = dec2
    
    # Fallback to known tokens if Web3 unavailable
    if (not symbol or decimals is None):
        known = KNOWN_TOKENS.get(address.lower())
        if known:
            symbol = symbol or known["symbol"]
            name = name or known["name"]
            decimals = decimals if decimals is not None else known["decimals"]
            _debug(f"Using known metadata for {address}: {symbol}")
    
    # Fallback to token_info if web3 metadata is unavailable
    if (not symbol or decimals is None) and isinstance(info, dict):
        try:
            if not symbol:
                symbol = (info.get("symbol") or info.get("tokenSymbol") or None)  # type: ignore[assignment]
            if not name:
                name = (info.get("tokenName") or info.get("name") or None)  # type: ignore[assignment]
            if decimals is None:
                dv = info.get("divisor") or info.get("decimals")
                if dv is not None:
                    decimals = int(str(dv))
        except Exception:
            pass
    _dbg_sources: Dict[str, str] = {}
    _dbg_sources["metadata"] = "web3" if sym2 or dec2 is not None else ("known" if symbol else "unknown")

    # Supply and holders
    total_supply = client.total_supply(address)
    _dbg_sources["supply_total"] = "stats.tokensupply" if total_supply is not None else "none"
    
    if total_supply is not None:
        _debug(f"Total supply for {address}: {total_supply}")
    
    # Holder counts are optional and often gated; skip unless explicitly enabled
    holders: Optional[int] = None
    if _truthy_env("TOKEN_WATCH_ENABLE_HOLDERS"):
        try:
            v = info.get("holders") if isinstance(info, dict) else None
            if v is not None:
                holders = int(str(v))
        except Exception:
            holders = None
    # Configurable cap to avoid heavy responses on hot tokens
    _max_events = 0
    try:
        _max_events = int(os.getenv("TOKEN_WATCH_MAX_EVENTS") or "200")
    except Exception:
        _max_events = 200
    transfers_24h = client.recent_transfers_count(address, minutes=1440, max_events=_max_events)
    _dbg_sources["transfers_24h"] = "logs.getLogs" if isinstance(transfers_24h, int) else "none"

    # Price: attempt to read from tokeninfo.price.rate; else DEX quote
    price_usd: Optional[float] = None
    # Do not rely on tokeninfo for price; use DEX or 1.0 for quote token
    if price_usd is None:
        px = _price_via_dex(address)
        price_usd = px
        if px is not None:
            qt0 = _effective_quote_token_address()
            if qt0 and address.lower() == qt0.lower():
                _dbg_sources["price_usd"] = "quote=1.0"
            else:
                _dbg_sources["price_usd"] = "dex"
        else:
            # If the token is the quote token, set price to 1.0 even without DEX
            qt = _effective_quote_token_address()
            if qt and address.lower() == qt.lower():
                price_usd = 1.0
                _dbg_sources["price_usd"] = "quote=1.0"
            else:
                _dbg_sources["price_usd"] = "none"

    supply_circ: Optional[int] = None
    max_total_supply: Optional[int] = None
    # Circulating/max supply: optional best-effort from token_info
    if isinstance(info, dict):
        try:
            circ = info.get("circulatingSupply") or info.get("circulating_supply")
            if circ is not None:
                supply_circ = int(float(str(circ)))
        except Exception:
            pass
        try:
            m = info.get("maxTotalSupply") or info.get("max_supply")
            if m is not None:
                max_total_supply = int(float(str(m)))
        except Exception:
            pass

    marketcap_usd = None
    try:
        if price_usd is not None and (supply_circ or total_supply):
            base_supply = supply_circ if supply_circ is not None else total_supply
            if base_supply is not None:
                marketcap_usd = float(price_usd) * float(base_supply) / float(10 ** (decimals or 0))
                _dbg_sources["marketcap_usd"] = "computed"
    except Exception:
        pass

    # Optional debug print of sources
    try:
        dbg = {
            "address": address,
            "symbol": symbol,
            "decimals": decimals,
            "total_supply": total_supply,
            "price_usd": price_usd,
            "transfers_24h": transfers_24h,
            "sources": _dbg_sources,
        }
        _debug(json.dumps(dbg))
    except Exception:
        pass

    return TokenMetrics(
        address=address,
        symbol=symbol,
        name=name,
        decimals=decimals,
        price_usd=price_usd,
        supply_total=total_supply,
        supply_circulating=supply_circ,
        holders=holders,
        transfers_24h=transfers_24h,
        marketcap_usd=marketcap_usd,
        max_total_supply=max_total_supply,
        raw=info if isinstance(info, dict) else {},
    )


def persist_metrics(m: TokenMetrics) -> None:
    # Allow disabling create_all in production where Alembic migrations are expected
    if (os.getenv("SQL_CREATE_ALL_ON_START") or "true").strip().lower() in {"1", "true", "yes", "on"}:
        create_db_and_tables()
    with next(get_session()) as s:  # type: ignore[call-arg]
        # Upsert AssetToken
        tok = s.get(AssetToken, m.address)
        now = datetime.now(timezone.utc)
        if tok is None:
            tok = AssetToken(address=m.address, chain="base", symbol=m.symbol, name=m.name, decimals=m.decimals, created_at=now, updated_at=now)
            s.add(tok)
        else:
            changed = False
            if m.symbol and tok.symbol != m.symbol:
                tok.symbol = m.symbol; changed = True
            if m.name and tok.name != m.name:
                tok.name = m.name; changed = True
            if m.decimals is not None and tok.decimals != m.decimals:
                tok.decimals = m.decimals; changed = True
            if changed:
                tok.updated_at = now
        snap = TokenSnapshot(
            token_address=m.address,
            ts=now,
            price_usd=m.price_usd,
            supply_total=m.supply_total,
            supply_circulating=m.supply_circulating,
            holders=m.holders,
            transfers_24h=m.transfers_24h,
            marketcap_usd=m.marketcap_usd,
            max_total_supply=m.max_total_supply,
            raw_json=json.dumps(m.raw) if m.raw else None,
        )
        s.add(snap)
        s.commit()


def run_watch_loop() -> None:
    api_key = os.getenv("ETHERSCAN_API_KEY") 
    if not api_key:
        raise RuntimeError("Set ETHERSCAN_API_KEY in environment")
    # Prefer Etherscan V2 endpoint; allow override for testing
    base_url = os.getenv("ETHERSCAN_API_URL") or os.getenv("BASESCAN_API_URL") or DEFAULT_ETHERSCAN_V2_URL
    try:
        chain_id = int(os.getenv("ETHERSCAN_CHAIN_ID") or os.getenv("BASE_CHAIN_ID") or 8453)
    except Exception:
        chain_id = 8453
    interval = int(os.getenv("TOKEN_WATCH_INTERVAL_SECONDS") or 300)
    
    # Debug environment variables
    _debug("Environment check:")
    _debug(f"  - BASE_RPC_URL: {os.getenv('BASE_RPC_URL', 'NOT SET')}")
    _debug(f"  - RPC_URL: {os.getenv('RPC_URL', 'NOT SET')}")
    _debug(f"  - QUOTE_TOKEN_ADDRESS: {os.getenv('QUOTE_TOKEN_ADDRESS', 'NOT SET')}")
    _debug(f"  - API endpoint: {base_url}")
    _debug(f"  - Chain ID: {chain_id}")
    _debug(f"  - DEX_PROVIDERS: {os.getenv('DEX_PROVIDERS', 'NOT SET')}")
    _debug(f"  - UNISWAP_V2_ROUTER_ADDRESS: {os.getenv('UNISWAP_V2_ROUTER_ADDRESS', 'NOT SET')}")
    _debug(f"  - AERODROME_ROUTER_ADDRESS: {os.getenv('AERODROME_ROUTER_ADDRESS', 'NOT SET')}")
    _debug(f"  - AERODROME_STABLE: {os.getenv('AERODROME_STABLE', 'NOT SET')}")

    # Addresses to track: prefer explicit list, else known env tokens.
    explicit = os.getenv("TOKEN_WATCH_ADDRESSES")
    addrs: List[str] = []
    if explicit:
        addrs = [a.strip() for a in explicit.split(",") if a.strip()]
    else:
        for key in ("VVV_TOKEN_ADDRESS", "DIEM_TOKEN_ADDRESS", "USDC_ADDRESS"):
            v = os.getenv(key)
            if v:
                addrs.append(v.strip())
    if not addrs:
        raise RuntimeError("No token addresses configured. Set TOKEN_WATCH_ADDRESSES or VVV_TOKEN_ADDRESS/DIEM_TOKEN_ADDRESS/USDC_ADDRESS")

    client = BaseScanClient(api_key=api_key, base_url=base_url, chain_id=chain_id)
    once = _truthy_env("TOKEN_WATCH_ONCE") or _truthy_env("WATCH_TOKENS_ONCE")
    logger.info(f"tracking {len(addrs)} token(s): {', '.join(addrs)}; interval={interval}s")
    while True:
        for addr in addrs:
            try:
                metrics = collect_token_metrics(addr, client)
                persist_metrics(metrics)
                logger.info(f"{metrics.symbol or ''} {addr[:6]}.. price={_format_price(metrics.price_usd)} holders={metrics.holders} tx24h={metrics.transfers_24h}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"error for {addr}: {e}")
        if once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    # Quick check for minimal configuration
    if not (os.getenv("ETHERSCAN_API_KEY")):
        logger.error('ETHERSCAN_API_KEY missing; export it before running token watcher')
        logger.info('Minimal setup:')
        logger.info("export ETHERSCAN_API_KEY='your_api_key'")
        logger.info("export TOKEN_WATCH_DEBUG='true'  # See what's happening")
        logger.info('Optional but recommended:')
        logger.info("export BASE_RPC_URL='https://base.publicnode.com'  # For metadata")
        logger.info("export QUOTE_TOKEN_ADDRESS='0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'  # USDC for pricing")
        sys.exit(1)
    
    run_watch_loop()
