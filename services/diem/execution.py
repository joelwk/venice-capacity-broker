"""Trading execution models and configuration validation for DIEM/VVV execution."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from libs.dex.routes import RoutePlan


class TradeSide(str, Enum):
    """Trade direction."""

    BUY = "buy"
    SELL = "sell"


class ExecutionStatus(str, Enum):
    """Execution result status."""

    SIMULATED = "simulated"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass
class ExecutionIntent:
    """Execution intent for a trade.

    Captures the desired trade parameters including side, asset pair, size,
    slippage limits, and optional preferred route.
    """

    side: TradeSide
    token_in: str  # Token address or symbol (e.g., "DIEM", "USDC")
    token_out: str  # Token address or symbol
    amount_base_units: int  # Amount in base units (wei/smallest unit)
    slippage_bps: int = 50  # Max slippage in basis points (default 0.5%)
    pool_take_bps: int | None = None  # Max pool take in bps (defaults to risk policy)
    preferred_route: RoutePlan | None = None  # Optional preferred route
    min_received: int | None = None  # Minimum amount out (for exact-out)
    max_amount_in: int | None = None  # Maximum amount in (for exact-out)
    metadata: dict[str, Any] = field(default_factory=dict)  # Additional context

    def __post_init__(self) -> None:
        """Validate intent after initialization."""
        if self.amount_base_units <= 0:
            raise ValueError("amount_base_units must be positive")
        if self.slippage_bps < 0 or self.slippage_bps > 10_000:
            raise ValueError("slippage_bps must be between 0 and 10000")
        if self.pool_take_bps is not None and (
            self.pool_take_bps < 0 or self.pool_take_bps > 10_000
        ):
            raise ValueError("pool_take_bps must be between 0 and 10000")


@dataclass
class ExecutionResult:
    """Result of a trade execution attempt.

    Contains transaction details, effective price, slippage, and status.
    """

    status: ExecutionStatus
    intent: ExecutionIntent
    tx_hash: str | None = None  # On-chain transaction hash
    effective_price: float | None = None  # Effective execution price
    slippage_bps: float | None = None  # Actual slippage in bps
    pool_take_bps: float | None = None  # Actual pool take in bps
    gas_used: int | None = None  # Gas used (if available)
    gas_price: int | None = None  # Gas price in wei
    amount_in: int | None = None  # Actual amount in (may differ from intent)
    amount_out: int | None = None  # Actual amount out
    route_used: RoutePlan | None = None  # Route actually used
    error: str | None = None  # Error message if failed
    diagnostics: dict[str, Any] = field(default_factory=dict)  # Additional diagnostics

    def as_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        result: dict[str, Any] = {
            "status": self.status.value,
            "side": self.intent.side.value,
            "token_in": self.intent.token_in,
            "token_out": self.intent.token_out,
            "amount_base_units": self.intent.amount_base_units,
        }
        if self.tx_hash:
            result["tx_hash"] = self.tx_hash
        if self.effective_price is not None:
            result["effective_price"] = self.effective_price
        if self.slippage_bps is not None:
            result["slippage_bps"] = self.slippage_bps
        if self.pool_take_bps is not None:
            result["pool_take_bps"] = self.pool_take_bps
        if self.gas_used is not None:
            result["gas_used"] = self.gas_used
        if self.gas_price is not None:
            result["gas_price"] = self.gas_price
        if self.amount_in is not None:
            result["amount_in"] = self.amount_in
        if self.amount_out is not None:
            result["amount_out"] = self.amount_out
        if self.route_used:
            result["route_tokens"] = list(self.route_used.tokens)
        if self.error:
            result["error"] = self.error
        if self.diagnostics:
            result["diagnostics"] = self.diagnostics
        return result


class ExecutionConfigError(Exception):
    """Raised when execution configuration is invalid."""


def validate_execution_env() -> dict[str, Any]:
    """Validate that all required environment variables for execution are present.

    Returns a dict with validation results:
    - valid: bool indicating if all required vars are present
    - missing: list of missing required variable names
    - warnings: list of warnings for optional but recommended vars
    - config: dict of validated config values

    Raises ExecutionConfigError if critical configuration is missing.
    """
    required = {
        "BASE_RPC_URL": os.getenv("BASE_RPC_URL"),
        "BASE_CHAIN_ID": os.getenv("BASE_CHAIN_ID"),
        "DIEM_TOKEN_ADDRESS": os.getenv("DIEM_TOKEN_ADDRESS"),
        "VVV_TOKEN_ADDRESS": os.getenv("VVV_TOKEN_ADDRESS"),
    }

    optional_but_recommended = {
        "DEX_PROVIDERS": os.getenv("DEX_PROVIDERS"),
        "UNISWAP_V2_ROUTER_ADDRESS": os.getenv("UNISWAP_V2_ROUTER_ADDRESS"),
        "RISK_MAX_SLIPPAGE_BPS": os.getenv("RISK_MAX_SLIPPAGE_BPS"),
        "RISK_MAX_POOL_TAKE_BPS": os.getenv("RISK_MAX_POOL_TAKE_BPS"),
        "DIEM_PREMIUM_THRESHOLD": os.getenv("DIEM_PREMIUM_THRESHOLD"),
        "DIEM_DISCOUNT_THRESHOLD": os.getenv("DIEM_DISCOUNT_THRESHOLD"),
    }

    missing = [name for name, value in required.items() if not value]
    warnings = [name for name, value in optional_but_recommended.items() if not value]

    config: dict[str, Any] = {}
    for name, value in required.items():
        if value:
            config[name] = value
    for name, value in optional_but_recommended.items():
        if value:
            config[name] = value

    # Validate BASE_CHAIN_ID is numeric if present
    chain_id = config.get("BASE_CHAIN_ID")
    if chain_id:
        try:
            int(chain_id)
        except (ValueError, TypeError):
            missing.append("BASE_CHAIN_ID")  # Treat as missing if invalid

    result = {
        "valid": len(missing) == 0,
        "missing": missing,
        "warnings": warnings,
        "config": config,
    }

    if missing:
        raise ExecutionConfigError(
            f"Missing required execution configuration: {', '.join(missing)}"
        )

    return result
