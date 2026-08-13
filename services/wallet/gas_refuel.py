"""Gas refueling service for automated ETH acquisition when wallet balance is low.

This service enables the agent to swap other assets (USDC, USDBC, VVV, DIEM) to ETH
when gas funds are depleted, allowing continuous operation.

Usage:
    from services.wallet.gas_refuel import GasRefuelService

    refuel = GasRefuelService()
    result = refuel.check_and_refuel()

Environment variables:
    GAS_REFUEL_ENABLE: Enable gas refueling (default: 1)
    GAS_REFUEL_MIN_ETH_WEI: Minimum ETH balance to trigger refuel (default: 0.01 ETH)
    GAS_REFUEL_TARGET_ETH_WEI: Target ETH balance after refuel (default: 0.05 ETH)
    GAS_REFUEL_ASSET_PRIORITY: Comma-separated asset priority (default: USDC,USDBC,VVV,DIEM)
    GAS_REFUEL_MAX_SLIPPAGE_BPS: Max slippage for swaps (default: 100 = 1%)
    GAS_REFUEL_DRY_RUN: Simulate but don't execute (default: 0)
    WETH_ADDRESS: WETH contract address (default: Base mainnet WETH)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger("SERVICE[gas_refuel]")

# Base mainnet WETH
DEFAULT_WETH_ADDRESS = "0x4200000000000000000000000000000000000006"

# Default configuration (Base-optimized: gas is ~0.001 gwei, swap costs ~0.0000003 ETH)
DEFAULT_MIN_ETH_WEI = 100_000_000_000_000  # 0.0001 ETH (~$0.33) - enough for ~300 txs
DEFAULT_TARGET_ETH_WEI = (
    500_000_000_000_000  # 0.0005 ETH (~$1.65) - enough for ~1500 txs
)
DEFAULT_MAX_SLIPPAGE_BPS = 100  # 1%
DEFAULT_ASSET_PRIORITY = ["USDC", "USDBC", "VVV", "DIEM"]
DEFAULT_CONSOLIDATE_MIN_USDBC = 5_000_000  # $5 (6 decimals)
DEFAULT_CONSOLIDATE_SLIPPAGE_BPS = 50  # 0.5%

# Minimum amounts to attempt swap (avoid dust trades)
MIN_SWAP_AMOUNTS_WEI = {
    "USDC": 5_000_000,  # $5 minimum (6 decimals)
    "USDBC": 5_000_000,  # $5 minimum (6 decimals)
    "VVV": 5_000_000_000_000_000_000,  # 5 VVV minimum (18 decimals)
    "DIEM": 10_000_000_000_000,  # 0.00001 DIEM minimum (18 decimals)
}

# Token addresses on Base mainnet
TOKEN_ADDRESSES = {
    "USDC": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "USDBC": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA",
    "VVV": "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",
    "DIEM": "0xF4d97F2da56e8c3098f3a8D538DB630A2606a024",
}


@dataclass
class RefuelResult:
    """Result of a gas refuel operation."""

    success: bool
    action: str  # "refueled", "skipped", "failed", "dry_run"
    reason: str
    eth_balance_before_wei: int
    eth_balance_after_wei: int | None = None
    asset_used: str | None = None
    asset_amount_wei: int | None = None
    eth_received_wei: int | None = None
    tx_hash: str | None = None
    unwrap_tx_hash: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "action": self.action,
            "reason": self.reason,
            "eth_balance_before_wei": self.eth_balance_before_wei,
            "eth_balance_after_wei": self.eth_balance_after_wei,
            "asset_used": self.asset_used,
            "asset_amount_wei": self.asset_amount_wei,
            "eth_received_wei": self.eth_received_wei,
            "tx_hash": self.tx_hash,
            "unwrap_tx_hash": self.unwrap_tx_hash,
            "error": self.error,
        }


@dataclass
class GasRefuelService:
    """Service for automated gas refueling by swapping assets to ETH."""

    _web3: Any = field(default=None, repr=False)
    _wallet_address: str | None = field(default=None, repr=False)

    def __post_init__(self):
        self._enabled = os.getenv("GAS_REFUEL_ENABLE", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._min_eth_wei = int(
            os.getenv("GAS_REFUEL_MIN_ETH_WEI", str(DEFAULT_MIN_ETH_WEI))
        )
        self._target_eth_wei = int(
            os.getenv("GAS_REFUEL_TARGET_ETH_WEI", str(DEFAULT_TARGET_ETH_WEI))
        )
        self._max_slippage_bps = int(
            os.getenv("GAS_REFUEL_MAX_SLIPPAGE_BPS", str(DEFAULT_MAX_SLIPPAGE_BPS))
        )
        self._dry_run = os.getenv("GAS_REFUEL_DRY_RUN", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._weth_address = (
            os.getenv("WETH_ADDRESS")
            or os.getenv("WETH_TOKEN_ADDRESS")
            or DEFAULT_WETH_ADDRESS
        ).strip()

        # Parse asset priority
        priority_env = os.getenv("GAS_REFUEL_ASSET_PRIORITY", "")
        if priority_env.strip():
            self._asset_priority = [
                a.strip().upper() for a in priority_env.split(",") if a.strip()
            ]
        else:
            self._asset_priority = DEFAULT_ASSET_PRIORITY.copy()

        _logger.info(
            "GasRefuelService initialized: enabled=%s min_eth=%s target_eth=%s priority=%s dry_run=%s",
            self._enabled,
            self._min_eth_wei,
            self._target_eth_wei,
            self._asset_priority,
            self._dry_run,
        )

    def _get_web3(self):
        """Lazily initialize Web3 connection."""
        if self._web3 is None:
            try:
                from libs.agentkit_ext.web3_utils import get_web3

                self._web3 = get_web3()
            except Exception as e:
                _logger.warning("Failed to initialize Web3: %s", e)
                return None
        return self._web3

    def _get_wallet_address(self) -> str | None:
        """Get the wallet address."""
        if self._wallet_address is None:
            try:
                from services.wallet.provider import get_default_provider

                self._wallet_address = get_default_provider().address
            except Exception as e:
                _logger.warning("Failed to get wallet address: %s", e)
                # Fallback to env
                self._wallet_address = os.getenv("TREASURY_ADDRESS")
        return self._wallet_address

    def _get_eth_balance(self) -> int | None:
        """Get current ETH balance in wei."""
        w3 = self._get_web3()
        addr = self._get_wallet_address()
        if w3 is None or addr is None:
            return None
        try:
            return int(w3.eth.get_balance(addr))
        except Exception as e:
            _logger.warning("Failed to get ETH balance: %s", e)
            return None

    def _get_token_balance(self, symbol: str) -> int | None:
        """Get token balance in wei."""
        w3 = self._get_web3()
        addr = self._get_wallet_address()
        if w3 is None or addr is None:
            return None

        # Special case: WETH is used for checking wrapped ETH balance
        if symbol.upper() == "WETH":
            token_addr = self._weth_address
        else:
            token_addr = self._get_token_address(symbol)

        if not token_addr:
            return None

        try:
            # Use minimal ERC20 ABI
            erc20_abi = [
                {
                    "constant": True,
                    "inputs": [{"name": "_owner", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "balance", "type": "uint256"}],
                    "type": "function",
                }
            ]
            contract = w3.eth.contract(
                address=w3.to_checksum_address(token_addr), abi=erc20_abi
            )
            return int(
                contract.functions.balanceOf(w3.to_checksum_address(addr)).call()
            )
        except Exception as e:
            _logger.warning("Failed to get %s balance: %s", symbol, e)
            return None

    def _get_token_address(self, symbol: str) -> str | None:
        """Get token contract address from symbol."""
        symbol_upper = symbol.upper()
        # Check env overrides first
        env_key = f"{symbol_upper}_TOKEN_ADDRESS"
        env_addr = os.getenv(env_key, "").strip()
        if env_addr:
            return env_addr
        # Fallback to hardcoded
        return TOKEN_ADDRESSES.get(symbol_upper)

    def _calculate_swap_amount(self, symbol: str, eth_deficit_wei: int) -> int | None:
        """Calculate how much of an asset to swap to cover ETH deficit.

        Returns the amount in token wei, or None if insufficient balance.
        """
        balance = self._get_token_balance(symbol)
        if balance is None or balance == 0:
            return None

        min_amount = MIN_SWAP_AMOUNTS_WEI.get(symbol.upper(), 0)
        if balance < min_amount:
            _logger.info(
                "Skipping %s: balance %s below minimum %s", symbol, balance, min_amount
            )
            return None

        # Get approximate value of token in ETH
        # For stablecoins (USDC, USDBC), assume 1 USDC ≈ ETH/3300 (rough estimate)
        # The actual swap will use real quotes
        eth_price_usd = float(os.getenv("ETH_PRICE_USD_ESTIMATE", "3300"))

        if symbol.upper() in {"USDC", "USDBC"}:
            # 6 decimals, value in USD
            value_usd = balance / 1e6
            estimated_eth = int((value_usd / eth_price_usd) * 1e18)
        elif symbol.upper() == "VVV":
            # 18 decimals, ~$1.1 per VVV
            vvv_price = float(os.getenv("VVV_PRICE_USD_ESTIMATE", "1.1"))
            value_usd = (balance / 1e18) * vvv_price
            estimated_eth = int((value_usd / eth_price_usd) * 1e18)
        elif symbol.upper() == "DIEM":
            # 18 decimals, ~$160 per DIEM
            diem_price = float(os.getenv("DIEM_PRICE_USD_ESTIMATE", "160"))
            value_usd = (balance / 1e18) * diem_price
            estimated_eth = int((value_usd / eth_price_usd) * 1e18)
        else:
            # Unknown token, skip
            return None

        # Calculate how much of the token to swap to cover deficit + buffer
        if estimated_eth >= eth_deficit_wei:
            # We have enough, calculate exact fraction needed
            fraction = eth_deficit_wei / estimated_eth
            swap_amount = int(balance * fraction * 1.1)  # 10% buffer for slippage
            return min(swap_amount, balance)  # Don't exceed balance
        # Swap everything we have
        return balance

    def _estimate_fair_eth_value(self, symbol: str, amount_wei: int) -> float:
        """Estimate fair ETH value for a token amount based on reference prices.

        Returns estimated ETH (in wei) that the swap should return.
        Used for sanity checking quotes against catastrophic pricing.
        """
        eth_price_usd = float(os.getenv("ETH_PRICE_USD_ESTIMATE", "3300"))

        if symbol.upper() in {"USDC", "USDBC"}:
            # Stablecoins: 6 decimals, ~$1 each
            value_usd = amount_wei / 1e6
        elif symbol.upper() == "VVV":
            # VVV: 18 decimals, ~$1.1 per VVV
            vvv_price = float(os.getenv("VVV_PRICE_USD_ESTIMATE", "1.1"))
            value_usd = (amount_wei / 1e18) * vvv_price
        elif symbol.upper() == "DIEM":
            # DIEM: 18 decimals, ~$160 per DIEM
            diem_price = float(os.getenv("DIEM_PRICE_USD_ESTIMATE", "160"))
            value_usd = (amount_wei / 1e18) * diem_price
        else:
            # Unknown token - can't estimate
            return 0.0

        # Convert USD value to ETH (in wei)
        estimated_eth_wei = (value_usd / eth_price_usd) * 1e18
        return estimated_eth_wei

    def _execute_swap_to_weth(self, symbol: str, amount_wei: int) -> dict[str, Any]:
        """Execute swap from token to WETH using DEX aggregator."""
        token_addr = self._get_token_address(symbol)
        if not token_addr:
            return {"success": False, "error": f"Unknown token: {symbol}"}

        try:
            from libs.dex.composite import attach_composite_metadata
            from libs.dex.providers import build_aggregator_from_env
            from libs.dex.routes import make_route

            # Build route: TOKEN -> WETH
            route = make_route([token_addr, self._weth_address])

            # Get quote using best_quote (exact-in)
            aggregator = build_aggregator_from_env()
            quote = aggregator.best_quote(amount_wei, route)

            if symbol.upper() == "DIEM":
                usdc_addr = self._get_token_address("USDC")
                if usdc_addr:
                    multi_hop_route = make_route(
                        [token_addr, usdc_addr, self._weth_address]
                    )
                    bridge_legs = [
                        {
                            "token_in": token_addr,
                            "token_out": usdc_addr,
                            "provider": "aerodrome_cl",
                            "pool_address": (
                                os.getenv("DIEM_USDC_POOL_ADDRESS") or ""
                            ).strip()
                            or None,
                            "fee": None,
                        },
                        {
                            "token_in": usdc_addr,
                            "token_out": self._weth_address,
                            "provider": "uniswap_v2",
                            "pool_address": None,
                            "fee": None,
                        },
                    ]
                    try:
                        attach_composite_metadata(
                            multi_hop_route,
                            bridge_legs=bridge_legs,
                            is_composite=True,
                        )
                    except Exception:
                        pass
                    composite_quote = aggregator.best_quote(amount_wei, multi_hop_route)
                    if composite_quote is not None:
                        quote = composite_quote
                        route = multi_hop_route
            if quote is None and symbol.upper() == "VVV":
                usdc_addr = self._get_token_address("USDC")
                if usdc_addr:
                    multi_hop_route = make_route(
                        [token_addr, usdc_addr, self._weth_address]
                    )
                    vvv_usdc_pool = (
                        os.getenv("VVV_USDC_POOL_ADDRESS")
                        or os.getenv("VVV_USDC_POOL_V3_ADDRESS")
                        or ""
                    ).strip()
                    try:
                        vvv_usdc_fee = int(os.getenv("VVV_USDC_POOL_FEE") or "3000")
                    except Exception:
                        vvv_usdc_fee = None
                    try:
                        weth_usdc_fee = int(os.getenv("WETH_USDC_POOL_FEE") or "500")
                    except Exception:
                        weth_usdc_fee = None
                    bridge_legs = [
                        {
                            "token_in": token_addr,
                            "token_out": usdc_addr,
                            "provider": (
                                os.getenv("VVV_USDC_BRIDGE_PROVIDER") or "aerodrome_cl"
                            )
                            .strip()
                            .lower(),
                            "pool_address": vvv_usdc_pool or None,
                            "fee": vvv_usdc_fee,
                        },
                        {
                            "token_in": usdc_addr,
                            "token_out": self._weth_address,
                            "provider": "uniswap_v3",
                            "pool_address": None,
                            "fee": weth_usdc_fee,
                        },
                    ]
                    try:
                        attach_composite_metadata(
                            multi_hop_route,
                            bridge_legs=bridge_legs,
                            is_composite=True,
                        )
                    except Exception:
                        pass
                    composite_quote = aggregator.best_quote(amount_wei, multi_hop_route)
                    if composite_quote is not None:
                        quote = composite_quote
                        route = multi_hop_route

            if quote is None:
                return {"success": False, "error": "No quote available for swap"}

            # CRITICAL: Price sanity check to prevent catastrophic swaps
            # Compare quote against reference price estimate
            expected_eth_wei = self._estimate_fair_eth_value(symbol, amount_wei)
            if expected_eth_wei > 0:
                # Maximum acceptable deviation from fair value (default 50% = 5000 bps)
                max_deviation_bps = int(
                    os.getenv("GAS_REFUEL_MAX_PRICE_DEVIATION_BPS", "5000")
                )
                min_acceptable = expected_eth_wei * (10000 - max_deviation_bps) / 10000

                if quote.amount_out < min_acceptable:
                    deviation_pct = (
                        (expected_eth_wei - quote.amount_out) / expected_eth_wei
                    ) * 100
                    _logger.error(
                        "GAS REFUEL BLOCKED: Quote %.6f ETH is %.1f%% below fair value %.6f ETH for %s %s. "
                        "This swap would cause significant loss. tx_hash=BLOCKED provider=%s",
                        quote.amount_out / 1e18,
                        deviation_pct,
                        expected_eth_wei / 1e18,
                        amount_wei,
                        symbol,
                        getattr(quote, "provider", "unknown"),
                    )
                    return {
                        "success": False,
                        "error": f"Quote {quote.amount_out / 1e18:.6f} ETH is {deviation_pct:.1f}% below fair value {expected_eth_wei / 1e18:.6f} ETH - swap blocked to prevent loss",
                        "quote_eth": quote.amount_out,
                        "expected_eth": expected_eth_wei,
                        "deviation_pct": deviation_pct,
                    }

                _logger.info(
                    "Gas refuel price sanity OK: quote=%.6f ETH, fair=%.6f ETH, deviation=%.1f%%",
                    quote.amount_out / 1e18,
                    expected_eth_wei / 1e18,
                    ((expected_eth_wei - quote.amount_out) / expected_eth_wei) * 100
                    if expected_eth_wei > 0
                    else 0,
                )

            # Calculate min output with slippage
            min_out = int(quote.amount_out * (10000 - self._max_slippage_bps) / 10000)

            _logger.info(
                "Gas refuel swap: %s %s -> WETH, expected=%s min=%s provider=%s",
                amount_wei,
                symbol,
                quote.amount_out,
                min_out,
                getattr(quote, "provider", "unknown"),
            )

            if self._dry_run:
                return {
                    "success": True,
                    "dry_run": True,
                    "amount_in": amount_wei,
                    "expected_out": quote.amount_out,
                    "min_out": min_out,
                    "provider": quote.provider,
                }

            # Execute trade
            result = aggregator.trade_best(amount_wei, self._max_slippage_bps, route)

            # CRITICAL: Log tx_hash for on-chain transaction traceability
            _logger.info(
                "Gas refuel swap EXECUTED: %s %s -> WETH tx_hash=%s provider=%s",
                amount_wei,
                symbol,
                result.get("tx_hash", "unknown"),
                result.get("provider", "unknown"),
            )

            return {
                "success": True,
                "tx_hash": result.get("tx_hash"),
                "provider": result.get("provider"),
                "amount_in": amount_wei,
                "expected_out": quote.amount_out,
            }

        except Exception as e:
            _logger.error("Swap to WETH failed: %s", e)
            return {"success": False, "error": str(e)}

    def _unwrap_weth(self, amount_wei: int) -> dict[str, Any]:
        """Unwrap WETH to native ETH."""
        w3 = self._get_web3()
        addr = self._get_wallet_address()
        if w3 is None or addr is None:
            return {"success": False, "error": "Web3 or wallet not available"}

        try:
            from libs.agentkit_ext.agentkit_wallet import send_tx

            # WETH withdraw ABI
            weth_abi = [
                {
                    "constant": False,
                    "inputs": [{"name": "wad", "type": "uint256"}],
                    "name": "withdraw",
                    "outputs": [],
                    "type": "function",
                }
            ]

            weth_contract = w3.eth.contract(
                address=w3.to_checksum_address(self._weth_address), abi=weth_abi
            )

            _logger.info("Unwrapping %s WETH to ETH", amount_wei)

            if self._dry_run:
                return {
                    "success": True,
                    "dry_run": True,
                    "amount": amount_wei,
                }

            # Build and send transaction
            fn = weth_contract.functions.withdraw(amount_wei)
            built = fn.build_transaction({"from": w3.to_checksum_address(addr)})
            tx_hash = send_tx(self._weth_address, built["data"])

            return {
                "success": True,
                "tx_hash": tx_hash,
                "amount": amount_wei,
            }

        except Exception as e:
            _logger.error("WETH unwrap failed: %s", e)
            return {"success": False, "error": str(e)}

    def check_and_refuel(self) -> RefuelResult:
        """Check ETH balance and refuel if needed.

        Returns a RefuelResult indicating what action was taken.
        """
        if not self._enabled:
            return RefuelResult(
                success=True,
                action="skipped",
                reason="gas_refuel_disabled",
                eth_balance_before_wei=0,
            )

        eth_balance = self._get_eth_balance()
        if eth_balance is None:
            return RefuelResult(
                success=False,
                action="failed",
                reason="cannot_get_eth_balance",
                eth_balance_before_wei=0,
                error="Failed to get ETH balance",
            )

        _logger.info(
            "Gas refuel check: balance=%s wei (%.6f ETH), min=%s wei",
            eth_balance,
            eth_balance / 1e18,
            self._min_eth_wei,
        )

        if eth_balance >= self._min_eth_wei:
            return RefuelResult(
                success=True,
                action="skipped",
                reason="sufficient_balance",
                eth_balance_before_wei=eth_balance,
            )

        # Calculate deficit
        eth_deficit = self._target_eth_wei - eth_balance
        _logger.info(
            "Gas refuel needed: deficit=%s wei (%.6f ETH)",
            eth_deficit,
            eth_deficit / 1e18,
        )

        # Quick win: unwrap any existing WETH before attempting swaps.
        weth_balance = self._get_token_balance("WETH")
        if weth_balance and weth_balance > 0:
            _logger.info(
                "Found existing WETH balance: %s, unwrapping first", weth_balance
            )
            unwrap_result = self._unwrap_weth(weth_balance)

            if not unwrap_result.get("success"):
                _logger.warning("WETH unwrap failed: %s", unwrap_result.get("error"))
                return RefuelResult(
                    success=False,
                    action="failed",
                    reason="unwrap_failed",
                    eth_balance_before_wei=eth_balance,
                    asset_used="WETH",
                    asset_amount_wei=weth_balance,
                    error=unwrap_result.get("error"),
                )

            if self._dry_run:
                return RefuelResult(
                    success=True,
                    action="dry_run",
                    reason="would_unwrap_weth",
                    eth_balance_before_wei=eth_balance,
                    asset_used="WETH",
                    asset_amount_wei=weth_balance,
                    eth_received_wei=weth_balance,
                )

            # Wait for unwrap to confirm
            time.sleep(2)
            new_eth_balance = self._get_eth_balance()

            _logger.info(
                "Gas refuel complete: WETH -> ETH, new balance=%s wei",
                new_eth_balance,
            )

            return RefuelResult(
                success=True,
                action="refueled",
                reason="unwrapped_weth",
                eth_balance_before_wei=eth_balance,
                eth_balance_after_wei=new_eth_balance,
                asset_used="WETH",
                asset_amount_wei=weth_balance,
                eth_received_wei=weth_balance,
                unwrap_tx_hash=unwrap_result.get("tx_hash"),
            )

        # Try each asset in priority order
        for symbol in self._asset_priority:
            _logger.info("Checking %s for gas refuel...", symbol)

            swap_amount = self._calculate_swap_amount(symbol, eth_deficit)
            if swap_amount is None or swap_amount == 0:
                _logger.info(
                    "Skipping %s: insufficient balance or below minimum", symbol
                )
                continue

            _logger.info(
                "Attempting gas refuel: swap %s %s -> WETH -> ETH", swap_amount, symbol
            )

            # Execute swap to WETH
            swap_result = self._execute_swap_to_weth(symbol, swap_amount)

            if not swap_result.get("success"):
                _logger.warning(
                    "Swap %s -> WETH failed: %s", symbol, swap_result.get("error")
                )
                continue

            if self._dry_run:
                return RefuelResult(
                    success=True,
                    action="dry_run",
                    reason="would_refuel",
                    eth_balance_before_wei=eth_balance,
                    asset_used=symbol,
                    asset_amount_wei=swap_amount,
                    eth_received_wei=swap_result.get("expected_out"),
                )

            # Wait a moment for swap to confirm
            time.sleep(2)

            # Check WETH balance and unwrap
            weth_balance = self._get_token_balance("WETH")
            if weth_balance and weth_balance > 0:
                unwrap_result = self._unwrap_weth(weth_balance)

                if not unwrap_result.get("success"):
                    _logger.warning(
                        "WETH unwrap failed: %s", unwrap_result.get("error")
                    )
                    # Still report partial success
                    return RefuelResult(
                        success=False,
                        action="failed",
                        reason="unwrap_failed",
                        eth_balance_before_wei=eth_balance,
                        asset_used=symbol,
                        asset_amount_wei=swap_amount,
                        tx_hash=swap_result.get("tx_hash"),
                        error=unwrap_result.get("error"),
                    )

                # Wait for unwrap to confirm
                time.sleep(2)

                # Check new ETH balance
                new_eth_balance = self._get_eth_balance()

                _logger.info(
                    "Gas refuel complete: %s -> WETH -> ETH, new balance=%s wei",
                    symbol,
                    new_eth_balance,
                )

                return RefuelResult(
                    success=True,
                    action="refueled",
                    reason="success",
                    eth_balance_before_wei=eth_balance,
                    eth_balance_after_wei=new_eth_balance,
                    asset_used=symbol,
                    asset_amount_wei=swap_amount,
                    eth_received_wei=weth_balance,
                    tx_hash=swap_result.get("tx_hash"),
                    unwrap_tx_hash=unwrap_result.get("tx_hash"),
                )

            # WETH balance is 0, swap might have output ETH directly (some routers do this)
            new_eth_balance = self._get_eth_balance()
            if new_eth_balance and new_eth_balance > eth_balance:
                return RefuelResult(
                    success=True,
                    action="refueled",
                    reason="success_direct",
                    eth_balance_before_wei=eth_balance,
                    eth_balance_after_wei=new_eth_balance,
                    asset_used=symbol,
                    asset_amount_wei=swap_amount,
                    eth_received_wei=new_eth_balance - eth_balance,
                    tx_hash=swap_result.get("tx_hash"),
                )

        # No asset available for refueling
        _logger.error("Gas refuel failed: no assets available with sufficient balance")

        return RefuelResult(
            success=False,
            action="failed",
            reason="no_assets_available",
            eth_balance_before_wei=eth_balance,
            error="No assets available for gas refuel. Need to deposit ETH or tokens.",
        )

    def get_status(self) -> dict[str, Any]:
        """Get current gas refuel status."""
        eth_balance = self._get_eth_balance()

        asset_balances = {}
        for symbol in self._asset_priority:
            balance = self._get_token_balance(symbol)
            if balance is not None:
                asset_balances[symbol] = balance

        needs_refuel = eth_balance is not None and eth_balance < self._min_eth_wei

        return {
            "enabled": self._enabled,
            "dry_run": self._dry_run,
            "eth_balance_wei": eth_balance,
            "eth_balance_eth": eth_balance / 1e18 if eth_balance else None,
            "min_eth_wei": self._min_eth_wei,
            "target_eth_wei": self._target_eth_wei,
            "needs_refuel": needs_refuel,
            "asset_priority": self._asset_priority,
            "asset_balances": asset_balances,
            "weth_address": self._weth_address,
        }


# Convenience function for orchestrator integration
def check_and_refuel_gas() -> RefuelResult:
    """Check and refuel gas if needed. Convenience function."""
    service = GasRefuelService()
    return service.check_and_refuel()


@dataclass
class QuoteConsolidationResult:
    """Result of consolidating bridged USDbC into native USDC."""

    success: bool
    action: str  # "converted", "skipped", "failed", "dry_run"
    reason: str
    usdbc_balance_before_wei: int
    usdbc_balance_after_wei: int | None = None
    usdc_balance_after_wei: int | None = None
    usdc_received_wei: int | None = None
    tx_hash: str | None = None
    provider: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "action": self.action,
            "reason": self.reason,
            "usdbc_balance_before_wei": self.usdbc_balance_before_wei,
            "usdbc_balance_after_wei": self.usdbc_balance_after_wei,
            "usdc_balance_after_wei": self.usdc_balance_after_wei,
            "usdc_received_wei": self.usdc_received_wei,
            "tx_hash": self.tx_hash,
            "provider": self.provider,
            "error": self.error,
        }


@dataclass
class QuoteTokenConsolidator:
    """Swap bridged USDbC into the configured quote token (USDC) before trading."""

    _web3: Any = field(default=None, repr=False)
    _wallet_address: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        flag = os.getenv("QUOTE_TOKEN_CONSOLIDATE_ENABLE", "0").strip().lower()
        self._enabled = flag in {"1", "true", "yes", "on"}
        self._dry_run = os.getenv(
            "QUOTE_TOKEN_CONSOLIDATE_DRY_RUN", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._min_usdbc = int(
            os.getenv(
                "QUOTE_TOKEN_CONSOLIDATE_MIN_USDBC",
                str(DEFAULT_CONSOLIDATE_MIN_USDBC),
            )
        )
        self._slippage_bps = int(
            os.getenv(
                "QUOTE_TOKEN_CONSOLIDATE_SLIPPAGE_BPS",
                str(DEFAULT_CONSOLIDATE_SLIPPAGE_BPS),
            )
        )
        self._usdc_address = (
            os.getenv("QUOTE_TOKEN_ADDRESS")
            or os.getenv("USDC_TOKEN_ADDRESS")
            or TOKEN_ADDRESSES.get("USDC")
            or ""
        ).strip()
        self._usdbc_address = (
            os.getenv("USDBC_TOKEN_ADDRESS") or TOKEN_ADDRESSES.get("USDBC") or ""
        ).strip()

    def _get_web3(self):
        if self._web3 is None:
            try:
                from libs.agentkit_ext.web3_utils import get_web3

                self._web3 = get_web3()
            except Exception as exc:
                _logger.warning("Quote consolidation: web3 init failed: %s", exc)
                return None
        return self._web3

    def _get_wallet_address(self) -> str | None:
        if self._wallet_address is None:
            try:
                from services.wallet.provider import get_default_provider

                self._wallet_address = get_default_provider().address
            except Exception:
                self._wallet_address = os.getenv("TREASURY_ADDRESS")
        return self._wallet_address

    def _get_token_balance(self, token_addr: str) -> int | None:
        w3 = self._get_web3()
        wallet = self._get_wallet_address()
        if w3 is None or wallet is None or not token_addr:
            return None

        try:
            erc20_abi = [
                {
                    "constant": True,
                    "inputs": [{"name": "_owner", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "balance", "type": "uint256"}],
                    "type": "function",
                }
            ]
            contract = w3.eth.contract(
                address=w3.to_checksum_address(token_addr), abi=erc20_abi
            )
            return int(
                contract.functions.balanceOf(w3.to_checksum_address(wallet)).call()
            )
        except Exception as exc:
            _logger.warning("Quote consolidation: balance fetch failed: %s", exc)
            return None

    def consolidate(self) -> QuoteConsolidationResult:
        if not self._enabled:
            return QuoteConsolidationResult(
                success=True,
                action="skipped",
                reason="consolidation_disabled",
                usdbc_balance_before_wei=0,
            )

        if not self._usdc_address or not self._usdbc_address:
            return QuoteConsolidationResult(
                success=False,
                action="failed",
                reason="missing_token_address",
                usdbc_balance_before_wei=0,
                error="USDC or USDbC address missing",
            )

        if self._usdc_address.strip().lower() == self._usdbc_address.strip().lower():
            return QuoteConsolidationResult(
                success=True,
                action="skipped",
                reason="quote_is_usdbc",
                usdbc_balance_before_wei=0,
            )

        balance = self._get_token_balance(self._usdbc_address)
        if balance is None:
            return QuoteConsolidationResult(
                success=False,
                action="failed",
                reason="balance_unavailable",
                usdbc_balance_before_wei=0,
                error="Failed to fetch USDbC balance",
            )

        if balance < self._min_usdbc:
            return QuoteConsolidationResult(
                success=True,
                action="skipped",
                reason="below_threshold",
                usdbc_balance_before_wei=balance,
            )

        try:
            from libs.dex.providers import build_aggregator_from_env
            from libs.dex.routes import make_route

            route = make_route([self._usdbc_address, self._usdc_address])
            aggregator = build_aggregator_from_env()
            quote = aggregator.best_quote(balance, route)
        except Exception as exc:
            return QuoteConsolidationResult(
                success=False,
                action="failed",
                reason="quote_error",
                usdbc_balance_before_wei=balance,
                error=str(exc),
            )

        if quote is None:
            return QuoteConsolidationResult(
                success=False,
                action="failed",
                reason="no_liquidity",
                usdbc_balance_before_wei=balance,
                error="No quote available for USDbC -> USDC",
            )

        min_out = int(quote.amount_out * (10000 - self._slippage_bps) / 10000)

        _logger.info(
            "Quote consolidation: swapping %s USDbC -> USDC (min_out=%s, provider=%s)",
            balance,
            min_out,
            getattr(quote, "provider", None),
        )

        if self._dry_run:
            return QuoteConsolidationResult(
                success=True,
                action="dry_run",
                reason="dry_run",
                usdbc_balance_before_wei=balance,
                usdbc_balance_after_wei=balance,
                usdc_balance_after_wei=self._get_token_balance(self._usdc_address),
                usdc_received_wei=quote.amount_out,
                provider=getattr(quote, "provider", None),
            )

        try:
            trade_result = aggregator.trade_best(balance, self._slippage_bps, route)
        except Exception as exc:
            return QuoteConsolidationResult(
                success=False,
                action="failed",
                reason="trade_failed",
                usdbc_balance_before_wei=balance,
                error=str(exc),
            )

        usdbc_after = self._get_token_balance(self._usdbc_address)
        usdc_after = self._get_token_balance(self._usdc_address)

        return QuoteConsolidationResult(
            success=True,
            action="converted",
            reason="success",
            usdbc_balance_before_wei=balance,
            usdbc_balance_after_wei=usdbc_after,
            usdc_balance_after_wei=usdc_after,
            usdc_received_wei=getattr(quote, "amount_out", None),
            tx_hash=(trade_result or {}).get("tx_hash"),
            provider=getattr(quote, "provider", None),
        )


def consolidate_quote_token() -> QuoteConsolidationResult:
    """Convenience wrapper to consolidate USDbC into USDC."""

    return QuoteTokenConsolidator().consolidate()
