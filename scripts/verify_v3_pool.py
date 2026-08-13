"""Script to verify VVV/USDC V3 pool state on-chain."""

import os
import sys
from decimal import Decimal

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web3 import Web3

from libs.agentkit_ext.web3_utils import get_web3


def verify_v3_pool(pool_address: str):
    """Query V3 pool state to verify liquidity and tick ranges."""
    w3 = get_web3()

    pool_abi = [
        {
            "constant": True,
            "inputs": [],
            "name": "slot0",
            "outputs": [
                {"name": "sqrtPriceX96", "type": "uint160"},
                {"name": "tick", "type": "int24"},
                {"name": "observationIndex", "type": "uint16"},
                {"name": "observationCardinality", "type": "uint16"},
                {"name": "observationCardinalityNext", "type": "uint16"},
                {"name": "feeProtocol", "type": "uint8"},
                {"name": "unlocked", "type": "bool"},
            ],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "constant": True,
            "inputs": [],
            "name": "liquidity",
            "outputs": [{"name": "", "type": "uint128"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "constant": True,
            "inputs": [],
            "name": "token0",
            "outputs": [{"name": "", "type": "address"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "constant": True,
            "inputs": [],
            "name": "token1",
            "outputs": [{"name": "", "type": "address"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "constant": True,
            "inputs": [{"name": "wordPosition", "type": "int16"}],
            "name": "tickBitmap",
            "outputs": [{"name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "constant": True,
            "inputs": [],
            "name": "fee",
            "outputs": [{"name": "", "type": "uint24"}],
            "stateMutability": "view",
            "type": "function",
        },
    ]

    pool = w3.eth.contract(address=Web3.to_checksum_address(pool_address), abi=pool_abi)

    print(f"Querying V3 pool: {pool_address}")
    print("-" * 60)

    # Get slot0
    slot0 = pool.functions.slot0().call()
    sqrt_price_x96 = slot0[0]
    current_tick = slot0[1]
    fee_protocol = slot0[5]
    unlocked = slot0[6]

    # Get liquidity
    liquidity = pool.functions.liquidity().call()

    # Get tokens
    token0 = pool.functions.token0().call()
    token1 = pool.functions.token1().call()
    fee = pool.functions.fee().call()

    # Calculate price from sqrtPriceX96
    Q96 = 2**96
    sqrt_price = Decimal(sqrt_price_x96) / Decimal(Q96)
    price = sqrt_price * sqrt_price

    print(f"Token0: {token0}")
    print(f"Token1: {token1}")
    print(f"Fee: {fee} ({fee / 10000:.2f}%)")
    print(f"Current Tick: {current_tick}")
    print(f"sqrtPriceX96: {sqrt_price_x96}")
    print(f"Price (token1/token0): {price:.10f}")
    print(f"Liquidity: {liquidity}")
    print(f"Unlocked: {unlocked}")
    print(f"Fee Protocol: {fee_protocol}")

    # Check tick bitmap around current tick
    tick_spacing = 60  # Standard for 0.3% fee tier
    word_position = current_tick // (tick_spacing * 256)

    print("\nTick Bitmap Analysis:")
    print(f"Tick Spacing: {tick_spacing}")
    print(f"Word Position: {word_position}")

    # Check a few words around current position
    for offset in [-1, 0, 1]:
        word_pos = word_position + offset
        try:
            bitmap = pool.functions.tickBitmap(word_pos).call()
            if bitmap > 0:
                print(f"  Word {word_pos}: Has liquidity (bitmap={hex(bitmap)})")
            else:
                print(f"  Word {word_pos}: No liquidity")
        except Exception as e:
            print(f"  Word {word_pos}: Error - {e}")

    return {
        "pool_address": pool_address,
        "token0": token0,
        "token1": token1,
        "fee": fee,
        "current_tick": current_tick,
        "sqrt_price_x96": sqrt_price_x96,
        "price": float(price),
        "liquidity": liquidity,
        "unlocked": unlocked,
    }


if __name__ == "__main__":
    pool_addr = os.getenv(
        "VVV_USDC_POOL_V3_ADDRESS", "0x67a11022b7b6ed66f81233f6c8ed6e48f7826530"
    )

    if not pool_addr:
        print("Error: VVV_USDC_POOL_V3_ADDRESS not set")
        sys.exit(1)

    result = verify_v3_pool(pool_addr)
    print("\n" + "=" * 60)
    print("Pool verification complete")
