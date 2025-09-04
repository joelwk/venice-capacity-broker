from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests

# Optional: load .env automatically if python-dotenv is installed
try:  # pragma: no cover - optional convenience
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:
    pass

from db.session import get_session, create_db_and_tables
from db.models import AssetToken, TokenSnapshot


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
        """
        try:
            cutoff_ts = int(time.time() - minutes * 60)
            from_block = self.block_number_by_time(cutoff_ts)
            if not from_block:
                return None
            # ERC-20 Transfer signature
            topic0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
            j = self._get(
                {
                    "module": "logs",
                    "action": "getLogs",
                    "address": address,
                    "fromBlock": str(from_block),
                    "toBlock": "latest",
                    "topic0": topic0,
                    "page": "1",
                    "offset": str(max_events),
                    "sort": "desc",
                }
            )
            res = j.get("result")
            if isinstance(res, list):
                return min(len(res), max_events)
            return None
        except Exception:
            return None


def _erc20_metadata_via_web3(address: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    try:
        # Check if BASE_RPC_URL is set before attempting connection
        if not os.getenv("BASE_RPC_URL"):
            if _truthy_env("TOKEN_WATCH_DEBUG"):
                print(f"[token-watcher][debug] BASE_RPC_URL not set, skipping Web3 metadata")
            return None, None, None
            
        from web3 import Web3  # lazy import
        from libs.agentkit_ext.web3_utils import get_contract, get_web3

        if _truthy_env("TOKEN_WATCH_DEBUG"):
            print(f"[token-watcher][debug] Attempting Web3 metadata for {address}")
        
        w3 = get_web3()
        if not w3.is_connected():
            if _truthy_env("TOKEN_WATCH_DEBUG"):
                print(f"[token-watcher][debug] Web3 not connected!")
            return None, None, None
            
        erc20 = get_contract(w3, Web3.to_checksum_address(address), "erc20.json")
        name = str(erc20.functions.name().call())
        symbol = str(erc20.functions.symbol().call())
        decimals = int(erc20.functions.decimals().call())
        
        if _truthy_env("TOKEN_WATCH_DEBUG"):
            print(f"[token-watcher][debug] Web3 metadata success: {symbol}, {name}, {decimals}")
        
        return symbol, name, decimals
    except Exception as e:
        if _truthy_env("TOKEN_WATCH_DEBUG"):
            print(f"[token-watcher][debug] Web3 metadata error for {address}: {type(e).__name__}: {e}")
        return None, None, None


def _truthy_env(name: str, default: str = "false") -> bool:
    return (os.getenv(name) or default).strip().lower() in {"1", "true", "yes", "on"}


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
    if default_qt and _truthy_env("TOKEN_WATCH_DEBUG"):
        print(f"[token-watcher][debug] QUOTE_TOKEN_ADDRESS not set; using default for chain {chain_id}: {default_qt}")
    return default_qt


def _price_via_dex(address: str) -> Optional[float]:
    # Price token in QUOTE_TOKEN_ADDRESS (e.g., USDC) using the DEX aggregator.
    try:
        from services.marketdata.provider import MarketDataProvider

        if _truthy_env("TOKEN_WATCH_DEBUG"):
            print(f"[token-watcher][debug] Attempting DEX price for {address}")
        
        quote = _effective_quote_token_address()
        if not quote:
            if _truthy_env("TOKEN_WATCH_DEBUG"):
                print(f"[token-watcher][debug] QUOTE_TOKEN_ADDRESS not set and no default available")
            return None
        if address.lower() == quote.lower():
            return 1.0
            
        md = MarketDataProvider()
        best = md.best_price([address, quote], amount_in_decimal=1.0)
        # best['price'] is quote per token (e.g., USDC per 1 token)
        price = float(best.get("price"))
        
        if _truthy_env("TOKEN_WATCH_DEBUG"):
            print(f"[token-watcher][debug] DEX price success: {price}")
        
        return price
    except Exception as e:
        if _truthy_env("TOKEN_WATCH_DEBUG"):
            print(f"[token-watcher][debug] DEX price error for {address}: {type(e).__name__}: {e}")
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
            if _truthy_env("TOKEN_WATCH_DEBUG"):
                print(f"[token-watcher][debug] Using known metadata for {address}: {symbol}")
    
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
    
    if _truthy_env("TOKEN_WATCH_DEBUG") and total_supply is not None:
        print(f"[token-watcher][debug] Total supply for {address}: {total_supply}")
    
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
    if _truthy_env("TOKEN_WATCH_DEBUG"):
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
            print("[token-watcher][debug] "+json.dumps(dbg))
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
        now = datetime.utcnow()
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
    if _truthy_env("TOKEN_WATCH_DEBUG"):
        print("[token-watcher][debug] Environment check:")
        print(f"  - BASE_RPC_URL: {os.getenv('BASE_RPC_URL', 'NOT SET')}")
        print(f"  - RPC_URL: {os.getenv('RPC_URL', 'NOT SET')}")
        print(f"  - QUOTE_TOKEN_ADDRESS: {os.getenv('QUOTE_TOKEN_ADDRESS', 'NOT SET')}")
        print(f"  - API endpoint: {base_url}")
        print(f"  - Chain ID: {chain_id}")

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
    print(f"[token-watcher] tracking {len(addrs)} token(s): {', '.join(addrs)}; interval={interval}s")
    while True:
        for addr in addrs:
            try:
                metrics = collect_token_metrics(addr, client)
                persist_metrics(metrics)
                print(f"[token-watcher] {metrics.symbol or ''} {addr[:6]}.. price={metrics.price_usd} holders={metrics.holders} tx24h={metrics.transfers_24h}")
            except Exception as e:  # noqa: BLE001
                print(f"[token-watcher] error for {addr}: {e}")
        if once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    # Quick check for minimal configuration
    if not (os.getenv("ETHERSCAN_API_KEY")):
        print("ERROR: ETHERSCAN_API_KEY")
        print("\nMinimal setup:")
        print("export ETHERSCAN_API_KEY='your_api_key'")
        print("export TOKEN_WATCH_DEBUG='true'  # See what's happening")
        print("\nOptional but recommended:")
        print("export BASE_RPC_URL='https://base.publicnode.com'  # For metadata")
        print("export QUOTE_TOKEN_ADDRESS='0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'  # USDC for pricing")
        sys.exit(1)
    
    run_watch_loop()
