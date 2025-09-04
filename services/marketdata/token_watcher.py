from __future__ import annotations

import json
import os
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

    def token_info(self, address: str) -> Optional[dict]:
        try:
            j = self._get({"module": "token", "action": "tokeninfo", "contractaddress": address})
            res = j.get("result")
            if isinstance(res, list) and res:
                return res[0]
            if isinstance(res, dict):
                return res
            return None
        except Exception:
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

    def holder_count(self, address: str) -> Optional[int]:
        # Not all chains expose a direct count; try best-effort.
        try:
            j = self._get({"module": "token", "action": "tokenholdercount", "contractaddress": address})
            res = j.get("result")
            if isinstance(res, str) and res.isdigit():
                return int(res)
            return int(str(res))
        except Exception:
            return None

    def recent_transfers_count(self, address: str, minutes: int = 1440, max_events: int = 200) -> Optional[int]:
        try:
            j = self._get(
                {
                    "module": "account",
                    "action": "tokentx",
                    "contractaddress": address,
                    "page": "1",
                    "offset": str(max_events),
                    "sort": "desc",
                }
            )
            res = j.get("result")
            if not isinstance(res, list):
                return None
            cutoff = int(time.time() - minutes * 60)
            cnt = 0
            for ev in res:
                try:
                    ts = int(ev.get("timeStamp") or ev.get("timeStamp"))
                except Exception:
                    continue
                if ts >= cutoff:
                    cnt += 1
            return cnt
        except Exception:
            return None


def _erc20_metadata_via_web3(address: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    try:
        from web3 import Web3  # lazy import
        from libs.agentkit_ext.web3_utils import get_contract, get_web3

        w3 = get_web3()
        erc20 = get_contract(w3, Web3.to_checksum_address(address), "erc20.json")
        name = str(erc20.functions.name().call())
        symbol = str(erc20.functions.symbol().call())
        decimals = int(erc20.functions.decimals().call())
        return symbol, name, decimals
    except Exception:
        return None, None, None


def _price_via_dex(address: str) -> Optional[float]:
    # Price token in QUOTE_TOKEN_ADDRESS (e.g., USDC) using the DEX aggregator.
    try:
        from services.marketdata.provider import MarketDataProvider

        md = MarketDataProvider()
        quote = os.getenv("QUOTE_TOKEN_ADDRESS")
        if not quote:
            return None
        best = md.best_price([address, quote], amount_in_decimal=1.0)
        # best['price'] is quote per token (e.g., USDC per 1 token)
        return float(best.get("price"))
    except Exception:
        return None


def collect_token_metrics(address: str, client: BaseScanClient) -> TokenMetrics:
    info = client.token_info(address) or {}
    # Parse metadata, fallback to on-chain if missing
    symbol = (info.get("symbol") if isinstance(info, dict) else None) or None
    name = (info.get("tokenName") if isinstance(info, dict) else None) or info.get("name") if isinstance(info, dict) else None
    try:
        decimals = int(info.get("divisor") or info.get("decimals")) if isinstance(info, dict) else None
    except Exception:
        decimals = None
    if not symbol or decimals is None:
        sym2, name2, dec2 = _erc20_metadata_via_web3(address)
        symbol = symbol or sym2
        name = name or name2
        decimals = decimals if decimals is not None else dec2

    # Supply and holders
    total_supply = client.total_supply(address)
    holders = client.holder_count(address)
    transfers_24h = client.recent_transfers_count(address, minutes=1440, max_events=200)

    # Price: attempt to read from tokeninfo.price.rate; else DEX quote
    price_usd: Optional[float] = None
    if isinstance(info, dict):
        try:
            price_obj = info.get("price")
            if isinstance(price_obj, dict):
                # Etherscan v2 returns {'rate': '1.23', 'diff': '...', ...}
                rate = price_obj.get("rate") or price_obj.get("usd")
                if rate is not None:
                    price_usd = float(rate)
        except Exception:
            pass
    if price_usd is None:
        price_usd = _price_via_dex(address)

    supply_circ = None
    max_total_supply = None
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
    api_key = os.getenv("ETHERSCAN_API_KEY") or os.getenv("BASESCAN_API_KEY")
    if not api_key:
        raise RuntimeError("Set BASESCAN_API_KEY or ETHERSCAN_API_KEY in environment")
    # Prefer Etherscan V2 endpoint; allow override for testing
    base_url = os.getenv("ETHERSCAN_API_URL") or os.getenv("BASESCAN_API_URL") or DEFAULT_ETHERSCAN_V2_URL
    try:
        chain_id = int(os.getenv("ETHERSCAN_CHAIN_ID") or os.getenv("BASE_CHAIN_ID") or 8453)
    except Exception:
        chain_id = 8453
    interval = int(os.getenv("TOKEN_WATCH_INTERVAL_SECONDS") or 300)

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
    print(f"[token-watcher] tracking {len(addrs)} token(s): {', '.join(addrs)}; interval={interval}s")
    while True:
        for addr in addrs:
            try:
                metrics = collect_token_metrics(addr, client)
                persist_metrics(metrics)
                print(f"[token-watcher] {metrics.symbol or ''} {addr[:6]}.. price={metrics.price_usd} holders={metrics.holders} tx24h={metrics.transfers_24h}")
            except Exception as e:  # noqa: BLE001
                print(f"[token-watcher] error for {addr}: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    run_watch_loop()
