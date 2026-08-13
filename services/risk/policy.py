from __future__ import annotations

import math
import os
from dataclasses import dataclass


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
    slippage_bps_cap: int = 50  # 0.5% default cap
    premium_threshold: float = 1.05
    discount_threshold: float = 1.05
    pool_take_bps_cap: int = 25
    slippage_premium_mult: float = 2.0
    slippage_hard_cap_bps: int = 300
    _diem_decimals_cache: int | None = None
    max_stake_usd: float | None = None

    @classmethod
    def from_env(cls) -> RiskPolicy:
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
                # Support scientific notation (e.g., "5e17")
                return int(float(v))
            except Exception:
                return default

        premium = _f("DIEM_PREMIUM_THRESHOLD", 1.05)
        if premium <= 1.0:
            premium = 1.05
        discount = _f("DIEM_DISCOUNT_THRESHOLD", 0.0)
        if discount <= 1.0:
            discount = premium

        return cls(
            max_trade_usd=_f("RISK_MAX_DIEM_TRADE_USD", 10_000.0),
            max_inventory_usd=_f("RISK_MAX_DIEM_INVENTORY_USD", 100_000.0),
            max_trade_units=_i("RISK_MAX_DIEM_TRADE_UNITS", 0),
            slippage_bps_cap=_i("RISK_MAX_SLIPPAGE_BPS", 50),
            premium_threshold=premium,
            discount_threshold=discount,
            pool_take_bps_cap=_i("RISK_MAX_POOL_TAKE_BPS", 25),
            slippage_premium_mult=_f("RISK_DIEM_SLIPPAGE_PREMIUM_MULT", 2.0),
            slippage_hard_cap_bps=_i("RISK_DIEM_SLIPPAGE_HARD_CAP_BPS", 300),
            max_stake_usd=_f("RISK_MAX_STAKE_USD", 0.0) or None,
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
            from web3 import Web3  # type: ignore

            from libs.agentkit_ext.web3_utils import get_contract, get_web3

            addr = os.getenv("DIEM_TOKEN_ADDRESS")
            if not addr:
                raise OSError(
                    "DIEM_TOKEN_ADDRESS must be set or DIEM_DECIMALS provided"
                )
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
        return int(tokens * (10**decimals))

    def usd_from_units(self, units: int, price_usd_per_diem: float) -> float:
        decimals = self._diem_decimals()
        tokens = units / float(10**decimals)
        v = tokens * price_usd_per_diem
        # Guard against tiny float overshoot above budget thresholds
        return max(0.0, v - 1e-9)

    # --- policy decisions ---
    def max_allowed_units(
        self, price_usd_per_diem: float, current_inventory_usd: float | None = None
    ) -> int:
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
        current_inventory_usd: float | None = None,
    ) -> int:
        limit = self.max_allowed_units(price_usd_per_diem, current_inventory_usd)
        return min(desired_units, limit)

    def allow_trade_units(
        self,
        desired_units: int,
        price_usd_per_diem: float,
        current_inventory_usd: float | None = None,
    ) -> dict[str, object]:
        suggested = self.suggest_trade_units(
            desired_units, price_usd_per_diem, current_inventory_usd
        )
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
        prices_usd: dict[str, float] | None = None,
        vvv_decimals: int = 18,
        usdc_decimals: int = 6,
    ) -> tuple[float, dict[str, float]]:
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

    # --- staking caps ---
    def max_stake(
        self,
        vvv_price_usd: float,
        *,
        current_staked_usd: float | None = None,
        vvv_decimals: int = 18,
    ) -> int:
        """Return max additional VVV units (base) allowed to stake by USD cap.

        Uses env RISK_MAX_STAKE_USD when provided; otherwise falls back to max_inventory_usd.
        """
        if vvv_price_usd <= 0:
            return 0
        cap_usd = (
            float(self.max_stake_usd)
            if (self.max_stake_usd is not None)
            else float(self.max_inventory_usd)
        )
        remaining = cap_usd
        if current_staked_usd is not None:
            remaining = max(0.0, cap_usd - float(current_staked_usd))
        tokens = remaining / float(vvv_price_usd)
        return int(tokens * (10 ** int(vvv_decimals)))

    def max_stake_from_prices(
        self,
        prices_usd: dict[str, float],
        *,
        current_staked_units: int = 0,
        vvv_decimals: int = 18,
    ) -> int:
        """Convenience wrapper to compute max stake from price map and current units."""
        vvv_px = float(prices_usd.get("VVV", 0.0) or 0.0)
        if vvv_px <= 0:
            return 0
        cur_usd = (
            float(current_staked_units) / float(10 ** int(vvv_decimals))
        ) * vvv_px
        return self.max_stake(
            vvv_px, current_staked_usd=cur_usd, vvv_decimals=vvv_decimals
        )

    # --- slippage ---
    def check_slippage(
        self, exec_price: float, ref_price: float, *, cap_bps: float | None = None
    ) -> dict[str, float | bool]:
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
        try:
            cap_val = (
                float(cap_bps) if cap_bps is not None else float(self.slippage_bps_cap)
            )
        except Exception:
            cap_val = float(self.slippage_bps_cap)
        return {"ok": slip <= float(cap_val), "slippage_bps": float(slip)}

    def _compute_dynamic_slippage_cap(
        self,
        *,
        price_ratio: float | None,
        liquidity_slippage_bps: float | None = None,
    ) -> float:
        """Scale slippage cap with premium/discount magnitude while honoring a hard cap."""

        base_cap = max(0.0, float(self.slippage_bps_cap))
        try:
            hard_cap = max(base_cap, float(self.slippage_hard_cap_bps))
        except Exception:
            hard_cap = base_cap
        try:
            mult = max(0.0, float(self.slippage_premium_mult))
        except Exception:
            mult = 0.0

        def _premium_bps() -> float:
            try:
                if price_ratio is None:
                    return 0.0
                ratio = float(price_ratio)
                if ratio <= 0 or not math.isfinite(ratio):
                    return 0.0
                return abs(ratio - 1.0) * 10_000.0
            except Exception:
                return 0.0

        premium_bps = _premium_bps()
        liquidity_floor = 0.0
        if liquidity_slippage_bps is not None:
            try:
                liquidity_floor = max(0.0, float(liquidity_slippage_bps))
            except Exception:
                liquidity_floor = 0.0

        signal_bps = max(premium_bps, liquidity_floor)
        if mult <= 0 or signal_bps <= 0:
            return float(base_cap)

        scaled = signal_bps * mult
        effective = min(hard_cap, scaled)
        # Never go below the static cap so quiet markets retain baseline protection
        return float(max(base_cap, effective))

    # --- utilization/volatility hooks (optional) ---
    def utilization_multiplier(self, utilization_ratio: float | None) -> float:
        """Return sizing multiplier based on utilization in [0,1].

        Uses env RISK_UTIL_ALPHA (default 0.5) so that multiplier = 1 + alpha * util.
        If utilization is None or invalid, returns 1.0.
        """
        try:
            if utilization_ratio is None:
                return 1.0
            u = max(0.0, min(1.0, float(utilization_ratio)))
            alpha = float(os.getenv("RISK_UTIL_ALPHA", "0.5") or 0.5)
            return max(0.0, 1.0 + alpha * u)
        except Exception:
            return 1.0

    def volatility_bps(self, prices: list[float]) -> float:
        """Compute simple realized volatility (bps) of log returns over the sample.

        Returns 0.0 on invalid input. Not annualized; intended as a relative signal.
        """
        try:
            import math

            xs = [float(p) for p in prices if float(p) > 0]
            if len(xs) < 2:
                return 0.0
            rets = [math.log(xs[i] / xs[i - 1]) for i in range(1, len(xs))]
            mu = sum(rets) / float(len(rets))
            var = sum((r - mu) ** 2 for r in rets) / float(max(1, len(rets) - 1))
            vol = (max(0.0, var)) ** 0.5
            return float(vol * 10_000.0)
        except Exception:
            return 0.0

    def cap_by_volatility(self, units: int, vol_bps: float | None) -> int:
        """Optionally reduce units when volatility exceeds a cap.

        Env RISK_MAX_VOLATILITY_BPS (default: disabled if <=0). If vol_bps is None, no change.
        Scales units by (cap / vol) when vol > cap.
        """
        try:
            cap = float(os.getenv("RISK_MAX_VOLATILITY_BPS", "0") or 0.0)
            if vol_bps is None or cap <= 0:
                return int(units)
            v = float(vol_bps)
            if v <= 0:
                return int(units)
            if v <= cap:
                return int(units)
            scale = max(0.0, min(1.0, cap / v))
            return int(int(units) * scale)
        except Exception:
            return int(units)

    def size_with_risk(
        self,
        desired_units: int,
        price_usd_per_diem: float,
        *,
        current_inventory_usd: float | None = None,
        utilization_ratio: float | None = None,
        vol_bps: float | None = None,
        reserve_cap_units: int | None = None,
    ) -> int:
        """Combined sizing: base gate -> utilization multiplier -> volatility cap -> reserve cap.

        Returns non-negative int units.
        """
        base = self.suggest_trade_units(
            desired_units, price_usd_per_diem, current_inventory_usd
        )
        if base <= 0:
            return 0
        # Utilization multiplier
        mult = self.utilization_multiplier(utilization_ratio)
        try:
            sized = int(max(0, int(base) * mult))
        except Exception:
            sized = int(base)
        # Volatility cap
        sized2 = self.cap_by_volatility(sized, vol_bps)
        # Optional pool reserve cap
        if reserve_cap_units is not None and int(reserve_cap_units) >= 0:
            try:
                sized2 = min(int(sized2), int(reserve_cap_units))
            except Exception:
                pass
        return max(0, int(sized2))

    # --- guardrail thresholds ---
    @staticmethod
    def _coerce_threshold(value: float | None, default: float) -> float:
        try:
            val = float(value) if value is not None else float(default)
        except Exception:
            return float(default)
        if not (val > 1.0):
            return float(default)
        return float(val)

    def premium_trigger(self, default: float = 1.05) -> float:
        """Env-driven premium multiple to trigger mint/sell."""
        return self._coerce_threshold(self.premium_threshold, default)

    def discount_trigger(self, default: float | None = None) -> float:
        """Env-driven discount multiple to trigger buy/burn."""
        base = self.premium_trigger(default or 1.05)
        return self._coerce_threshold(self.discount_threshold, base)

    def pool_take_cap_bps(self, default: int = 25) -> int:
        """Max pool take in basis points for venue reserve caps."""
        try:
            cap = int(self.pool_take_bps_cap)
        except Exception:
            cap = int(default)
        if cap <= 0:
            return int(default)
        return cap
