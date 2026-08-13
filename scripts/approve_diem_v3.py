import os
from pathlib import Path

from web3 import Web3

# --- CONFIG (from your env/logs) ---
BASE_RPC_URL = "https://mainnet.base.org"
CHAIN_ID = 8453
DIEM_TOKEN_ADDRESS = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
UNISWAP_V3_ROUTER_ADDRESS = "0x2626664c2603336E57B271c5C0b26F421741e481"
ORCHESTRATOR_WALLET = os.environ.get("TREASURY_ADDRESS") or os.environ.get(
    "ORCHESTRATOR_WALLET"
)
# Ensure repository root is on sys.path for auxiliary imports (mirrors CLI entrypoint).
try:
    from apps._path import REPO_ROOT
except Exception:  # pragma: no cover - fallback for direct module execution
    REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_runtime_env() -> None:
    """Best-effort loading of repo-level dotenv files for API runtime.

    Replit Deployments and bare uvicorn launches do not automatically populate os.environ
    with values from .env/.env.docker, so we mirror the CLI bootstrap logic here.
    """

    if os.getenv("DISABLE_RUNTIME_DOTENV"):
        return

    docker_env = REPO_ROOT / ".env.docker"
    local_env = REPO_ROOT / "docker" / ".env.local"

    try:
        from libs.env import load_dotenv_if_present  # type: ignore
    except Exception:
        try:
            from dotenv import load_dotenv
        except Exception:
            return

        load_dotenv(dotenv_path=str(REPO_ROOT / ".env"), override=False)
        if docker_env.exists():
            load_dotenv(dotenv_path=str(docker_env), override=True)
        if local_env.exists():
            load_dotenv(dotenv_path=str(local_env), override=True)
        return

    load_dotenv_if_present(path=str(REPO_ROOT / ".env"), override=False)
    if docker_env.exists():
        load_dotenv_if_present(path=str(docker_env), override=True)
    if local_env.exists():
        load_dotenv_if_present(path=str(local_env), override=True)


_load_runtime_env()
# Load private key for the ORCHESTRATOR_WALLET from env
ETH_PRIVATE_KEY = os.environ.get("ETH_PRIVATE_KEY")
if not ETH_PRIVATE_KEY:
    raise SystemExit("ETH_PRIVATE_KEY env var is not set")

# Minimal ERC-20 ABI for approve
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    }
]


def main() -> None:
    w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL))
    if not w3.is_connected():
        raise SystemExit("Failed to connect to Base RPC")

    acct = w3.eth.account.from_key(ETH_PRIVATE_KEY)
    if acct.address.lower() != ORCHESTRATOR_WALLET.lower():
        print(
            f"WARNING: ETH_PRIVATE_KEY address {acct.address} != orchestrator {ORCHESTRATOR_WALLET}"
        )

    diem = w3.eth.contract(
        address=Web3.to_checksum_address(DIEM_TOKEN_ADDRESS),
        abi=ERC20_ABI,
    )

    router = Web3.to_checksum_address(UNISWAP_V3_ROUTER_ADDRESS)
    nonce = w3.eth.get_transaction_count(acct.address)

    tx = diem.functions.approve(router, 2**256 - 1).build_transaction(
        {
            "from": acct.address,
            "nonce": nonce,
            "chainId": CHAIN_ID,
            # You can tweak gas / gasPrice as needed
            "gas": 100_000,
            "maxFeePerGas": w3.to_wei("1", "gwei"),
            "maxPriorityFeePerGas": w3.to_wei("0.1", "gwei"),
        }
    )

    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"Sent approve tx: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"Tx mined with status={receipt.status}")


if __name__ == "__main__":
    main()
