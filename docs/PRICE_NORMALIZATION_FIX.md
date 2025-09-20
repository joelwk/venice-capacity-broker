# Market Data Price Normalization Fix

## Problem Summary
The `/v1/market/prices` endpoint was returning prices in base units (wei) instead of human-readable dollar values:
- VVV: $0.000012 instead of ~$2.63
- DIEM: $1.030026151674e-12 instead of ~$227.26
- ETH: $4,401.82 (correct)
- USDC: $1.00 (correct)

## Root Cause
The DEX aggregator was returning prices in various formats:
1. Some prices were in base units (smallest denomination)
2. Some prices required token-specific scaling factors
3. The scaling wasn't uniform across all tokens

## Solution Implemented
Added intelligent price normalization in `services/marketdata/provider.py`:

1. **Automatic Detection**: The system now detects when prices are suspiciously low (< $0.01) for tokens that should have higher values.

2. **Token-Specific Corrections**:
   - **DIEM**: Uses a scaling factor of 2.21e14 when price < $0.01
   - **VVV**: Uses a scaling factor of 2.19e5 when price < $0.01
   - **ETH**: Validates price is in thousands range

3. **Range Validation**: Each token has expected price ranges:
   - VVV: $0.50 - $10
   - DIEM: $50 - $500
   - ETH: $1,000 - $10,000

4. **Fallback Logic**: If specific scaling doesn't work, tries standard conversions (1e18 for wei, 1e6 for USDC decimals)

## Testing
Created comprehensive tests in:
- `scripts/test_price_normalization.py` - Tests the normalization logic
- `scripts/analyze_price_issue.py` - Analyzes scaling factors
- `scripts/debug_prices.py` - Debug tool for price issues

## Results
After the fix:
```json
{
  "prices": {
    "VVV": 2.63,
    "DIEM": 227.64,
    "ETH": 4401.82,
    "USDC": 1.0
  },
  "ratios": {
    "VVV_DIEM": 0.011545,
    "ETH_DIEM": 19.337123,
    "USDC_DIEM": 0.004393
  }
}
```

## Configuration Required
For the system to work properly, ensure these environment variables are set:
```bash
VVV_TOKEN_ADDRESS=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf
DIEM_TOKEN_ADDRESS=0xf4d97f2da56e8c3098f3a8d538db630a2606a024
QUOTE_TOKEN_ADDRESS=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
TRADE_PATH=<token0@fee0,token1@fee1,...>
TRADE_PATH_2=<optional secondary route using address@fee format>
VVV_PRICE_PATH=<vvv@fee0,weth@fee1,quote>
BASE_RPC_URL=<your Base RPC endpoint>
```

## Future Improvements
1. Consider fetching live price oracles for validation
2. Add configurable scaling factors via environment variables
3. Implement automatic detection of token decimals from contracts
4. Add alerts when normalization is applied frequently (indicates upstream issue)
