# Settlement Risk Hints Implementation

## Overview

This document describes the implementation of risk hints in the settlement preview functionality, which now surfaces `slippageBps` and `poolTakeBps` metrics to provide transparency about trading risks.


## Changes Made

### 1. Enhanced Response Model

Added `poolTakeBps` field to the `DexPreviewResponse` model:
```python
class DexPreviewResponse(BaseModel):
    # ... existing fields ...
    slippageBps: int | None = None
    poolTakeBps: int | None = None  # NEW
```


### 2. Risk Metrics Calculation

#### For Exact-Out Quotes (Primary Path)
- **Slippage**: Calculated by comparing execution price vs mid-price reference
- **Pool Take**: Calculated as percentage of input amount vs input reserve
- Both metrics are computed when DEX aggregator returns a valid quote

#### For Fallback Path (Approximation)
- **Slippage**: Estimated using constant product formula impact
- **Pool Take**: Calculated same as primary path
- Marked with `approx=true` to indicate estimation


### 3. Enhanced Error Messages

When quotes exceed configured caps, detailed error messages now include:
- Actual values that triggered the rejection
- Configured cap values for comparison
- Clear indication of which limit was exceeded

Example error messages:
- `"slippage 250 bps exceeds cap of 150 bps"`
- `"input exceeds pool take cap: 300 bps > 100 bps allowed"`


## Configuration

Risk caps are controlled by environment variables:
- `RISK_MAX_SLIPPAGE_BPS`: Maximum allowed slippage (default: 150 = 1.5%)
- `RISK_MAX_POOL_TAKE_BPS`: Maximum pool take percentage (default: 100 = 1%)


## API Response Examples

### Successful Quote with Risk Hints
```json
{
    "provider": "uniswap_v2",
    "fromToken": "0xDIEM...",
    "toToken": "0xUSDC...",
    "toAsset": "USDC",
    "path": ["0xDIEM...", "0xWETH...", "0xUSDC..."],
    "amountIn": 1050000,
    "amountOut": 1000000,
    "expiresAt": 1234567890,
    "approx": false,
    "slippageBps": 45,      // 0.45% slippage
    "poolTakeBps": 25       // 0.25% of pool reserves
}
```

### Fallback Approximation
```json
{
    "provider": null,
    "fromToken": "0xDIEM...",
    "toToken": "0xUSDC...",
    "toAsset": "USDC",
    "path": ["0xDIEM...", "0xUSDC..."],
    "amountIn": 1050000,
    "amountOut": 1000000,
    "expiresAt": 1234567890,
    "approx": true,         // Indicates estimation
    "slippageBps": 50,      // Estimated based on pool impact
    "poolTakeBps": 30       // Actual calculation
}
```


## Testing

Test coverage includes:
1. **Normal operation**: Verify risk hints are calculated and returned
2. **Fallback path**: Ensure approximations still provide risk metrics
3. **Slippage cap enforcement**: Verify detailed error messages
4. **Pool take cap enforcement**: Verify rejection with actual values
5. **Edge cases**: Missing reserves, decimal handling, etc.

Run tests with:
```bash
uv run pytest tests/test_settlement_preview_risk_hints.py -v
```


## UI Integration

The frontend can use these risk hints to:
1. Display warning indicators when slippage/pool impact is high
2. Show exact risk percentages to users before confirming trades
3. Provide clear error messages when quotes are rejected
4. Differentiate between exact quotes and approximations


## Future Enhancements

Potential improvements for future iterations:
1. Add MEV risk indicators
2. Include gas cost impact on effective price
3. Provide confidence intervals for approximations
4. Support for multi-hop slippage aggregation
