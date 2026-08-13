from __future__ import annotations

import json
import os
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from libs.env import bootstrap_env

DEFAULT_DEX_PROVIDERS = "uniswap_v2,aerodrome,uniswap_v3"
DEFAULT_UNISWAP_V2_ROUTER = "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24"
DEFAULT_UNISWAP_V3_ROUTER = "0x2626664c2603336e57b271c5c0b26f421741e481"
DEFAULT_UNISWAP_V3_QUOTER = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
DEFAULT_AERODROME_ROUTER = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"


class ConfigError(EnvironmentError):
    """Raised when required configuration is missing or invalid."""


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_int(value: str | None, default: int | None = None) -> int | None:
    if value is None:
        return default
    raw = str(value).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _parse_provider_spec(raw: str) -> list[dict[str, Any]]:
    if not raw:
        return []
    trimmed = raw.strip()
    if trimmed.startswith("["):
        parsed = json.loads(trimmed)
        if isinstance(parsed, list):
            result: list[dict[str, Any]] = []
            for entry in parsed:
                if isinstance(entry, dict) and entry.get("name"):
                    entry["name"] = str(entry["name"]).strip().lower()
                    result.append(entry)
            return result
    return [
        {"name": part.strip().lower()} for part in trimmed.split(",") if part.strip()
    ]


@dataclass
class DexSettings:
    providers: list[dict[str, Any]]
    discovery: list[str]
    execution: list[str]
    uniswap_v2_router: str
    uniswap_v3_router: str
    uniswap_v3_quoter: str
    aerodrome_router: str
    aerodrome_cl_router: str
    aerodrome_stable: bool
    diem_debug_routes: bool
    diem_usdc_pool_address: str
    diem_usdc_tick_spacing: int | None


@dataclass
class TradeSettings:
    sell_path: str
    buy_path: str | None


@dataclass
class TokenSettings:
    diem: str
    vvv: str
    quote: str
    weth: str | None


@dataclass
class DebugSettings:
    diem_routes: bool
    marketdata_sanity: bool


@dataclass
class AppConfig:
    dex: DexSettings
    trade: TradeSettings
    tokens: TokenSettings
    debug: DebugSettings

    def require(self, groups: Sequence[str] | None = None) -> None:
        groups = list(groups) if groups else ["tokens", "trade", "dex"]
        missing: list[str] = []
        dex_missing: list[str] = []

        if "tokens" in groups:
            for key, val in (
                ("DIEM_TOKEN_ADDRESS", self.tokens.diem),
                ("VVV_TOKEN_ADDRESS", self.tokens.vvv),
                ("QUOTE_TOKEN_ADDRESS", self.tokens.quote),
            ):
                if not val:
                    missing.append(key)

        if "trade" in groups and not self.trade.sell_path:
            missing.append("TRADE_PATH")

        if "dex" in groups:
            if not self.dex.providers:
                dex_missing.append("DEX_PROVIDERS")
            for spec in self.dex.providers:
                name = str(spec.get("name", "")).strip().lower()
                if name == "uniswap_v2" and not self.dex.uniswap_v2_router:
                    dex_missing.append("UNISWAP_V2_ROUTER_ADDRESS")
                if name == "uniswap_v3":
                    if not self.dex.uniswap_v3_router:
                        dex_missing.append("UNISWAP_V3_ROUTER_ADDRESS")
                    if not self.dex.uniswap_v3_quoter:
                        dex_missing.append("UNISWAP_V3_QUOTER_ADDRESS")
                if name == "aerodrome" and not self.dex.aerodrome_router:
                    dex_missing.append("AERODROME_ROUTER_ADDRESS")
                if name == "aerodrome_cl":
                    if not self.dex.aerodrome_cl_router:
                        dex_missing.append("AERODROME_CL_ROUTER_ADDRESS")
                    if not self.dex.diem_usdc_pool_address:
                        dex_missing.append("DIEM_USDC_POOL_ADDRESS")
                    if not self.dex.diem_usdc_tick_spacing or (
                        int(self.dex.diem_usdc_tick_spacing) <= 0
                    ):
                        dex_missing.append("DIEM_USDC_TICK_SPACING")

        if missing or dex_missing:
            parts: list[str] = []
            if missing:
                parts.append(
                    "Missing required env vars: " + ", ".join(sorted(set(missing)))
                )
            if dex_missing:
                parts.append(
                    "DEX configuration incomplete: "
                    + ", ".join(sorted(set(dex_missing)))
                )
            raise ConfigError("; ".join(parts))


_CONFIG: AppConfig | None = None


