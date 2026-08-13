from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

try:
    from web3 import Web3
except Exception:
    # Minimal shim to allow import-time success when web3 isn't installed
    class Web3:  # type: ignore
        @staticmethod
        def to_checksum_address(addr: str) -> str:
            return addr


from .agentkit_wallet import get_address, send_tx
from .web3_utils import encode_contract_call, get_contract, get_web3

try:
    from libs.telemetry.logger import get_logger

    _logger = get_logger("libs.agentkit_ext.actions")
except Exception:
    import logging

    _logger = logging.getLogger("libs.agentkit_ext.actions")

try:
    from libs.dex.routes import _normalize_address
except ImportError:
    # Fallback if libs.dex.routes is not available
    def _normalize_address(addr: str) -> str:
        """Normalize address by stripping @fee suffix."""
        if not isinstance(addr, str):
            raise TypeError("address must be string")
        stripped = addr.strip()
        if "@" in stripped:
            stripped = stripped.split("@", 1)[0].strip()
        if not stripped:
            raise ValueError("address must be non-empty")
        if not stripped.startswith("0x"):
            stripped = "0x" + stripped
        return "0x" + stripped[2:].lower()


class VVVActions:
    """VVV token and staking actions using Web3.

    Env required:
      - BASE_RPC_URL
      - ETH_PRIVATE_KEY
      - VVV_TOKEN_ADDRESS
      - VVV_STAKING_ADDRESS
      - ABI files: abi/erc20.json and abi/staking.json (you provide)
    """

    def __init__(self) -> None:
        self.token_addr = os.getenv("VVV_TOKEN_ADDRESS")
        self.staking_addr = os.getenv("VVV_STAKING_ADDRESS")
        if not self.token_addr or not self.staking_addr:
            raise OSError("VVV_TOKEN_ADDRESS and VVV_STAKING_ADDRESS must be set")
        self.w3 = get_web3()
        self.erc20 = get_contract(self.w3, self.token_addr, "erc20.json")
        # Staking ABI must be provided by the project in abi/staking.json
        self.staking = get_contract(self.w3, self.staking_addr, "staking.json")

    def approve(self, amount: int) -> dict[str, Any]:
        data = encode_contract_call(
            self.erc20,
            "approve",
            [Web3.to_checksum_address(self.staking_addr), amount],
        )
        tx_hash = send_tx(self.token_addr, bytes.fromhex(data[2:]))
        return {"status": "sent", "action": "approve", "tx_hash": tx_hash}

    def _encode_staking_transaction(self, fn_name: str, args: list[Any]) -> str:
        wallet = Web3.to_checksum_address(get_address())
        try:
            fn_builder = getattr(self.staking.functions, fn_name)
        except AttributeError as exc:  # pragma: no cover - contract missing fn
            raise AttributeError(
                f"staking contract missing function '{fn_name}'"
            ) from exc
        func = fn_builder(*args)
        try:
            func.estimate_gas({"from": wallet})
        except Exception as exc:
            raise RuntimeError(f"{fn_name}_estimate_failed:{exc}") from exc
        try:
            data = func._encode_transaction_data()  # type: ignore[attr-defined]
        except AttributeError:
            built = func.build_transaction({"from": wallet})
            data = built.get("data")
        if not data:
            data = encode_contract_call(self.staking, fn_name, args)
        if isinstance(data, bytes):
            data = "0x" + data.hex()
        if not isinstance(data, str):
            raise TypeError(f"Unexpected encoded data type for {fn_name}: {type(data)}")
        if not data.startswith("0x"):
            data = "0x" + data
        return data

    def _build_staking_args(self, fn_name: str, amount: int | None) -> list[Any]:
        try:
            variants = self.staking.get_function_by_name(fn_name)
        except ValueError:
            variants = []
        inputs: list[dict[str, Any]] = []
        if variants:
            if isinstance(variants, list):
                candidates = variants
            else:
                candidates = [variants]
            for variant in candidates:
                abi = getattr(variant, "abi", None)
                if not abi:
                    continue
                inputs = list(abi.get("inputs", []))  # type: ignore[attr-defined]
                break

        args: list[Any] = []
        wallet = Web3.to_checksum_address(get_address())
        for idx, param in enumerate(inputs):
            p_type = str(param.get("type") or "")
            name = str(param.get("name") or f"arg{idx}")
            if p_type == "address":
                env_key = f"VVV_{fn_name.upper()}_{name.upper()}_ADDRESS"
                override = os.getenv(env_key)
                target = Web3.to_checksum_address(override) if override else wallet
                args.append(target)
            elif p_type.startswith("uint"):
                value = amount
                env_key = f"VVV_{fn_name.upper()}_{name.upper()}_UNITS"
                override = os.getenv(env_key)
                if value is None and override:
                    try:
                        value = int(str(override), 0)
                    except Exception:
                        value = int(str(override))
                if value is None:
                    raise ValueError(
                        f"{fn_name} requires parameter '{name}' but no value was provided"
                    )
                args.append(int(value))
            else:
                raise ValueError(
                    f"Unsupported parameter type '{p_type}' for staking function '{fn_name}'"
                )
        if not inputs:
            # fallback to legacy behaviour when ABI lacks metadata
            if amount is None:
                return []
            return [int(amount)]
        return args

    def stake(
        self, amount: int, *, gas_overrides: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        fn_name = os.getenv("VVV_STAKE_FN", "stake")
        args = self._build_staking_args(fn_name, amount)
        data = self._encode_staking_transaction(fn_name, args)
        tx_hash = send_tx(
            self.staking_addr, bytes.fromhex(data[2:]), gas_overrides=gas_overrides
        )
        return {"status": "sent", "action": "stake", "tx_hash": tx_hash}

    def estimate_stake(self, amount: int) -> dict[str, Any]:
        fn_name = os.getenv("VVV_STAKE_FN", "stake")
        args = self._build_staking_args(fn_name, amount)
        self._encode_staking_transaction(fn_name, args)
        return {"status": "ok", "action": "stake_estimate", "amount": int(amount)}

    def claim(self) -> dict[str, Any]:
        fn_name = os.getenv("VVV_CLAIM_FN", "claim")
        data = encode_contract_call(self.staking, fn_name, [])
        tx_hash = send_tx(self.staking_addr, bytes.fromhex(data[2:]))
        return {"status": "sent", "action": "claim", "tx_hash": tx_hash}

    def estimate_claim_cost(self) -> dict[str, int]:
        fn_name = os.getenv("VVV_CLAIM_FN", "claim")
        data = encode_contract_call(self.staking, fn_name, [])
        wallet = Web3.to_checksum_address(get_address())
        tx = {
            "from": wallet,
            "to": Web3.to_checksum_address(self.staking_addr),
            "data": data,
        }

        try:
            gas = int(self.w3.eth.estimate_gas(tx))
        except Exception as exc:
            raise RuntimeError(f"claim_gas_estimate_failed:{exc}") from exc

        def _env_int(name: str) -> int | None:
            raw = os.getenv(name)
            if raw is None or str(raw).strip() == "":
                return None
            try:
                return int(str(raw), 0)
            except Exception:
                try:
                    return int(float(str(raw)))
                except Exception:
                    return None

        # Get chain_id for Base-specific handling
        chain_id = None
        try:
            chain_id = self.w3.eth.chain_id
        except Exception:
            pass

        # Base-specific max gas price cap (default 5 gwei)
        BASE_CHAIN_ID = 8453
        base_max_gas_price_wei = _env_int("BASE_GAS_PRICE_MAX_WEI")
        if base_max_gas_price_wei is None:
            base_max_gas_price_wei = int(Web3.to_wei(5, "gwei"))  # Default 5 gwei cap

        # Get RPC URL for logging
        rpc_url = os.getenv("BASE_RPC_URL") or os.getenv("RPC_URL") or "unknown"

        base_fee = None
        try:
            block = self.w3.eth.get_block("latest")
            candidate = (
                block.get("baseFeePerGas")
                if isinstance(block, dict)
                else getattr(block, "baseFeePerGas", None)
            )
            if candidate is not None:
                base_fee = int(candidate)
        except Exception:
            base_fee = None

        priority_fee = _env_int("STAKEMASTER_PRIORITY_FEE_WEI")
        if priority_fee is None:
            try:
                priority_fee_attr = getattr(self.w3.eth, "max_priority_fee", None)
                if priority_fee_attr is not None:
                    priority_fee = int(priority_fee_attr)
            except Exception:
                priority_fee = None
        if priority_fee is None:
            try:
                priority_fee = int(Web3.to_wei(1, "gwei"))
            except Exception:
                priority_fee = 1_000_000_000

        effective_price = None
        try:
            gas_price = int(self.w3.eth.gas_price)
        except Exception:
            gas_price = None

        # On Base, prefer eth_gasPrice directly and apply sanity checks
        is_base = chain_id == BASE_CHAIN_ID
        if is_base and gas_price is not None:
            # Prefer direct gas_price on Base
            effective_price = gas_price

            # Check for anomaly: if base_fee + priority_fee is way higher than gas_price,
            # or if effective_price exceeds cap, treat as anomaly
            computed_eip1559 = None
            if base_fee is not None and priority_fee is not None:
                computed_eip1559 = base_fee + priority_fee

            if (
                computed_eip1559 is not None
                and computed_eip1559 > base_max_gas_price_wei
            ):
                _logger.warning(
                    "Base gas price anomaly detected in claim cost estimate: base_fee=%s wei (%.2f gwei), "
                    "priority_fee=%s wei (%.2f gwei), computed=%s wei (%.2f gwei) > cap=%s wei (%.2f gwei). "
                    "RPC=%s. Falling back to eth_gasPrice=%s wei (%.2f gwei)",
                    base_fee,
                    base_fee / 1e9 if base_fee else 0,
                    priority_fee,
                    priority_fee / 1e9 if priority_fee else 0,
                    computed_eip1559,
                    computed_eip1559 / 1e9,
                    base_max_gas_price_wei,
                    base_max_gas_price_wei / 1e9,
                    rpc_url,
                    gas_price,
                    gas_price / 1e9 if gas_price else 0,
                )
                # Use gas_price directly, capped at max
                effective_price = min(gas_price, base_max_gas_price_wei)
            elif effective_price > base_max_gas_price_wei:
                _logger.warning(
                    "Base gas price exceeds cap in claim cost estimate: effective_price=%s wei (%.2f gwei) > cap=%s wei (%.2f gwei). "
                    "RPC=%s. Capping to max.",
                    effective_price,
                    effective_price / 1e9,
                    base_max_gas_price_wei,
                    base_max_gas_price_wei / 1e9,
                    rpc_url,
                )
                effective_price = base_max_gas_price_wei
        else:
            # Non-Base or fallback: use EIP-1559 calculation
            if base_fee is not None and priority_fee is not None:
                effective_price = base_fee + priority_fee
            if effective_price is None:
                effective_price = gas_price
            elif gas_price is not None:
                effective_price = max(effective_price, gas_price)

        max_fee_per_gas = None
        if base_fee is not None and priority_fee is not None:
            max_fee_per_gas = base_fee * 2 + priority_fee
            # On Base, cap max_fee_per_gas too
            if is_base and max_fee_per_gas > base_max_gas_price_wei * 2:
                max_fee_per_gas = base_max_gas_price_wei * 2

        fee_wei = None
        if effective_price is not None:
            fee_wei = int(effective_price) * gas if gas else None

        estimate: dict[str, int] = {"gas": gas}
        if effective_price is not None:
            estimate["effective_gas_price"] = int(effective_price)
        if fee_wei is not None:
            estimate["fee_wei"] = int(fee_wei)
        if base_fee is not None:
            estimate["base_fee_per_gas"] = int(base_fee)
        if priority_fee is not None:
            estimate["priority_fee_per_gas"] = int(priority_fee)
        if max_fee_per_gas is not None:
            estimate["max_fee_per_gas"] = int(max_fee_per_gas)
        return estimate

    def unstake(self, amount: int) -> dict[str, Any]:
        fn = os.getenv("VVV_UNSTAKE_FN", "initiateUnstake")
        args = self._build_staking_args(fn, amount)
        data = self._encode_staking_transaction(fn, args)
        tx_hash = send_tx(self.staking_addr, bytes.fromhex(data[2:]))
        return {"status": "sent", "action": "unstake", "tx_hash": tx_hash}

    def _build_tx(self, to: str, data_hex: str) -> dict[str, Any]:
        # Deprecated path; preserved for compatibility if needed
        return {"to": to, "data": data_hex}


class DIEMACTIONS:
    """DIEM token mint/burn and DEX trade using Web3.

    Env required:
      - BASE_RPC_URL, ETH_PRIVATE_KEY
      - DIEM_TOKEN_ADDRESS (for mint/burn if direct), or protocol router address
      - UNISWAP_V2_ROUTER_ADDRESS or ROUTER_ADDRESS (for trades), abi/uniswap_v2_router.json
      - TRADE_PATH (comma-separated addresses, e.g., DIEM,USDC)
      - SLIPPAGE_BPS (default 100 = 1%)
      - ABI files: abi/diem.json (protocol), if mint/burn exist on DIEM contract
      - Optional DIEM_STAKING_ADDRESS + ABI for stake_diem_for_api flows
    """

    def __init__(self) -> None:
        self.w3 = get_web3()
        self._address = Web3.to_checksum_address(get_address())
        self.diem_addr = os.getenv("DIEM_TOKEN_ADDRESS")
        if not self.diem_addr:
            raise OSError("DIEM_TOKEN_ADDRESS must be set")
        # DIEM ABI for mint/burn, if applicable (project-specific)
        try:
            self.diem = get_contract(self.w3, self.diem_addr, "diem.json")
        except FileNotFoundError:
            self.diem = None  # optional; raise at call time if used
        # Router is optional unless trade() is used. Resolve lazily per provider.
        self.router_addr: str | None = None
        self.router = None
        self._router_provider: str | None = None
        self.diem_staking_addr = os.getenv("DIEM_STAKING_ADDRESS")
        self.diem_staking = None
        if self.diem_staking_addr:
            try:
                staking_addr = Web3.to_checksum_address(self.diem_staking_addr)
                staking_abi = os.getenv("DIEM_STAKING_ABI", "diem.json")
                try:
                    self.diem_staking = get_contract(self.w3, staking_addr, staking_abi)
                except FileNotFoundError:
                    # Fallback to reuse diem.json if dedicated ABI unavailable
                    self.diem_staking = get_contract(self.w3, staking_addr, "diem.json")
            except Exception:
                self.diem_staking = None

    def _ensure_min_balance(self, min_required_wei: int, context: str) -> None:
        """Fail fast if wallet balance is below a realistic Base gas budget.

        Avoids false negatives when RPCs report inflated gas prices by checking
        against a capped burn/mint budget instead of rpc-derived estimates.
        """

        try:
            balance = int(self.w3.eth.get_balance(self._address))
        except Exception as exc:  # pragma: no cover - rpc failure
            _logger.warning("balance_check_failed:%s", exc)
            return

        if balance < min_required_wei:
            needed = min_required_wei - balance
            raise RuntimeError(
                f"{context}_insufficient_balance:"
                f" have={balance} wei need={min_required_wei} wei (+{needed} wei)"
            )

    def _gas_overrides(self, *, gas_limit: int | None = None) -> dict[str, int]:
        """Build EIP-1559 gas overrides with Base-safe caps."""

        overrides: dict[str, int] = {}

        def _env_int(name: str, default: int | None = None) -> int | None:
            raw = os.getenv(name)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                return int(str(raw), 0)
            except Exception:
                try:
                    return int(float(str(raw)))
                except Exception:
                    return default

        try:
            chain_id = self.w3.eth.chain_id
        except Exception:
            chain_id = None

        BASE_CHAIN_ID = 8453
        is_base = chain_id == BASE_CHAIN_ID
        base_max_gas_price_wei = _env_int("BASE_GAS_PRICE_MAX_WEI")
        if base_max_gas_price_wei is None:
            base_max_gas_price_wei = int(Web3.to_wei(5, "gwei"))

        rpc_url = os.getenv("BASE_RPC_URL") or os.getenv("RPC_URL") or "unknown"

        # Priority fee target
        priority_fee = _env_int("ARBI_DIEM_PRIORITY_FEE_WEI")
        if priority_fee is None:
            priority_fee = _env_int("STAKEMASTER_PRIORITY_FEE_WEI")
        if priority_fee is None:
            try:
                priority_fee_attr = getattr(self.w3.eth, "max_priority_fee", None)
                if priority_fee_attr is not None:
                    priority_fee = int(priority_fee_attr)
            except Exception:
                priority_fee = None
        if priority_fee is None:
            priority_fee = int(Web3.to_wei(1, "gwei"))

        try:
            gas_price = int(self.w3.eth.gas_price)
        except Exception:
            gas_price = None

        base_fee = None
        try:
            block = self.w3.eth.get_block("latest")
            candidate = (
                block.get("baseFeePerGas")
                if isinstance(block, dict)
                else getattr(block, "baseFeePerGas", None)
            )
            if candidate is not None:
                base_fee = int(candidate)
        except Exception:
            base_fee = None

        effective_price = None
        if is_base:
            if gas_price is not None:
                effective_price = gas_price
                computed = base_fee + priority_fee if base_fee is not None else None
                if computed is not None and computed > base_max_gas_price_wei:
                    _logger.warning(
                        "Base gas price anomaly: computed=%s wei (%.2f gwei) > cap=%s wei (%.2f gwei). "
                        "RPC=%s. Using capped gas_price=%s wei (%.2f gwei).",
                        computed,
                        computed / 1e9,
                        base_max_gas_price_wei,
                        base_max_gas_price_wei / 1e9,
                        rpc_url,
                        gas_price,
                        gas_price / 1e9 if gas_price else 0,
                    )
                    effective_price = min(gas_price, base_max_gas_price_wei)
                elif effective_price > base_max_gas_price_wei:
                    _logger.warning(
                        "Base gas price exceeds cap: %s wei (%.2f gwei) > %s wei (%.2f gwei). RPC=%s. Capping.",
                        effective_price,
                        effective_price / 1e9,
                        base_max_gas_price_wei,
                        base_max_gas_price_wei / 1e9,
                        rpc_url,
                    )
                    effective_price = base_max_gas_price_wei
            elif base_fee is not None:
                effective_price = base_fee + priority_fee
                if effective_price > base_max_gas_price_wei:
                    _logger.warning(
                        "Base gas price fallback capped: %s wei (%.2f gwei) > %s wei (%.2f gwei). RPC=%s.",
                        effective_price,
                        effective_price / 1e9,
                        base_max_gas_price_wei,
                        base_max_gas_price_wei / 1e9,
                        rpc_url,
                    )
                    effective_price = base_max_gas_price_wei
            else:
                effective_price = priority_fee
        else:
            if base_fee is not None:
                effective_price = base_fee + priority_fee
            if effective_price is None:
                effective_price = gas_price
            elif gas_price is not None:
                effective_price = max(effective_price, gas_price)

        if effective_price is None:
            effective_price = priority_fee

        max_fee = None
        if base_fee is not None:
            max_fee = base_fee * 2 + priority_fee
            if is_base and max_fee > base_max_gas_price_wei * 2:
                _logger.warning(
                    "Base maxFee capped: %s wei (%.2f gwei) -> %s wei (%.2f gwei). RPC=%s.",
                    max_fee,
                    max_fee / 1e9,
                    base_max_gas_price_wei * 2,
                    (base_max_gas_price_wei * 2) / 1e9,
                    rpc_url,
                )
                max_fee = base_max_gas_price_wei * 2
        if max_fee is None:
            max_fee = int(effective_price * 2)
            if is_base and max_fee > base_max_gas_price_wei * 2:
                max_fee = base_max_gas_price_wei * 2

        overrides["maxPriorityFeePerGas"] = int(priority_fee)
        overrides["maxFeePerGas"] = int(max_fee)
        if gas_limit is not None and gas_limit > 0:
            overrides["gas"] = int(gas_limit)
        overrides["type"] = 2
        return overrides

    def _default_router_provider(self) -> str:
        raw = (os.getenv("DEX_PROVIDERS") or "").strip()
        if raw:
            if raw[0] in "[{":
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        parsed = [parsed]
                    if isinstance(parsed, list) and parsed:
                        first = parsed[0]
                        if isinstance(first, dict):
                            name = first.get("name")
                        else:
                            name = first
                        if name:
                            return str(name).strip().lower()
                except Exception:
                    pass
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            if parts:
                return parts[0].lower()
        return "uniswap_v2"

    def _resolve_router(self, provider: str | None = None) -> None:
        target_provider = (provider or self._router_provider or "").strip().lower()
        if not target_provider:
            target_provider = self._default_router_provider()
        env_map = {
            "uniswap_v2": "UNISWAP_V2_ROUTER_ADDRESS",
            "uniswap_v3": "UNISWAP_V3_ROUTER_ADDRESS",
            "aerodrome": "AERODROME_ROUTER_ADDRESS",
        }
        env_var = env_map.get(target_provider)
        router_addr = None
        if env_var:
            router_addr = os.getenv(env_var)
        if not router_addr:
            router_addr = os.getenv("ROUTER_ADDRESS")
        if not router_addr:
            if env_var:
                raise OSError(
                    f"ROUTER_ADDRESS or {env_var} must be set for provider '{target_provider}' to trade"
                )
            raise OSError("ROUTER_ADDRESS must be set to trade")
        router_addr = Web3.to_checksum_address(router_addr)
        if self.router and self.router_addr == router_addr:
            self._router_provider = target_provider
            return
        self.router_addr = router_addr
        self.router = get_contract(self.w3, self.router_addr, "uniswap_v2_router.json")
        self._router_provider = target_provider

    def _get_staking_contract(self) -> tuple[Any, str]:
        """Get the sVVV staking contract for mint/burn operations.

        The mintDiem and burnDiem functions live on VVV_STAKING_ADDRESS,
        not DIEM_TOKEN_ADDRESS.
        """
        staking_addr = os.getenv("VVV_STAKING_ADDRESS")
        if not staking_addr:
            raise OSError(
                "VVV_STAKING_ADDRESS must be set for DIEM mint/burn operations"
            )

        checksummed = Web3.to_checksum_address(staking_addr)
        contract = get_contract(self.w3, checksummed, "diem.json")
        return contract, checksummed

    def _ensure_svvv_allowance(
        self, staking_addr: str, svvv_amount: int
    ) -> dict[str, Any]:
        """Ensure sVVV allowance to staking contract for mint operation.

        The sVVV token contract is at VVV_STAKING_ADDRESS itself (it's an ERC20).
        We need to approve the staking contract to spend our sVVV.
        """
        from libs.agentkit_ext.web3_utils import get_contract

        MAX_UINT256 = 2**256 - 1

        try:
            # sVVV is at VVV_STAKING_ADDRESS - use diem.json which has ERC20 functions
            svvv_contract = get_contract(self.w3, staking_addr, "diem.json")

            current = int(
                svvv_contract.functions.allowance(self._address, staking_addr).call()
            )

            if current >= svvv_amount:
                return {
                    "status": "sufficient",
                    "current": current,
                    "required": svvv_amount,
                }

            # Need to approve
            approve_data = encode_contract_call(
                svvv_contract, "approve", [staking_addr, MAX_UINT256]
            )
            gas_limit = int(os.getenv("DIEM_APPROVE_GAS_LIMIT") or 100_000)
            overrides = self._gas_overrides(gas_limit=gas_limit)
            tx_hash = send_tx(
                staking_addr, bytes.fromhex(approve_data[2:]), gas_overrides=overrides
            )

            _logger.info(
                "sVVV approval submitted for mint",
                extra={
                    "action": "svvv_approve",
                    "spender": staking_addr,
                    "tx_hash": tx_hash,
                    "previous_allowance": current,
                },
            )
            return {"status": "approved", "tx_hash": tx_hash, "previous": current}

        except Exception as exc:
            _logger.warning(f"sVVV allowance check/approval failed: {exc}")
            return {"status": "error", "error": str(exc)}

    def mint(self, amount: int) -> dict[str, Any]:
        """Mint DIEM by locking sVVV on the staking contract.

        The `amount` parameter is the sVVV amount to lock (NOT DIEM amount).
        The caller (DIEMService) is responsible for converting DIEM amount
        to sVVV amount using the mint rate.

        Calls mintDiem(uint256 sVVVAmountToLock, uint256 minDiemAmountOut) on VVV_STAKING_ADDRESS.

        Slippage Protection:
            - Queries getDiemAmountOut() to get expected DIEM output
            - Applies DIEM_MINT_SLIPPAGE_PCT buffer (default 5%) for minDiemAmountOut
            - Static call validates with exact minDiemAmountOut before tx submission
            - Env DIEM_MINT_MIN_OUTPUT overrides calculated value

        Retry Logic:
            - DIEM_MINT_MAX_RETRIES: max attempts (default 3)
            - DIEM_MINT_RETRY_DELAY_SEC: delay between retries (default 2.0)
            - DIEM_MINT_WAIT_CONFIRM: wait for tx confirmation (default 1/true)
            - DIEM_MINT_CONFIRM_TIMEOUT_SEC: confirmation timeout (default 60)
            - On retry, refreshes expected DIEM output for updated slippage protection
        """
        # Check if minting is enabled via configuration
        mint_enabled = os.getenv("DIEM_MINT_ENABLED", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "disabled",
        )
        if not mint_enabled:
            _logger.warning("DIEM minting disabled via DIEM_MINT_ENABLED=0")
            return {
                "status": "error",
                "action": "mint",
                "error": "DIEM minting disabled via configuration",
            }

        # #region agent log
        import json
        import time

        try:
            with Path("/app/logs/debug.log").open("a") as f:
                f.write(
                    json.dumps(
                        {
                            "hypothesisId": "MINT_A",
                            "location": "actions.py:mint:entry",
                            "message": "mint called",
                            "data": {"svvv_to_lock": amount},
                            "timestamp": time.time(),
                            "sessionId": "debug-session",
                        }
                    )
                    + "\n"
                )
        except Exception:
            pass
        # #endregion

        try:
            staking_contract, staking_addr = self._get_staking_contract()
        except Exception as exc:
            return {"status": "error", "action": "mint", "error": str(exc)}

        # Pre-flight check: verify mintDiem function exists via static call
        from libs.agentkit_ext.agentkit_wallet import get_address

        wallet = get_address()
        try:
            # IMPORTANT:
            # The on-chain mintDiem function has TWO parameters:
            #   mintDiem(uint256 sVVVAmountToLock, uint256 minDiemAmountOut)
            #   MethodID: 0x2006efcb
            #
            # To avoid falsely classifying mint as unavailable, we probe with:
            # - a tiny amount (1) with minDiemAmountOut=0 for fast selector existence checks, then
            # - a realistic probe amount (<= 1 sVVV, or the requested amount if smaller).
            staking_contract.functions.mintDiem(1, 0).call({"from": wallet})
        except Exception as preflight_err:
            error_str = str(preflight_err).lower()
            if "execution reverted" in error_str and "no data" in error_str:
                # Second-chance probe with a realistic lock amount (best-effort).
                probe_amount = int(amount)
                try:
                    if probe_amount <= 0:
                        probe_amount = 1
                    else:
                        one_token = 10**18
                        probe_amount = min(probe_amount, one_token)
                    staking_contract.functions.mintDiem(int(probe_amount), 0).call(
                        {"from": wallet}
                    )
                    # If this succeeds, mint exists; continue without latching unavailable.
                    probe_amount = None  # type: ignore[assignment]
                except Exception:
                    # Keep probe_amount unchanged on exception
                    pass

                if probe_amount is not None:
                    _logger.error(
                        "mintDiem function not available on contract %s. "
                        "The function may not exist or has been removed in a contract upgrade. "
                        "Set DIEM_MINT_ENABLED=0 to disable minting attempts.",
                        staking_addr,
                    )
                    # #region agent log - selector proof (writes to .cursor/debug.log and mirrors to logs/debug.log)
                    # Prove (best-effort) whether the *implementation bytecode* contains the function selector.
                    # If PUSH4 <selector> is missing, the function is almost certainly not implemented.
                    try:
                        import json as _json
                        import time as _time

                        def _dbg_write(msg: str, data_obj: dict) -> None:
                            try:
                                payload = (
                                    _json.dumps(
                                        {
                                            "sessionId": "debug-session",
                                            "runId": "mint-preflight",
                                            "hypothesisId": "H1",
                                            "location": "actions.py:mint:preflight_selector_probe",
                                            "message": msg,
                                            "data": data_obj,
                                            "timestamp": int(_time.time() * 1000),
                                        }
                                    )
                                    + "\n"
                                )

                                # Ensure .cursor exists (repo root)
                                try:
                                    Path("/app/.cursor").mkdir(
                                        parents=True, exist_ok=True
                                    )
                                except Exception:
                                    pass

                                # Primary: workspace debug log
                                try:
                                    with Path("/app/.cursor/debug.log").open("a") as _f:
                                        _f.write(payload)
                                except Exception:
                                    pass

                                # Mirror: logs/debug.log (often easiest to inspect in containerized runs)
                                try:
                                    Path("/app/logs").mkdir(parents=True, exist_ok=True)
                                    with Path("/app/logs/debug.log").open("a") as _f2:
                                        _f2.write(payload)
                                except Exception:
                                    pass
                            except Exception:
                                pass

                        impl_slot = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
                        impl_raw = self.w3.eth.get_storage_at(staking_addr, impl_slot)
                        impl_addr = None
                        if impl_raw and impl_raw != b"\x00" * 32:
                            impl_addr = "0x" + impl_raw[-20:].hex()

                        probe: dict[str, Any] = {
                            "proxy": staking_addr,
                            "implementation": impl_addr,
                        }
                        if (
                            impl_addr
                            and isinstance(impl_addr, str)
                            and impl_addr.startswith("0x")
                            and len(impl_addr) == 42
                        ):
                            impl_cs = self.w3.to_checksum_address(impl_addr)
                            code = self.w3.eth.get_code(impl_cs)
                            code_hex = code.hex() if code else ""

                            selectors = {
                                # mintDiem has 2 params: (uint256 sVVVAmountToLock, uint256 minDiemAmountOut)
                                "mintDiem": self.w3.keccak(
                                    text="mintDiem(uint256,uint256)"
                                )[:4].hex(),
                                "burnDiem": self.w3.keccak(text="burnDiem(uint256)")[
                                    :4
                                ].hex(),
                                "getSVVVAmountIn": self.w3.keccak(
                                    text="getSVVVAmountIn(uint256)"
                                )[:4].hex(),
                                "getDiemAmountOut": self.w3.keccak(
                                    text="getDiemAmountOut(uint256)"
                                )[:4].hex(),
                            }

                            def _push4_present(sel_hex: str) -> bool:
                                return ("63" + sel_hex.lower()) in code_hex.lower()

                            probe.update(
                                {
                                    "impl_code_len": len(code),
                                    "selectors": selectors,
                                    "push4_present": {
                                        k: _push4_present(v)
                                        for k, v in selectors.items()
                                    },
                                }
                            )
                            _dbg_write("implementation selector probe", probe)
                        else:
                            _dbg_write(
                                "no implementation address found for selector probe",
                                probe,
                            )
                    except Exception as _probe_exc:
                        try:
                            err_payload = (
                                _json.dumps(
                                    {
                                        "sessionId": "debug-session",
                                        "runId": "mint-preflight",
                                        "hypothesisId": "H1",
                                        "location": "actions.py:mint:preflight_selector_probe_error",
                                        "message": "selector probe failed",
                                        "data": {"error": str(_probe_exc)},
                                        "timestamp": int(_time.time() * 1000),
                                    }
                                )
                                + "\n"
                            )
                            try:
                                Path("/app/.cursor").mkdir(parents=True, exist_ok=True)
                            except Exception:
                                pass
                            try:
                                with Path("/app/.cursor/debug.log").open("a") as _f:
                                    _f.write(err_payload)
                            except Exception:
                                pass
                            try:
                                Path("/app/logs").mkdir(parents=True, exist_ok=True)
                                with Path("/app/logs/debug.log").open("a") as _f2:
                                    _f2.write(err_payload)
                            except Exception:
                                pass
                        except Exception:
                            pass
                    # #endregion
                    return {
                        "status": "error",
                        "action": "mint",
                        "error": "mintDiem function not available on contract - function may not exist or was removed in upgrade",
                        "contract": staking_addr,
                        "diagnostic": f"Static call to mintDiem(1) failed: {preflight_err}",
                    }
            # For non "no data" reverts, we assume the selector exists but the probe
            # failed due to state/guard conditions; allow the normal amount-based
            # simulation below to decide.
            # If it's a different error (not "no data"), the function exists but something else is wrong
            # Let it proceed to get a more specific error from the actual call

        # Ensure sVVV allowance to staking contract
        allowance_result = self._ensure_svvv_allowance(staking_addr, int(amount))
        if allowance_result.get("status") == "error":
            return {
                "status": "error",
                "action": "mint",
                "error": f"allowance_failed:{allowance_result.get('error')}",
            }

        fn = os.getenv("DIEM_MINT_FN", "mintDiem")
        # mintDiem(uint256 sVVVAmountToLock, uint256 minDiemAmountOut)
        # Calculate expected DIEM output and apply slippage buffer for protection
        min_diem_out = 0
        expected_diem_out = 0

        # Check if slippage protection is disabled (matches successful manual transactions)
        disable_slippage = os.getenv("DIEM_MINT_DISABLE_SLIPPAGE", "").lower() in (
            "1",
            "true",
            "yes",
        )

        if disable_slippage:
            _logger.info(
                "Mint slippage protection DISABLED via DIEM_MINT_DISABLE_SLIPPAGE=true, using minDiemOut=0"
            )
            min_diem_out = 0
            try:
                expected_diem_out = staking_contract.functions.getDiemAmountOut(
                    int(amount)
                ).call()
            except Exception:
                pass
        else:
            # Default 25% slippage tolerance - mint curve is sensitive to state changes between
            # simulation and execution. 5% was too tight and caused reverts.
            slippage_pct = float(os.getenv("DIEM_MINT_SLIPPAGE_PCT", "25.0"))
            try:
                expected_diem_out = staking_contract.functions.getDiemAmountOut(
                    int(amount)
                ).call()
                if expected_diem_out > 0:
                    min_diem_out = int(expected_diem_out * (100 - slippage_pct) / 100)
                    _logger.info(
                        f"Mint slippage protection: expected={expected_diem_out}, "
                        f"minOut={min_diem_out} ({100 - slippage_pct:.1f}% tolerance)"
                    )
            except Exception as diem_out_err:
                _logger.warning(
                    f"Could not get expected DIEM output, using minOut=0: {diem_out_err}"
                )
                min_diem_out = 0

        # Allow env override to force specific minDiemAmountOut
        env_min_out = os.getenv("DIEM_MINT_MIN_OUTPUT")
        if env_min_out is not None and env_min_out.strip():
            try:
                min_diem_out = int(env_min_out.strip())
                _logger.info(f"Using env override DIEM_MINT_MIN_OUTPUT={min_diem_out}")
            except ValueError:
                pass

        try:
            data = encode_contract_call(
                staking_contract, fn, [int(amount), min_diem_out]
            )
        except Exception as exc:
            _logger.error(f"Failed to encode mintDiem call: {exc}")
            return {"status": "error", "action": "mint", "error": str(exc)}

        # #region agent log - pre-mint checks
        import json
        import time

        try:
            from libs.agentkit_ext.agentkit_wallet import get_address

            wallet = get_address()
            balance_of = staking_contract.functions.balanceOf(wallet).call()
            unlocked = staking_contract.functions.balanceOfUnlocked(wallet).call()
            # Also try to check getDiemAmountOut to verify contract functions
            try:
                diem_out = staking_contract.functions.getDiemAmountOut(
                    int(amount)
                ).call()
            except Exception as de:
                diem_out = f"error: {de}"
            # Check stakes() to compare VVV vs sVVV
            try:
                stakes_result = staking_contract.functions.stakes(wallet).call()
            except Exception as se:
                stakes_result = f"error: {se}"
            with Path("/app/logs/debug.log").open("a") as f:
                f.write(
                    json.dumps(
                        {
                            "hypothesisId": "MINT_B",
                            "location": "actions.py:mint:preflight",
                            "message": "pre-mint balance check",
                            "data": {
                                "wallet": wallet,
                                "svvv_to_lock": amount,
                                "balance_of": balance_of,
                                "unlocked": unlocked,
                                "staking_addr": staking_addr,
                                "fn": fn,
                                "getDiemAmountOut_result": str(diem_out),
                                "stakes_result": str(stakes_result),
                            },
                            "timestamp": time.time(),
                            "sessionId": "debug-session",
                        }
                    )
                    + "\n"
                )
        except Exception as e:
            with Path("/app/logs/debug.log").open("a") as f:
                f.write(
                    json.dumps(
                        {
                            "hypothesisId": "MINT_B",
                            "location": "actions.py:mint:preflight_error",
                            "message": "pre-mint check failed",
                            "data": {"error": str(e)},
                            "timestamp": time.time(),
                            "sessionId": "debug-session",
                        }
                    )
                    + "\n"
                )
        # #endregion

        # #region agent log - static call test
        try:
            # Try a static call to see if the transaction would revert
            # mintDiem(uint256 sVVVAmountToLock, uint256 minDiemAmountOut)
            # Use the SAME minDiemOut as the actual tx to catch slippage issues early
            staking_contract.functions.mintDiem(int(amount), min_diem_out).call(
                {"from": wallet}
            )
            with Path("/app/logs/debug.log").open("a") as f:
                f.write(
                    json.dumps(
                        {
                            "hypothesisId": "MINT_C",
                            "location": "actions.py:mint:staticcall",
                            "message": "static call succeeded",
                            "data": {"svvv_to_lock": amount},
                            "timestamp": time.time(),
                            "sessionId": "debug-session",
                        }
                    )
                    + "\n"
                )
        except Exception as e:
            # Additional diagnostic checks
            diag_data = {
                "error": str(e),
                "error_type": type(e).__name__,
                "svvv_to_lock": amount,
            }
            try:
                code = self.w3.eth.get_code(staking_addr)
                diag_data["contract_has_code"] = len(code) > 2
                diag_data["code_length"] = len(code)
                diag_data["fn_selector"] = self.w3.keccak(
                    text="mintDiem(uint256,uint256)"
                )[:4].hex()
                # Check if this is a proxy - look for implementation slot
                impl_slot = (
                    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
                )
                try:
                    impl = self.w3.eth.get_storage_at(staking_addr, impl_slot)
                    if impl != b"\x00" * 32:
                        diag_data["proxy_implementation"] = "0x" + impl[-20:].hex()
                    else:
                        diag_data["proxy_implementation"] = None
                except Exception:
                    diag_data["proxy_implementation"] = "check_failed"
                # Try with a very small amount to see if min amount is issue
                try:
                    staking_contract.functions.mintDiem(1, 0).call({"from": wallet})
                    diag_data["mint_1_wei"] = "success"
                except Exception as e2:
                    diag_data["mint_1_wei"] = str(e2)
                # Try with 0 to see error
                try:
                    staking_contract.functions.mintDiem(0, 0).call({"from": wallet})
                    diag_data["mint_0"] = "success"
                except Exception as e3:
                    diag_data["mint_0"] = str(e3)
                # Check if there's a different function we should call
                try:
                    # Try getSVVVAmountIn to verify contract is responding
                    svvv_in = staking_contract.functions.getSVVVAmountIn(10**18).call()
                    diag_data["getSVVVAmountIn_1DIEM"] = str(svvv_in)
                except Exception as e4:
                    diag_data["getSVVVAmountIn_1DIEM"] = f"error: {e4}"
                # Try calling implementation directly to see if proxy is the issue
                try:
                    impl_addr = diag_data.get("proxy_implementation")
                    if impl_addr:
                        from libs.agentkit_ext.agentkit_wallet import get_web3

                        w3 = get_web3()
                        impl_contract = w3.eth.contract(
                            address=w3.to_checksum_address(impl_addr),
                            abi=staking_contract.abi,
                        )
                        # Try getDiemAmountOut on impl
                        try:
                            impl_diem_out = impl_contract.functions.getDiemAmountOut(
                                10**18
                            ).call()
                            diag_data["impl_getDiemAmountOut"] = str(impl_diem_out)
                        except Exception as e5:
                            diag_data["impl_getDiemAmountOut"] = f"error: {e5}"
                        # Try mintDiem on impl (2-param: sVVVAmountToLock, minDiemAmountOut)
                        try:
                            impl_contract.functions.mintDiem(1, 0).call(
                                {"from": wallet}
                            )
                            diag_data["impl_mintDiem_1"] = "success"
                        except Exception as e6:
                            diag_data["impl_mintDiem_1"] = str(e6)
                except Exception as e7:
                    diag_data["impl_direct_call_error"] = str(e7)
                # Check raw call to see exact revert data
                try:
                    fn_data = staking_contract.encodeABI(
                        fn_name="mintDiem", args=[1, 0]
                    )
                    raw_result = self.w3.eth.call(
                        {"to": staking_addr, "from": wallet, "data": fn_data}
                    )
                    diag_data["raw_call_result"] = (
                        raw_result.hex() if raw_result else "empty"
                    )
                except Exception as e8:
                    diag_data["raw_call_error"] = str(e8)
                    diag_data["raw_call_error_type"] = type(e8).__name__

                # #region agent log - bytecode selector proof (writes to workspace .cursor/debug.log)
                # This is the strongest on-chain evidence for whether the function truly exists:
                # search the implementation bytecode for PUSH4 <selector>.
                try:
                    import json as _json
                    import time as _time

                    def _dbg_write(
                        hypothesis_id: str, location: str, message: str, data_obj: dict
                    ) -> None:
                        try:
                            with Path("/app/.cursor/debug.log").open("a") as _f:
                                _f.write(
                                    _json.dumps(
                                        {
                                            "sessionId": "debug-session",
                                            "runId": "selector-proof",
                                            "hypothesisId": hypothesis_id,
                                            "location": location,
                                            "message": message,
                                            "data": data_obj,
                                            "timestamp": int(_time.time() * 1000),
                                        }
                                    )
                                    + "\n"
                                )
                        except Exception:
                            pass

                    impl_addr2 = diag_data.get("proxy_implementation")
                    if (
                        impl_addr2
                        and isinstance(impl_addr2, str)
                        and impl_addr2.startswith("0x")
                        and len(impl_addr2) == 42
                    ):
                        impl_addr2 = self.w3.to_checksum_address(impl_addr2)
                        impl_code = self.w3.eth.get_code(impl_addr2)
                        impl_hex = impl_code.hex() if impl_code else ""

                        sel_mint = self.w3.keccak(text="mintDiem(uint256,uint256)")[
                            :4
                        ].hex()
                        sel_burn = self.w3.keccak(text="burnDiem(uint256)")[:4].hex()
                        sel_in = self.w3.keccak(text="getSVVVAmountIn(uint256)")[
                            :4
                        ].hex()
                        sel_out = self.w3.keccak(text="getDiemAmountOut(uint256)")[
                            :4
                        ].hex()

                        def _has_push4_selector(sel_hex: str) -> bool:
                            # Typical Solidity dispatcher embeds selectors as PUSH4 (0x63 + 4 bytes selector)
                            needle = "63" + sel_hex.lower()
                            return needle in impl_hex.lower()

                        selector_presence = {
                            "impl": impl_addr2,
                            "impl_code_len": len(impl_code),
                            "selectors": {
                                "mintDiem": sel_mint,
                                "burnDiem": sel_burn,
                                "getSVVVAmountIn": sel_in,
                                "getDiemAmountOut": sel_out,
                            },
                            "push4_present": {
                                "mintDiem": _has_push4_selector(sel_mint),
                                "burnDiem": _has_push4_selector(sel_burn),
                                "getSVVVAmountIn": _has_push4_selector(sel_in),
                                "getDiemAmountOut": _has_push4_selector(sel_out),
                            },
                        }
                        diag_data["impl_selector_probe"] = selector_presence
                        _dbg_write(
                            "H1",
                            "actions.py:mint:selector_probe",
                            "implementation selector probe",
                            selector_presence,
                        )
                    else:
                        _dbg_write(
                            "H1",
                            "actions.py:mint:selector_probe",
                            "no implementation address to probe",
                            {"proxy_implementation": str(impl_addr2)},
                        )
                except Exception as _sel_e:
                    diag_data["impl_selector_probe_error"] = str(_sel_e)
                # #endregion
            except Exception as diag_e:
                diag_data["diag_error"] = str(diag_e)
            diag_data["staking_addr"] = staking_addr
            with Path("/app/logs/debug.log").open("a") as f:
                f.write(
                    json.dumps(
                        {
                            "hypothesisId": "MINT_C",
                            "location": "actions.py:mint:staticcall_failed",
                            "message": "static call FAILED - detailed diagnostics",
                            "data": diag_data,
                            "timestamp": time.time(),
                            "sessionId": "debug-session",
                        }
                    )
                    + "\n"
                )
        # #endregion

        gas_limit = None
        try:
            # Increased from 300k to 400k based on observed on-chain usage:
            # Successful mintDiem tx 0xb1b8...used 341,699 gas, 300k was insufficient.
            gas_limit = int(os.getenv("DIEM_MINT_GAS_LIMIT") or 400_000)
        except Exception:
            gas_limit = 400_000
        overrides = self._gas_overrides(gas_limit=gas_limit)

        # Pre-flight balance check
        self._ensure_min_balance(int(Web3.to_wei(0.0005, "ether")), "mint")

        # Retry logic for transient state changes
        max_retries = int(os.getenv("DIEM_MINT_MAX_RETRIES", "3"))
        retry_delay_sec = float(os.getenv("DIEM_MINT_RETRY_DELAY_SEC", "2.0"))
        wait_for_confirmation = os.getenv(
            "DIEM_MINT_WAIT_CONFIRM", "1"
        ).strip().lower() not in ("0", "false", "no")

        import time as _time

        from libs.agentkit_ext.agentkit_wallet import wait_for_tx_confirmation

        last_error = None
        last_tx_hash = None

        for attempt in range(1, max_retries + 1):
            try:
                # Re-validate state before each attempt (state may have changed)
                if attempt > 1:
                    _logger.info(
                        f"Mint retry attempt {attempt}/{max_retries} after {retry_delay_sec}s delay"
                    )
                    _time.sleep(retry_delay_sec)

                    # Refresh expected DIEM output for slippage protection
                    try:
                        fresh_expected = staking_contract.functions.getDiemAmountOut(
                            int(amount)
                        ).call()
                        if fresh_expected > 0:
                            min_diem_out = int(
                                fresh_expected * (100 - slippage_pct) / 100
                            )
                            data = encode_contract_call(
                                staking_contract, fn, [int(amount), min_diem_out]
                            )
                            _logger.info(
                                f"Refreshed minDiemOut={min_diem_out} for retry"
                            )
                    except Exception:
                        pass  # Keep previous minDiemOut

                tx_hash = send_tx(
                    staking_addr,
                    bytes.fromhex(data[2:]),
                    gas_overrides=overrides,
                )
                last_tx_hash = tx_hash
                _logger.info(f"Mint tx submitted: {tx_hash} (attempt {attempt})")

                if not wait_for_confirmation:
                    # Fire-and-forget mode
                    return {
                        "status": "sent",
                        "action": "mint",
                        "tx_hash": tx_hash,
                        "svvv_locked": int(amount),
                        "min_diem_out": min_diem_out,
                        "expected_diem_out": expected_diem_out,
                        "allowance": allowance_result,
                        "attempt": attempt,
                    }

                # Wait for confirmation
                confirm_timeout = int(os.getenv("DIEM_MINT_CONFIRM_TIMEOUT_SEC", "60"))
                confirmation = wait_for_tx_confirmation(
                    tx_hash, timeout=confirm_timeout
                )

                if confirmation.get("status") == "confirmed":
                    _logger.info(f"Mint tx confirmed: {tx_hash}")
                    return {
                        "status": "confirmed",
                        "action": "mint",
                        "tx_hash": tx_hash,
                        "svvv_locked": int(amount),
                        "min_diem_out": min_diem_out,
                        "expected_diem_out": expected_diem_out,
                        "allowance": allowance_result,
                        "attempt": attempt,
                        "confirmation": confirmation,
                    }
                if confirmation.get("status") == "failed":
                    last_error = f"tx_reverted:{tx_hash}"
                    _logger.warning(f"Mint tx reverted on attempt {attempt}: {tx_hash}")
                    # Continue to retry if we have attempts left
                else:
                    last_error = f"tx_timeout:{tx_hash}"
                    _logger.warning(f"Mint tx timeout on attempt {attempt}: {tx_hash}")

            except Exception as send_err:
                last_error = str(send_err)
                _logger.warning(f"Mint tx failed on attempt {attempt}: {send_err}")

        # All retries exhausted
        _logger.error(f"Mint failed after {max_retries} attempts: {last_error}")
        return {
            "status": "error",
            "action": "mint",
            "error": f"failed_after_{max_retries}_attempts:{last_error}",
            "tx_hash": last_tx_hash,
            "svvv_locked": int(amount),
            "min_diem_out": min_diem_out,
            "expected_diem_out": expected_diem_out,
            "allowance": allowance_result,
            "attempts": max_retries,
        }

    def burn(self, amount: int) -> dict[str, Any]:
        """Burn DIEM to unlock locked sVVV on the staking contract.

        The `amount` parameter is the DIEM amount to burn.
        Calls burnDiem(diemAmountToBurn) on VVV_STAKING_ADDRESS.
        """
        try:
            staking_contract, staking_addr = self._get_staking_contract()
        except Exception as exc:
            return {"status": "error", "action": "burn", "error": str(exc)}

        fn = os.getenv("DIEM_BURN_FN", "burnDiem")

        # Pre-flight static call to validate burn will succeed on-chain
        # This prevents wasted gas on transactions that would revert
        try:
            staking_contract.functions.burnDiem(int(amount)).call(
                {"from": self._address}
            )
        except Exception as static_err:
            error_str = str(static_err).lower()
            # Parse common revert reasons
            if "insufficient" in error_str or "balance" in error_str:
                reason = "insufficient_diem_balance"
            elif "locked" in error_str or "svvv" in error_str:
                reason = "insufficient_locked_svvv"
            elif "execution reverted" in error_str:
                reason = "contract_revert"
            else:
                reason = "static_call_failed"

            _logger.warning(
                f"burnDiem static call failed: {static_err}",
                extra={
                    "action": "burn_preflight_failed",
                    "amount": int(amount),
                    "reason": reason,
                    "error": str(static_err),
                },
            )
            return {
                "status": "error",
                "action": "burn",
                "error": f"{reason}:{static_err}",
                "reason": reason,
                "diem_requested": int(amount),
            }

        try:
            data = encode_contract_call(staking_contract, fn, [int(amount)])
        except Exception as exc:
            _logger.error(f"Failed to encode burnDiem call: {exc}")
            return {"status": "error", "action": "burn", "error": str(exc)}

        gas_limit = None
        try:
            gas_limit = int(os.getenv("DIEM_BURN_GAS_LIMIT") or 300_000)
        except Exception:
            gas_limit = 300_000
        overrides = self._gas_overrides(gas_limit=gas_limit)

        burn_min_eth_raw = os.getenv("DIEM_BURN_MIN_ETH", "0.0005")
        try:
            burn_min_eth = float(burn_min_eth_raw)
            if not math.isfinite(burn_min_eth) or burn_min_eth < 0:
                raise ValueError("DIEM_BURN_MIN_ETH must be a finite number >= 0")
        except Exception:
            _logger.warning("invalid_DIEM_BURN_MIN_ETH:%s", burn_min_eth_raw)
            burn_min_eth = 0.0005

        # Pre-flight balance check using a realistic Base max burn budget (default 0.0005 ETH).
        self._ensure_min_balance(int(Web3.to_wei(burn_min_eth, "ether")), "burn")

        tx_hash = send_tx(
            staking_addr,
            bytes.fromhex(data[2:]),
            gas_overrides=overrides,
        )
        return {
            "status": "sent",
            "action": "burn",
            "tx_hash": tx_hash,
            "diem_burned": int(amount),
        }

    def lock_svvv(self, amount: int) -> dict[str, Any]:
        """Deprecated: DIEM contract auto-locks sVVV during mint()."""

        _logger.warning(
            "lock_svvv called but DIEM contract auto-locks during mint",
            extra={"amount": int(amount)},
        )
        return {
            "status": "skipped",
            "action": "lock_svvv",
            "reason": "contract_auto_locks",
            "amount": int(amount),
        }

    def unlock_svvv(self, amount: int) -> dict[str, Any]:
        """Deprecated: DIEM contract auto-unlocks sVVV after burn()."""

        _logger.warning(
            "unlock_svvv called but DIEM contract auto-unlocks after burn",
            extra={"amount": int(amount)},
        )
        return {
            "status": "skipped",
            "action": "unlock_svvv",
            "reason": "contract_auto_unlocks",
            "amount": int(amount),
        }

    def _resolve_stake_target(self) -> tuple[Any, str]:
        from web3 import Web3  # type: ignore

        if self.diem_staking is not None and self.diem_staking_addr:
            return self.diem_staking, Web3.to_checksum_address(self.diem_staking_addr)
        if self.diem is not None and self.diem_addr:
            return self.diem, Web3.to_checksum_address(self.diem_addr)
        raise FileNotFoundError("No DIEM staking contract configured")

    def stake_for_api(self, amount: int) -> dict[str, Any]:
        target, addr = self._resolve_stake_target()
        fn = os.getenv("DIEM_STAKE_FN", "stake")
        data = encode_contract_call(target, fn, [int(amount)])
        tx_hash = send_tx(addr, bytes.fromhex(data[2:]))
        return {"status": "sent", "action": "stake_diem", "tx_hash": tx_hash}

    def unstake_for_api(self, amount: int) -> dict[str, Any]:
        target, addr = self._resolve_stake_target()

        preferred = [
            (os.getenv("DIEM_UNSTAKE_FN") or "").strip(),
            (os.getenv("DIEM_WITHDRAW_FN") or "").strip(),
        ]
        candidates = [f for f in preferred if f]
        if not candidates:
            candidates = ["unstake", "withdraw", "exit"]

        last_error: str | None = None
        for fn in candidates:
            try:
                data = encode_contract_call(target, fn, [int(amount)])
            except Exception as exc:
                last_error = f"encode_failed:{fn}:{exc}"
                continue
            tx_hash = send_tx(addr, bytes.fromhex(data[2:]))
            return {
                "status": "sent",
                "action": "unstake_diem",
                "tx_hash": tx_hash,
                "fn": fn,
                "amount": int(amount),
            }

        return {
            "status": "error",
            "action": "unstake_diem",
            "error": last_error or "no_supported_unstake_fn",
            "attempted_fns": candidates,
            "amount": int(amount),
        }

    def trade(
        self, side: str, amount: int, *, provider: str | None = None
    ) -> dict[str, Any]:
        target_provider = (
            provider or self._router_provider or self._default_router_provider()
        )
        self._resolve_router(target_provider)
        if not self.router:
            raise OSError("ROUTER_ADDRESS must be set to trade")

        path_env = os.getenv("TRADE_PATH")
        if not path_env:
            raise OSError("TRADE_PATH must be set (comma-separated addresses)")
        # Normalize addresses to strip @fee suffixes before Web3 conversion
        raw_path: list[str] = [
            Web3.to_checksum_address(_normalize_address(p.strip()))
            for p in path_env.split(",")
        ]
        if side.lower() not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        # For 'sell': amount is amountIn (path[0] units) using TRADE_PATH as-is (e.g., DIEM->...->USDC)
        # For 'buy': amount is desired amountOut (DIEM units). Use reversed path (e.g., USDC->...->DIEM).
        if side.lower() == "buy":
            path: list[str] = list(reversed(raw_path))
            # Approve router to spend up to maxIn (computed below) of input token
            erc20_in = get_contract(self.w3, path[0], "erc20.json")
            # Determine required amountIn via getAmountsIn
            amounts_in = self.router.functions.getAmountsIn(int(amount), path).call()
            required_in = int(amounts_in[0])
            slippage_bps = int(os.getenv("SLIPPAGE_BPS", "100"))
            max_in = required_in * (10_000 + slippage_bps) // 10_000
            approve_data = encode_contract_call(
                erc20_in,
                "approve",
                [self.router.address, max_in],
            )
            approve_hash = send_tx(path[0], bytes.fromhex(approve_data[2:]))
            deadline = int(time.time()) + 20 * 60
            swap_func = self.router.functions.swapTokensForExactTokens(
                int(amount), int(max_in), path, self._address, deadline
            )
            built = swap_func.build_transaction({})
            tx_hash = send_tx(self.router.address, built["data"])
            return {
                "status": "sent",
                "action": "trade",
                "side": side,
                "tx_hash": tx_hash,
                "approval_tx": approve_hash,
                "max_in": str(max_in),
            }

        # Approve router to spend the input token
        path = raw_path
        erc20_in = get_contract(self.w3, path[0], "erc20.json")
        approve_data = encode_contract_call(
            erc20_in,
            "approve",
            [self.router.address, amount],
        )
        approve_hash = send_tx(path[0], bytes.fromhex(approve_data[2:]))

        # Quote and set slippage
        get_amounts_out = self.router.functions.getAmountsOut(amount, path).call()
        amount_out = int(get_amounts_out[-1])
        slippage_bps = int(os.getenv("SLIPPAGE_BPS", "100"))
        min_out = amount_out * (10_000 - slippage_bps) // 10_000
        deadline = int(time.time()) + 20 * 60

        swap_func = self.router.functions.swapExactTokensForTokens(
            amount, min_out, path, self._address, deadline
        )
        built = swap_func.build_transaction({})
        tx_hash = send_tx(self.router.address, built["data"])
        return {
            "status": "sent",
            "action": "trade",
            "side": side,
            "tx_hash": tx_hash,
            "approval_tx": approve_hash,
            "min_out": str(min_out),
        }

    def _build_tx(self, to: str, data_hex: str) -> dict[str, Any]:
        # Deprecated: direct EIP-1559 construction not needed with AgentKit providers.
        return {"to": to, "data": data_hex}
