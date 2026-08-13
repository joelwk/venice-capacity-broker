import os

from web3 import Web3

# --- CONFIG (from your env/logs) ---
BASE_RPC_URL = "https://mainnet.base.org"
DIEM_TOKEN_ADDRESS = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
UNISWAP_V2_ROUTER_ADDRESS = "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24"
UNISWAP_V3_ROUTER_ADDRESS = "0x2626664c2603336e57b271c5c0b26f421741e481"
ORCHESTRATOR_WALLET = os.environ.get("TREASURY_ADDRESS") or os.environ.get(
    "ORCHESTRATOR_WALLET"
)
DIEM_DECIMALS = 18  # adjust if DIEM uses a different decimals value

# --- Minimal ERC-20 ABI for balance & allowance ---
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]


def main() -> None:
    w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL))
    if not w3.is_connected():
        raise SystemExit("Failed to connect to Base RPC")

    diem = w3.eth.contract(
        address=Web3.to_checksum_address(DIEM_TOKEN_ADDRESS),
        abi=ERC20_ABI,
    )

    if not ORCHESTRATOR_WALLET:
        raise SystemExit("Set TREASURY_ADDRESS or ORCHESTRATOR_WALLET")

    wallet = Web3.to_checksum_address(ORCHESTRATOR_WALLET)
    v2_router = Web3.to_checksum_address(UNISWAP_V2_ROUTER_ADDRESS)
    v3_router = Web3.to_checksum_address(UNISWAP_V3_ROUTER_ADDRESS)

    balance = diem.functions.balanceOf(wallet).call()
    allowance_v2 = diem.functions.allowance(wallet, v2_router).call()
    allowance_v3 = diem.functions.allowance(wallet, v3_router).call()

    print(f"DIEM balance (raw): {balance}")
    print(f"DIEM balance (tokens): {balance / (10**DIEM_DECIMALS):.18f}")

    print("\n--- Allowances ---")
    print(f"V2 router {v2_router}:")
    print(f"  raw:    {allowance_v2}")
    print(f"  tokens: {allowance_v2 / (10**DIEM_DECIMALS):.18f}")

    print(f"\nV3 router {v3_router}:")
    print(f"  raw:    {allowance_v3}")
    print(f"  tokens: {allowance_v3 / (10**DIEM_DECIMALS):.18f}")

    def note(label: str, value: int) -> None:
        if value == 0:
            print(
                f"  [{label}] WARNING: allowance is 0; router cannot pull DIEM (TRANSFER_FROM_FAILED is expected)."
            )
        elif value < 10**15:
            print(
                f"  [{label}] WARNING: allowance is very small; may be below intended trade size."
            )
        else:
            print(f"  [{label}] OK: allowance is non-zero.")

    print("\nNotes:")
    note("V2", allowance_v2)
    note("V3", allowance_v3)


if __name__ == "__main__":
    main()