def _apply_provider_defaults(
    providers: list[dict[str, Any]],
    dex_defaults: dict[str, str],
    aerodrome_stable: bool,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for spec in providers:
        name = str(spec.get("name", "")).strip().lower()
        updated = dict(spec)
        if name == "uniswap_v2":
            updated.setdefault("router", dex_defaults.get("uniswap_v2_router"))
        if name == "uniswap_v3":
            updated.setdefault("router", dex_defaults.get("uniswap_v3_router"))
            updated.setdefault("quoter", dex_defaults.get("uniswap_v3_quoter"))
        if name == "aerodrome":
            updated.setdefault("router", dex_defaults.get("aerodrome_router"))
            if "stable" not in updated:
                updated["stable"] = aerodrome_stable
        if name == "aerodrome_cl":
            updated.setdefault("router", dex_defaults.get("aerodrome_cl_router"))
            if "pool" not in updated and "pool_address" not in updated:
                updated["pool"] = (os.getenv("DIEM_USDC_POOL_ADDRESS") or "").strip()
            if "tick_spacing" not in updated and "tickSpacing" not in updated:
                updated["tick_spacing"] = (
                    os.getenv("DIEM_USDC_TICK_SPACING") or ""
                ).strip()
        merged.append(updated)
    return merged


def load_config() -> AppConfig:
    with suppress(Exception):
        bootstrap_env()

    dex_defaults = {
        "uniswap_v2_router": (
            os.getenv("UNISWAP_V2_ROUTER_ADDRESS")
            or os.getenv("ROUTER_ADDRESS")
            or DEFAULT_UNISWAP_V2_ROUTER
        ),
        "uniswap_v3_router": os.getenv(
            "UNISWAP_V3_ROUTER_ADDRESS", DEFAULT_UNISWAP_V3_ROUTER
        ),
        "uniswap_v3_quoter": os.getenv(
            "UNISWAP_V3_QUOTER_ADDRESS", DEFAULT_UNISWAP_V3_QUOTER
        ),
        "aerodrome_router": os.getenv(
            "AERODROME_ROUTER_ADDRESS", DEFAULT_AERODROME_ROUTER
        ),
        "aerodrome_cl_router": (os.getenv("AERODROME_CL_ROUTER_ADDRESS") or "").strip(),
    }

    raw_providers = os.getenv("DEX_PROVIDERS", DEFAULT_DEX_PROVIDERS)
    providers = _parse_provider_spec(raw_providers)
    aerodrome_stable = _as_bool(os.getenv("AERODROME_STABLE"), default=False)
    providers = _apply_provider_defaults(providers, dex_defaults, aerodrome_stable)
    # Promote Aerodrome CL provider to first-class config when router is set.
    cl_router = dex_defaults.get("aerodrome_cl_router") or ""
    if cl_router:
        has_cl = any(
            str(spec.get("name", "")).strip().lower() == "aerodrome_cl"
            for spec in providers
        )
        if not has_cl:
            cl_spec: dict[str, Any] = {"name": "aerodrome_cl", "router": cl_router}
            cl_pool = (os.getenv("DIEM_USDC_POOL_ADDRESS") or "").strip()
            if cl_pool:
                cl_spec["pool"] = cl_pool
            tick_spacing_raw = (os.getenv("DIEM_USDC_TICK_SPACING") or "").strip()
            if tick_spacing_raw:
                cl_spec["tick_spacing"] = tick_spacing_raw
            providers.append(cl_spec)

    discovery = _parse_csv(os.getenv("DEX_DISCOVERY_PROVIDERS"))
    execution = _parse_csv(os.getenv("DEX_EXEC_PROVIDERS"))
    if not discovery:
        discovery = [p.get("name", "") for p in providers if p.get("name")]
    if not execution:
        execution = list(discovery)

    diem_usdc_tick_spacing_raw = os.getenv("DIEM_USDC_TICK_SPACING")
    if (
        diem_usdc_tick_spacing_raw is None
        or not str(diem_usdc_tick_spacing_raw).strip()
    ):
        diem_usdc_tick_spacing = 100
    else:
        diem_usdc_tick_spacing = _parse_int(diem_usdc_tick_spacing_raw, default=None)

    dex = DexSettings(
        providers=providers,
        discovery=discovery,
        execution=execution,
        uniswap_v2_router=dex_defaults["uniswap_v2_router"],
        uniswap_v3_router=dex_defaults["uniswap_v3_router"],
        uniswap_v3_quoter=dex_defaults["uniswap_v3_quoter"],
        aerodrome_router=dex_defaults["aerodrome_router"],
        aerodrome_cl_router=dex_defaults["aerodrome_cl_router"],
        aerodrome_stable=aerodrome_stable,
        diem_debug_routes=_as_bool(os.getenv("DIEM_DEBUG_ROUTES"), default=False),
        diem_usdc_pool_address=(os.getenv("DIEM_USDC_POOL_ADDRESS") or "").strip(),
        diem_usdc_tick_spacing=diem_usdc_tick_spacing,
    )

    trade = TradeSettings(
        sell_path=(os.getenv("TRADE_PATH") or "").strip(),
        buy_path=(os.getenv("TRADE_PATH_BUY") or "").strip() or None,
    )

    tokens = TokenSettings(
        diem=(os.getenv("DIEM_TOKEN_ADDRESS") or "").strip(),
        vvv=(os.getenv("VVV_TOKEN_ADDRESS") or "").strip(),
        quote=(os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip(),
        weth=(os.getenv("WETH_ADDRESS") or "").strip() or None,
    )

    debug = DebugSettings(
        diem_routes=dex.diem_debug_routes,
        marketdata_sanity=_as_bool(os.getenv("MARKETDATA_DEBUG_SANITY"), default=False),
    )

    cfg = AppConfig(dex=dex, trade=trade, tokens=tokens, debug=debug)
    # During tests we allow missing TRADE_PATH so behaviour can be asserted at call time.
    if os.getenv("PYTEST_CURRENT_TEST"):
        cfg.require(groups=("tokens", "dex"))
    else:
        cfg.require()
    return cfg


def get_config(reload: bool = False) -> AppConfig:
    global _CONFIG
    if reload or _CONFIG is None or os.getenv("PYTEST_CURRENT_TEST"):
        _CONFIG = load_config()
    return _CONFIG
