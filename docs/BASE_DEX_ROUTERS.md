# Base DEX Router Addresses

## Verified Router Addresses for Base Mainnet (Chain ID: 8453)

### Aerodrome Router
- **Current Production Address**: `0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43`
- **Alternate Address (may be outdated)**: `0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5`

The correct Aerodrome router address on Base is `0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43`. This is the active router that handles multi-hop swaps and has the most liquidity.

### Uniswap V2 Router
- **Address**: `0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24`
- **Type**: Router02 (supports multi-hop)

### Important Token Addresses on Base
- **WETH**: `0x4200000000000000000000000000000000000006`
- **USDC**: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
- **DIEM**: `0xF4d97F2da56e8c3098f3a8D538DB630A2606a024`
- **VVV**: `0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf`

## Environment Configuration

For proper token pricing on Base, your `.env` should include:

```bash
# Base RPC endpoints
RPC_URL=https://base.drpc.org
BASE_RPC_URL=https://mainnet.base.org

# Etherscan V2 API
ETHERSCAN_CHAIN_ID=8453
ETHERSCAN_API_URL=https://api.etherscan.io/v2/api

# Quote and bridge tokens
QUOTE_TOKEN_ADDRESS=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913    # USDC
DEX_BRIDGE_TOKEN_ADDRESS=0x4200000000000000000000000000000000000006  # WETH

# DEX routers
DEX_PROVIDERS=uniswap_v2,aerodrome
UNISWAP_V2_ROUTER_ADDRESS=0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24
AERODROME_ROUTER_ADDRESS=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43  # (CORRECT ADDRESS)
AERODROME_STABLE=false  # Most pairs are volatile; code tries both

# Token addresses for tracking
VVV_TOKEN_ADDRESS=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf
DIEM_TOKEN_ADDRESS=0xF4d97F2da56e8c3098f3a8D538DB630A2606a024

# Token watcher settings
TOKEN_WATCH_ENABLE_HOLDERS=1
TOKEN_WATCH_MAX_EVENTS=1000  # Increase from default 200 for accurate counts

# Pricing path for DIEM (used by MarketDataProvider.prices):
# Set TRADE_PATH so the first token is DIEM and the second is USDC to yield a price in USDC per DIEM.
# Example:
TRADE_PATH=0xF4d97F2da56e8c3098f3a8D538DB630A2606a024,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
```

## Troubleshooting DIEM Pricing

DIEM has liquidity primarily in the VVV/DIEM pool on Aerodrome. The pricing path is:
1. DIEM -> VVV (via Aerodrome VVV/DIEM pool)
2. VVV -> WETH (via Uniswap V2 or Aerodrome)
3. WETH -> USDC (via multiple pools)

With the corrected Aerodrome router address and multi-hop support enabled, DIEM should price correctly through this path.


## Router Capabilities

- Uniswap V2: Supports exact-in (`getAmountsOut` + `swapExactTokensForTokens`) and exact-out (`getAmountsIn` + `swapTokensForExactTokens`). Includes fee-on-transfer fallback via `swapExactTokensForTokensSupportingFeeOnTransferTokens`.
- Aerodrome: Supports exact-in via `getAmountsOut` and `swapExactTokensForTokensSimple` with `stable`/`volatile` flag. Exact-out is not supported by the ABI bundled in this repo (no `getAmountsIn`). The aggregator skips Aerodrome for buy-path exact-out.

## Trading Behavior Notes

- Fee-on-transfer (FOT): Uniswap V2 provider automatically falls back to `swapExactTokensForTokensSupportingFeeOnTransferTokens` when standard swaps revert due to FOT behavior.
- Buy path (exact-out): Use Uniswap V2 for exact-out. Configure slippage via `SLIPPAGE_BPS`.
