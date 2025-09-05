from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass
class RiskPolicy:
    """Simple, ENV-driven risk policy for DIEM exposure and trade sizing.

    Env vars (optional):
      - RISK_MAX_DIEM_TRADE_USD: max notional per trade in USD (default 10000)
      - RISK_MAX_DIEM_INVENTORY_USD: max inventory cap in USD (default 100000)
      - RISK_MAX_DIEM_TRADE_UNITS: absolute max units per trade (overrides USD)
      - DIEM_DECIMALS: override token decimals to avoid on-chain reads (default unset)
    """

    max_trade_usd: float = 10_000.0
    max_inventory_usd: float = 100_000.0
    max_trade_units: int = 0
    slippage_bps_cap: int = 150  # 1.5% default cap
    _diem_decimals_cache: Optional[int] = None

    @classmethod
    def from_env(cls) -> "RiskPolicy":
        def _f(name: str, default: float) -> float:
            v = os.getenv(name)
            if not v:
                return default
            try:
                return float(v)
            except Exception:
                return default

        def _i(name: str, default: int) -> int:
            v = os.getenv(name)
            if not v:
                return default
            try:
                return int(v)
            except Exception:
                return default

        return cls(
            max_trade_usd=_f("RISK_MAX_DIEM_TRADE_USD", 10_000.0),
            max_inventory_usd=_f("RISK_MAX_DIEM_INVENTORY_USD", 100_000.0),
            max_trade_units=_i("RISK_MAX_DIEM_TRADE_UNITS", 0),
            slippage_bps_cap=_i("RISK_MAX_SLIPPAGE_BPS", 150),
        )

    # --- helpers ---
    def _diem_decimals(self) -> int:
        if self._diem_decimals_cache is not None:
            return self._diem_decimals_cache
        # allow explicit override to avoid web3 dependency for reads
        env_dec = os.getenv("DIEM_DECIMALS")
        if env_dec:
            try:
                self._diem_decimals_cache = int(env_dec)
                return self._diem_decimals_cache
            except Exception:
                pass
        try:
            from libs.agentkit_ext.web3_utils import get_contract, get_web3
            from web3 import Web3  # type: ignore

            addr = os.getenv("DIEM_TOKEN_ADDRESS")
            if not addr:
                raise EnvironmentError("DIEM_TOKEN_ADDRESS must be set or DIEM_DECIMALS provided")
            w3 = get_web3()
            erc20 = get_contract(w3, Web3.to_checksum_address(addr), "erc20.json")
            self._diem_decimals_cache = int(erc20.functions.decimals().call())
        except Exception:
            # Default to 18 if we cannot fetch
            self._diem_decimals_cache = 18
        return self._diem_decimals_cache

    # --- conversions ---
    def units_from_usd(self, usd: float, price_usd_per_diem: float) -> int:
        if price_usd_per_diem <= 0:
            return 0
        decimals = self._diem_decimals()
        tokens = usd / price_usd_per_diem
        return int(tokens * (10 ** decimals))

    def usd_from_units(self, units: int, price_usd_per_diem: float) -> float:
        decimals = self._diem_decimals()
        tokens = units / float(10 ** decimals)
        v = tokens * price_usd_per_diem
        # Guard against tiny float overshoot above budget thresholds
        return max(0.0, v - 1e-9)

    # --- policy decisions ---
    def max_allowed_units(self, price_usd_per_diem: float, current_inventory_usd: Optional[float] = None) -> int:
        # absolute units limit takes precedence if set
        if self.max_trade_units and self.max_trade_units > 0:
            limit_units = int(self.max_trade_units)
        else:
            # USD-based limit
            # also respect remaining inventory budget if provided
            usd_cap = self.max_trade_usd
            if current_inventory_usd is not None:
                remaining = max(self.max_inventory_usd - current_inventory_usd, 0.0)
                usd_cap = min(usd_cap, remaining)
            limit_units = self.units_from_usd(usd_cap, price_usd_per_diem)
        return max(limit_units, 0)

    def suggest_trade_units(
        self,
        desired_units: int,
        price_usd_per_diem: float,
        current_inventory_usd: Optional[float] = None,
    ) -> int:
        limit = self.max_allowed_units(price_usd_per_diem, current_inventory_usd)
        return min(desired_units, limit)

    def allow_trade_units(
        self,
        desired_units: int,
        price_usd_per_diem: float,
        current_inventory_usd: Optional[float] = None,
    ) -> Dict[str, object]:
        suggested = self.suggest_trade_units(desired_units, price_usd_per_diem, current_inventory_usd)
        allowed = suggested > 0
        return {
            "ok": allowed,
            "desired_units": int(desired_units),
            "suggested_units": int(suggested),
            "price": float(price_usd_per_diem),
            "reason": None if allowed else "risk_limit",
        }

    # --- portfolio exposure ---
    def exposure_usd(
        self,
        diem_units: int = 0,
        vvv_units: int = 0,
        usdc_units: int = 0,
        prices_usd: Optional[Dict[str, float]] = None,
        vvv_decimals: int = 18,
        usdc_decimals: int = 6,
    ) -> Tuple[float, Dict[str, float]]:
        """Compute portfolio exposure in USD given units and prices.

        prices_usd: map like {"DIEM": px, "VVV": px, "USDC": 1.0}. Missing symbols default to 0 or 1 for USDC.
        Returns (total_usd, breakdown)
        """
        p = prices_usd or {}
        diem_px = float(p.get("DIEM", 0.0) or 0.0)
        vvv_px = float(p.get("VVV", 0.0) or 0.0)
        usdc_px = float(p.get("USDC", 1.0) or 1.0)
        d_dec = float(10 ** self._diem_decimals())
        v_dec = float(10 ** int(vvv_decimals))
        u_dec = float(10 ** int(usdc_decimals))
        diem_usd = (float(diem_units) / d_dec) * diem_px if diem_px > 0 else 0.0
        vvv_usd = (float(vvv_units) / v_dec) * vvv_px if vvv_px > 0 else 0.0
        usdc_usd = (float(usdc_units) / u_dec) * usdc_px
        total = diem_usd + vvv_usd + usdc_usd
        return total, {"DIEM": diem_usd, "VVV": vvv_usd, "USDC": usdc_usd}

    # --- slippage ---
    def check_slippage(self, exec_price: float, ref_price: float) -> Dict[str, float | bool]:
        """Return ok + slippage_bps given execution vs reference price.

        Positive slippage_bps indicates execution worse than reference.
        """
        if ref_price <= 0 or exec_price <= 0:
            return {"ok": False, "slippage_bps": float("inf")}
        try:
            # if we're selling DIEM for USDC, worse price means exec_price < ref_price
            slip = max(0.0, (ref_price - exec_price) / ref_price * 10_000.0)
        except Exception:
            slip = float("inf")
        return {"ok": slip <= float(self.slippage_bps_cap), "slippage_bps": float(slip)}
