from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - optional dependency
    from web3 import Web3  # type: ignore
except Exception:  # noqa: BLE001 - fallback when web3 is absent
    class Web3:  # type: ignore
        """Minimal checksum helper when web3 is unavailable."""

        @staticmethod
        def to_checksum_address(address: str) -> str:
            address = address.strip()
            if not address.startswith("0x") or len(address) != 42:
                return address
            prefix, hex_part = address[:2], address[2:]
            return prefix + hex_part.upper()


_LOG_PATH = Path(os.getenv("AGENTKIT_TEST_LOG", ".agentkit-test.log"))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def log_to_file(message: str, level: str = "INFO") -> None:
    """Persist a structured log line for local debugging."""

    level = (level or "INFO").upper()
    line = f"{_now()} [{level}] {message}\n"
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # pragma: no cover - logging must never raise in tests
        pass


class BaseActionProvider:
    """Simplified stand-in for Coinbase AgentKit action providers."""

    def __init__(self, provider_type: str) -> None:
        self.provider_type = provider_type

    def info(self) -> Dict[str, Any]:
        return {"provider_type": self.provider_type}


class WethActionProvider(BaseActionProvider):
    """Expose the configured WETH contract address for tests."""

    def __init__(self) -> None:
        super().__init__("weth")
        self.address = Web3.to_checksum_address(
            os.getenv("WETH_ADDRESS", "0x4200000000000000000000000000000000000006")
        )

    def resolve_token_address(self, symbol: str = "weth") -> str:
        return self.address


class PythActionProvider(BaseActionProvider):
    """Return deterministic feed identifiers for Pyth price lookups."""

    def __init__(self) -> None:
        super().__init__("pyth")
        self.default_feed_id = os.getenv("PYTH_BASE_FEED_ID", "0x" + "0" * 64)

    def get_price_feed_id(self, asset: str = "diem") -> str:
        return self.default_feed_id


def _default_token_registry() -> Dict[str, str]:
    registry = {
        "weth": os.getenv("WETH_ADDRESS", "0x4200000000000000000000000000000000000006"),
        "usdc": os.getenv("USDC_ADDRESS", "0xA0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"),
        "diem": os.getenv("DIEM_TOKEN_ADDRESS", "0xF4d97F2da56e8C3098F3a8D538dB630a2606a024"),
        "vvv": os.getenv("VVV_TOKEN_ADDRESS", "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"),
    }

    overrides = os.getenv("TOKEN_ADDRESS_OVERRIDES")
    if overrides:
        try:
            parsed = json.loads(overrides)
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    if isinstance(key, str) and isinstance(value, str):
                        registry[key.lower()] = value
        except Exception:
            log_to_file("Failed to parse TOKEN_ADDRESS_OVERRIDES", level="WARNING")

    for env_key, env_value in os.environ.items():
        if env_key.startswith("TOKEN_ADDRESS_") and env_value:
            registry[env_key[len("TOKEN_ADDRESS_") :].lower()] = env_value

    return registry


class CustomUniswapActionProvider(BaseActionProvider):
    """Token resolver compatible with Coinbase AgentKit's Uniswap provider."""

    def __init__(self, token_registry: Optional[Dict[str, str]] = None) -> None:
        super().__init__("uniswap")
        raw = token_registry or _default_token_registry()
        normalised: Dict[str, str] = {}
        for symbol, address in raw.items():
            if not isinstance(symbol, str) or not isinstance(address, str):
                continue
            key = symbol.lower().strip()
            if not key:
                continue
            normalised[key] = Web3.to_checksum_address(address)
        if "diem" in normalised:
            normalised.setdefault("diem_token", normalised["diem"])
        if "vvv" in normalised:
            normalised.setdefault("svvv", normalised["vvv"])
        self._registry = normalised

    def list_supported_tokens(self) -> List[str]:
        return sorted(self._registry.keys())

    def register_token(self, symbol: str, address: str) -> None:
        if not symbol or not address:
            raise ValueError("symbol and address must be provided")
        self._registry[symbol.lower()] = Web3.to_checksum_address(address)

    def resolve_token_address(self, symbol: str) -> str:
        if not symbol:
            raise ValueError("Token symbol must be provided")
        key = symbol.lower().strip()
        if key not in self._registry:
            raise ValueError(f"Unknown token symbol: {symbol}")
        return self._registry[key]


class AgentKitStub:
    """Transparent container exposing AgentKit-compatible provider listing."""

    def __init__(self, action_providers: Optional[List[BaseActionProvider]] = None) -> None:
        self.action_providers: List[BaseActionProvider] = action_providers or []

    def list_provider_types(self) -> List[str]:
        return [provider.provider_type for provider in self.action_providers]


_agentkit_instance: Optional[AgentKitStub] = None
_providers: Dict[str, BaseActionProvider] = {}


def reset_agentkit_for_tests() -> None:
    """Clear cached AgentKit state (useful for isolated unit tests)."""

    global _agentkit_instance, _providers
    _agentkit_instance = None
    _providers = {}
    log_to_file("AgentKit test cache reset", level="DEBUG")


def _build_default_providers() -> List[BaseActionProvider]:
    return [
        WethActionProvider(),
        PythActionProvider(),
        CustomUniswapActionProvider(),
    ]


def get_test_agentkit_instance() -> AgentKitStub:
    """Return a lazily constructed AgentKit stub for deterministic tests."""

    global _agentkit_instance
    if _agentkit_instance is None:
        providers = _build_default_providers()
        _agentkit_instance = AgentKitStub(action_providers=providers)
        log_to_file(
            "Initialised AgentKit stub with providers: "
            + ", ".join(p.provider_type for p in providers)
        )
    return _agentkit_instance


def _normalise_provider_type(provider_type: str) -> str:
    if not provider_type:
        raise ValueError("provider_type must be provided")
    return provider_type.lower().strip()


_ALIAS_MAP: Dict[str, str] = {
    "uniswap_v2": "uniswap",
    "uniswapv2": "uniswap",
}


def get_test_action_provider(provider_type: str) -> BaseActionProvider:
    """Return a cached test action provider ("weth", "pyth", "uniswap")."""

    global _providers, _agentkit_instance

    normalised = _ALIAS_MAP.get(_normalise_provider_type(provider_type), _normalise_provider_type(provider_type))

    if normalised in _providers:
        return _providers[normalised]

    if _agentkit_instance is None:
        log_to_file("AgentKit not initialized, creating instance first", level="INFO")
        get_test_agentkit_instance()

    assert _agentkit_instance is not None

    for provider in _agentkit_instance.action_providers:
        key = provider.provider_type.lower()
        _providers.setdefault(key, provider)

    if normalised not in _providers:
        raise ValueError(f"Unsupported provider type: {provider_type}")

    return _providers[normalised]


__all__ = [
    "AgentKitStub",
    "CustomUniswapActionProvider",
    "PythActionProvider",
    "WethActionProvider",
    "get_test_action_provider",
    "get_test_agentkit_instance",
    "log_to_file",
    "reset_agentkit_for_tests",
]
