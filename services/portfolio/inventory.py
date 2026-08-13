"""Portfolio inventory service for fetching balances and computing USD valuations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from libs.telemetry.logger import get_logger

try:
    from services.marketdata.provider import MarketDataProvider
except ImportError:
    MarketDataProvider = None  # type: ignore

try:
    from services.wallet.provider import describe_treasury_portfolio
except ImportError:
    describe_treasury_portfolio = None  # type: ignore

logger = get_logger("portfolio.inventory")


@dataclass
class PortfolioSnapshot:
    """Snapshot of portfolio balances and USD valuations."""

    address: str | None = None
    balances: dict[str, Any] = None  # type: ignore
    inventory_usd: float = 0.0
    per_asset_usd: dict[str, float] = None  # type: ignore
    errors: list[str] = None  # type: ignore

    def __post_init__(self) -> None:
        if self.balances is None:
            self.balances = {}
        if self.per_asset_usd is None:
            self.per_asset_usd = {}
        if self.errors is None:
            self.errors = []


class PortfolioInventory:
    """Fetches wallet balances and computes USD valuations."""

    def __init__(
        self,
        *,
        marketdata_provider: Any | None = None,
        wallet_address: str | None = None,
    ) -> None:
        self._marketdata = marketdata_provider
        self._wallet_address = wallet_address

    def snapshot(self, *, include_eth: bool = True) -> PortfolioSnapshot:
        """
        Fetch current portfolio balances and compute USD valuations.

        Returns:
            PortfolioSnapshot with balances, USD values, and any errors.
        """
        if describe_treasury_portfolio is None:
            logger.error("wallet provider unavailable")
            return PortfolioSnapshot(
                errors=["wallet provider unavailable"],
            )

        try:
            treasury_snapshot = describe_treasury_portfolio(
                wallet_address=self._wallet_address,
                include_eth=include_eth,
            )
        except Exception as exc:
            logger.exception("failed to fetch treasury portfolio")
            return PortfolioSnapshot(
                errors=[f"fetch failed: {exc}"],
            )

        address = treasury_snapshot.get("address")
        balances_raw = treasury_snapshot.get("balances", {})
        errors = list(treasury_snapshot.get("errors", []))

        if not address:
            errors.append("wallet address unavailable")
            return PortfolioSnapshot(
                errors=errors,
            )

        per_asset_usd: dict[str, float] = {}
        total_usd = 0.0

        if MarketDataProvider is None:
            logger.warning("marketdata provider unavailable; USD valuations skipped")
            return PortfolioSnapshot(
                address=address,
                balances=balances_raw,
                inventory_usd=0.0,
                per_asset_usd={},
                errors=errors + ["marketdata provider unavailable"],
            )

        try:
            provider = self._marketdata or MarketDataProvider()
            prices = provider.prices(["ETH", "USDC", "VVV", "DIEM"]) or {}
        except Exception as exc:
            logger.exception("failed to fetch prices")
            errors.append(f"price fetch failed: {exc}")
            return PortfolioSnapshot(
                address=address,
                balances=balances_raw,
                inventory_usd=0.0,
                per_asset_usd={},
                errors=errors,
            )

        for symbol, balance_info in balances_raw.items():
            if not isinstance(balance_info, dict):
                continue

            units_raw = balance_info.get("units")
            decimals = balance_info.get("decimals", 18)

            if units_raw is None:
                continue

            try:
                units = int(units_raw)
            except (TypeError, ValueError):
                continue

            if units <= 0:
                continue

            normalized_units = float(units) / (10.0**decimals)

            if symbol == "ETH":
                price_usd = float(prices.get("ETH") or prices.get("WETH") or 0.0)
            elif symbol == "USDC":
                price_usd = 1.0
            else:
                price_usd = float(prices.get(symbol) or 0.0)

            if price_usd <= 0:
                logger.debug(f"no price available for {symbol}")
                continue

            asset_usd = normalized_units * price_usd
            per_asset_usd[symbol] = asset_usd
            total_usd += asset_usd

        return PortfolioSnapshot(
            address=address,
            balances=balances_raw,
            inventory_usd=total_usd,
            per_asset_usd=per_asset_usd,
            errors=errors,
        )

    def get_usdc_balance(self, snapshot: PortfolioSnapshot | None = None) -> float:
        """Get USDC balance in USD (1:1)."""
        if snapshot is None:
            snapshot = self.snapshot(include_eth=False)
        return snapshot.per_asset_usd.get("USDC", 0.0)

    def get_vvv_balance(self, snapshot: PortfolioSnapshot | None = None) -> float:
        """Get VVV balance in USD."""
        if snapshot is None:
            snapshot = self.snapshot(include_eth=False)
        return snapshot.per_asset_usd.get("VVV", 0.0)

    def get_diem_balance(self, snapshot: PortfolioSnapshot | None = None) -> float:
        """Get DIEM balance in USD."""
        if snapshot is None:
            snapshot = self.snapshot(include_eth=False)
        return snapshot.per_asset_usd.get("DIEM", 0.0)

    def get_eth_balance(self, snapshot: PortfolioSnapshot | None = None) -> float:
        """Get ETH balance in USD."""
        if snapshot is None:
            snapshot = self.snapshot(include_eth=True)
        return snapshot.per_asset_usd.get("ETH", 0.0)


__all__ = ["PortfolioInventory", "PortfolioSnapshot"]
