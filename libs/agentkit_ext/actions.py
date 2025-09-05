from __future__ import annotations

import os
import time
from typing import Any, Dict, List

try:
    from web3 import Web3
except Exception:  # noqa: BLE001
    # Minimal shim to allow import-time success when web3 isn't installed
    class Web3:  # type: ignore
        @staticmethod
        def to_checksum_address(addr: str) -> str:
            return addr

from .web3_utils import get_contract, get_web3
from .agentkit_wallet import send_tx, get_address


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
            raise EnvironmentError("VVV_TOKEN_ADDRESS and VVV_STAKING_ADDRESS must be set")
        self.w3 = get_web3()
        self.erc20 = get_contract(self.w3, self.token_addr, "erc20.json")
        # Staking ABI must be provided by the project in abi/staking.json
        self.staking = get_contract(self.w3, self.staking_addr, "staking.json")

    def approve(self, amount: int) -> Dict[str, Any]:
        data = self.erc20.encode_abi(
            fn_name="approve",
            args=[Web3.to_checksum_address(self.staking_addr), amount],
        )
        tx_hash = send_tx(self.token_addr, bytes.fromhex(data[2:]))
        return {"status": "sent", "action": "approve", "tx_hash": tx_hash}

    def stake(self, amount: int) -> Dict[str, Any]:
        data = self.staking.encode_abi(
            fn_name=os.getenv("VVV_STAKE_FN", "stake"), args=[amount]
        )
        tx_hash = send_tx(self.staking_addr, bytes.fromhex(data[2:]))
        return {"status": "sent", "action": "stake", "tx_hash": tx_hash}

    def claim(self) -> Dict[str, Any]:
        data = self.staking.encode_abi(
            fn_name=os.getenv("VVV_CLAIM_FN", "claim"), args=[]
        )
        tx_hash = send_tx(self.staking_addr, bytes.fromhex(data[2:]))
        return {"status": "sent", "action": "claim", "tx_hash": tx_hash}

    def unstake(self, amount: int) -> Dict[str, Any]:
        fn = os.getenv("VVV_UNSTAKE_FN", "unstake")
        data = self.staking.encode_abi(fn_name=fn, args=[amount])
        tx_hash = send_tx(self.staking_addr, bytes.fromhex(data[2:]))
        return {"status": "sent", "action": "unstake", "tx_hash": tx_hash}

    def _build_tx(self, to: str, data_hex: str) -> Dict[str, Any]:
        # Deprecated path; preserved for compatibility if needed
        return {"to": to, "data": data_hex}


class DIEMACTIONS:
    """DIEM token mint/burn and DEX trade using Web3.

    Env required:
      - BASE_RPC_URL, ETH_PRIVATE_KEY
      - DIEM_TOKEN_ADDRESS (for mint/burn if direct), or protocol router address
      - ROUTER_ADDRESS (for trades), abi/uniswap_v2_router.json
      - TRADE_PATH (comma-separated addresses, e.g., DIEM,USDC)
      - SLIPPAGE_BPS (default 100 = 1%)
      - ABI files: abi/diem.json (protocol), if mint/burn exist on DIEM contract
    """

    def __init__(self) -> None:
        self.w3 = get_web3()
        self._address = Web3.to_checksum_address(get_address())
        self.diem_addr = os.getenv("DIEM_TOKEN_ADDRESS")
        self.router_addr = os.getenv("ROUTER_ADDRESS")
        if not self.diem_addr:
            raise EnvironmentError("DIEM_TOKEN_ADDRESS must be set")
        # DIEM ABI for mint/burn, if applicable (project-specific)
        try:
            self.diem = get_contract(self.w3, self.diem_addr, "diem.json")
        except FileNotFoundError:
            self.diem = None  # optional; raise at call time if used
        # Router is optional unless trade() is used
        self.router = get_contract(self.w3, self.router_addr, "uniswap_v2_router.json") if self.router_addr else None

    def mint(self, amount: int) -> Dict[str, Any]:
        if not self.diem:
            raise FileNotFoundError("abi/diem.json is required to call mint()")
        fn = os.getenv("DIEM_MINT_FN", "mint")
        data = self.diem.encode_abi(fn_name=fn, args=[amount])
        tx_hash = send_tx(self.diem_addr, bytes.fromhex(data[2:]))
        return {"status": "sent", "action": "mint", "tx_hash": tx_hash}

    def burn(self, amount: int) -> Dict[str, Any]:
        if not self.diem:
            raise FileNotFoundError("abi/diem.json is required to call burn()")
        fn = os.getenv("DIEM_BURN_FN", "burn")
        data = self.diem.encode_abi(fn_name=fn, args=[amount])
        tx_hash = send_tx(self.diem_addr, bytes.fromhex(data[2:]))
        return {"status": "sent", "action": "burn", "tx_hash": tx_hash}

    def trade(self, side: str, amount: int) -> Dict[str, Any]:
        if not self.router:
            raise EnvironmentError("ROUTER_ADDRESS must be set to trade")

        path_env = os.getenv("TRADE_PATH")
        if not path_env:
            raise EnvironmentError("TRADE_PATH must be set (comma-separated addresses)")
        path: List[str] = [Web3.to_checksum_address(p.strip()) for p in path_env.split(",")]
        if side.lower() not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        # For 'sell': amount is amountIn (path[0] units)
        # For 'buy': amount is desired amountOut (path[-1] units)
        if side.lower() == "buy":
            # Approve router to spend up to maxIn (computed below) of input token
            erc20_in = get_contract(self.w3, path[0], "erc20.json")
            # Determine required amountIn via getAmountsIn
            amounts_in = self.router.functions.getAmountsIn(int(amount), path).call()
            required_in = int(amounts_in[0])
            slippage_bps = int(os.getenv("SLIPPAGE_BPS", "100"))
            max_in = required_in * (10_000 + slippage_bps) // 10_000
            approve_data = erc20_in.encode_abi(
                fn_name="approve", args=[self.router.address, max_in]
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
        erc20_in = get_contract(self.w3, path[0], "erc20.json")
        approve_data = erc20_in.encode_abi(
            fn_name="approve", args=[self.router.address, amount]
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

    def _build_tx(self, to: str, data_hex: str) -> Dict[str, Any]:
        # Deprecated: direct EIP-1559 construction not needed with AgentKit providers.
        return {"to": to, "data": data_hex}
