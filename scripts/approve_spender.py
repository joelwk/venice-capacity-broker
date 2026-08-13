import argparse
import sys
from pathlib import Path

# Add project root to path
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

from web3 import Web3  # noqa: E402

from libs.agentkit_ext.web3_utils import (  # noqa: E402
    build_eip1559_tx,
    encode_contract_call,
    get_account_wallet,
    get_web3,
    send_contract_tx,
)
from libs.env import load_dotenv_if_present  # noqa: E402

# ERC20 ABI for approve/allowance
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
]


def main():
    parser = argparse.ArgumentParser(
        description="Approve a spender for an ERC20 token."
    )
    parser.add_argument("--token", required=True, help="Token address")
    parser.add_argument(
        "--spender", required=True, help="Spender address (e.g. Router)"
    )
    parser.add_argument(
        "--amount",
        default=None,
        help="Amount to approve (wei). Defaults to max uint256.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Check allowance only.")

    args = parser.parse_args()

    load_dotenv_if_present()

    # Extra env loading (Docker/Local override)
    repo_root = Path(__file__).resolve().parents[1]
    docker_env = repo_root / ".env.docker"
    local_env = repo_root / "docker" / ".env.local"

    if docker_env.exists():
        load_dotenv_if_present(path=str(docker_env), override=True)
    if local_env.exists():
        load_dotenv_if_present(path=str(local_env), override=True)

    w3 = get_web3()
    wallet = get_account_wallet()
    owner = wallet.address

    token_addr = Web3.to_checksum_address(args.token)
    spender_addr = Web3.to_checksum_address(args.spender)

    print(f"Owner:   {owner}")
    print(f"Token:   {token_addr}")
    print(f"Spender: {spender_addr}")

    token = w3.eth.contract(address=token_addr, abi=ERC20_ABI)

    try:
        symbol = token.functions.symbol().call()
    except Exception as exc:
        print(f"Symbol lookup failed: {exc}")
        symbol = "???"

    try:
        decimals = token.functions.decimals().call()
    except Exception as exc:
        print(f"Decimals lookup failed, defaulting to 18: {exc}")
        decimals = 18

    print(f"Asset:   {symbol} ({decimals} decimals)")

    current_allowance = token.functions.allowance(owner, spender_addr).call()
    print(f"Current: {current_allowance} ({current_allowance / 10**decimals:,.4f})")

    target_amount = int(args.amount) if args.amount else 2**256 - 1

    if current_allowance >= target_amount:
        print("✅ Allowance sufficient.")
        return

    if args.dry_run:
        print(f"Would approve {target_amount}. Pass without --dry-run to execute.")
        return

    # Check ETH balance for gas
    eth_balance = w3.eth.get_balance(owner)
    print(f"ETH Balance: {eth_balance / 10**18:.6f} ETH")

    if eth_balance == 0:
        print("❌ ERROR: Wallet has 0 ETH. Cannot pay for gas fees.")
        print("   Please fund your wallet with ETH (or Base native token) to proceed.")
        return

    # Check nonce for pending transactions
    try:
        pending_nonce = w3.eth.get_transaction_count(owner, "pending")
        latest_nonce = w3.eth.get_transaction_count(owner, "latest")
        if pending_nonce > latest_nonce:
            print(
                f"⚠️  WARNING: {pending_nonce - latest_nonce} pending transaction(s) detected."
            )
            print(f"   Latest nonce: {latest_nonce}, Pending nonce: {pending_nonce}")
    except Exception:
        pass  # Nonce check is optional

    print(f"Approving {target_amount}...")

    # Build TX
    # Using low-level build to control gas/nonce
    approve_data = encode_contract_call(token, "approve", [spender_addr, target_amount])

    # Try to estimate gas first to catch balance issues early
    # Use EIP-1559 format to match build_eip1559_tx
    try:
        # Get base fee and priority fee for proper EIP-1559 estimation
        try:
            latest_block = w3.eth.get_block("latest")
            base_fee = getattr(latest_block, "baseFeePerGas", None)
            if base_fee is None and isinstance(latest_block, dict):
                base_fee = latest_block.get("baseFeePerGas")
        except Exception:
            base_fee = None

        priority_fee_wei = Web3.to_wei(1, "gwei")  # Default 1 gwei
        if base_fee is not None:
            max_fee = int(base_fee * 2) + priority_fee_wei
        else:
            # Fallback to legacy gas price if base fee unavailable
            try:
                gas_price = w3.eth.gas_price
                max_fee = gas_price
            except Exception:
                max_fee = Web3.to_wei(2, "gwei")  # Conservative fallback

        test_tx = {
            "from": owner,
            "to": token_addr,
            "data": bytes.fromhex(approve_data[2:]),
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee_wei,
        }
        estimated_gas = w3.eth.estimate_gas(test_tx)
        print(f"Estimated gas: {estimated_gas:,}")

        # Calculate estimated cost
        estimated_cost = estimated_gas * max_fee
        print(
            f"Estimated cost: {estimated_cost / 10**18:.6f} ETH (max fee: {max_fee / 10**9:.2f} gwei)"
        )

        if eth_balance < estimated_cost * 1.2:  # 20% buffer
            print("⚠️  WARNING: Balance may be insufficient.")
            print(f"   Balance: {eth_balance / 10**18:.6f} ETH")
            print(f"   Estimated cost: {estimated_cost / 10**18:.6f} ETH")
            print(f"   Recommended: {estimated_cost * 1.2 / 10**18:.6f} ETH")
        else:
            print("✅ Balance sufficient for transaction")
    except Exception as e:
        error_msg = str(e)
        error_lower = error_msg.lower()
        if (
            "gas required exceeds allowance" in error_lower
            or "insufficient funds" in error_lower
        ):
            print("❌ ERROR: Transaction estimation failed.")
            print(f"   Error: {error_msg}")
            print(f"   Current balance: {eth_balance / 10**18:.6f} ETH")
            print("   This may indicate:")
            print("   - Insufficient ETH for gas")
            print("   - Token contract issue")
            print("   - RPC connection problem")
            print("   Attempting transaction anyway (may provide more details)...")
        else:
            # If it's a different error, continue and let build_eip1559_tx handle it
            print(f"⚠️  Gas estimation warning: {error_msg}")
            print("   Continuing with transaction build...")

    # Build the transaction
    try:
        tx = build_eip1559_tx(
            w3, owner, to=token_addr, data=bytes.fromhex(approve_data[2:])
        )
        print("Transaction built successfully")
        print(f"  Gas limit: {tx.get('gas', 'N/A'):,}")
        print(f"  Max fee per gas: {tx.get('maxFeePerGas', 0) / 10**9:.2f} gwei")
        print(f"  Priority fee: {tx.get('maxPriorityFeePerGas', 0) / 10**9:.2f} gwei")
        total_cost = tx.get("gas", 0) * tx.get("maxFeePerGas", 0)
        if total_cost > 0:
            print(f"  Total estimated cost: {total_cost / 10**18:.6f} ETH")
    except Exception as e:
        error_msg = str(e)
        error_lower = error_msg.lower()

        # Check for specific error patterns
        if "gas required exceeds allowance" in error_lower:
            print("❌ ERROR: Gas estimation failed - 'gas required exceeds allowance'")
            print("   This usually means one of:")
            print("   1. The RPC node cannot simulate the transaction")
            print("   2. There's a nonce mismatch (try checking pending transactions)")
            print("   3. The token contract has restrictions on approvals")
            print(
                f"   4. Insufficient ETH (though you have {eth_balance / 10**18:.6f} ETH)"
            )
            print("   ")
            print("   Attempting manual transaction build...")

            # Try manual build with explicit values
            try:
                latest_block = w3.eth.get_block("latest")
                base_fee = (
                    getattr(latest_block, "baseFeePerGas", None)
                    or latest_block.get("baseFeePerGas")
                    if isinstance(latest_block, dict)
                    else None
                )
                if base_fee is None:
                    # Fallback to gas_price
                    gas_price = w3.eth.gas_price
                    max_fee = gas_price
                    priority_fee = Web3.to_wei(0.1, "gwei")
                else:
                    priority_fee = Web3.to_wei(1, "gwei")
                    max_fee = int(base_fee * 2) + priority_fee

                nonce = w3.eth.get_transaction_count(owner)
                tx = {
                    "chainId": w3.eth.chain_id,
                    "from": owner,
                    "to": token_addr,
                    "nonce": nonce,
                    "data": bytes.fromhex(approve_data[2:]),
                    "value": 0,
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": priority_fee,
                    "gas": 100000,  # Conservative estimate for approve
                }
                print(f"   Built manual transaction with gas limit: {tx['gas']:,}")
            except Exception as e2:
                print(f"   Manual build also failed: {e2}")
                print(f"   Original error: {error_msg}")
                return
        elif "insufficient funds" in error_lower:
            print("❌ ERROR: Insufficient ETH balance to pay for gas.")
            print(f"   Error: {error_msg}")
            print(f"   Current balance: {eth_balance / 10**18:.6f} ETH")
            print("   Please fund your wallet with more ETH to cover gas fees.")
            return
        else:
            print(f"❌ ERROR: Failed to build transaction: {error_msg}")
            raise

    print("Sending transaction...")
    tx_hash = send_contract_tx(w3, wallet, tx)
    print(f"Tx Hash: {tx_hash}")

    print("Waiting for confirmation...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    if receipt.status == 1:
        print("✅ Approval successful!")
        new_allowance = token.functions.allowance(owner, spender_addr).call()
        print(f"New Allowance: {new_allowance}")
    else:
        print("❌ Transaction failed/reverted.")


if __name__ == "__main__":
    main()
