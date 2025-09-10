• Your diagnosis is correct: pricing hinges on env config, and when routers or token addresses are missing the provider returns `1.0`.

This is exactly how `services/marketdata/provider.py` is written.  

• Drop the Etherscan fallback advice in docs.

The code already has optional AMM/mid‑price helpers that use cached reserves via `services/marketdata/etherscan_verify.py`.  

• The buy page already calls `/v1/market/prices`, and the Broker exposes it.

`apps/control-plane/buy.html:119` fetches `/v1/market/prices?symbols=VVV,DIEM,ETH,USDC`, and `apps/broker-api/app.py:1463` forwards to `MarketDataProvider.prices()` and returns `{ symbols, prices, ratios }`.  

---

# What’s right vs. what to change

• Correct: `MarketDataProvider.prices()` and env dependencies.

DIEM is priced via `TRADE_PATH`, VVV via `[VVV_TOKEN_ADDRESS, QUOTE_TOKEN_ADDRESS]`, ETH via `WETH->QUOTE`.  
If any required env is missing, the code returns `1.0`.  

• Correct: DEX providers come from env.

Routers must be set for UniswapV2 and Aerodrome or quotes will fail and collapse to `1.0`.  

• Correct: Broker API serves `/v1/market/prices` and the buy page uses it.

The endpoint returns `{ symbols, prices, ratios }`.  

• Remove Etherscan fallback advice in docs (no code change).

The provider’s Etherscan helpers stay as optional fallbacks, but operators don’t need manual steps around Etherscan.  

---

# Updated implementation plan (Broker)

## 0) Preconditions

• Broker runs via Uvicorn (see `.replit` and `apps/broker-api/app.py`).  

## 1) Configure on‑chain env (no code changes)

Set these to match Base mainnet.  

DEX & network

• `DEX_PROVIDERS=uniswap_v2,aerodrome`  
• `UNISWAP_V2_ROUTER_ADDRESS=0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24`  
• `AERODROME_ROUTER_ADDRESS=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43`  
• `AERODROME_STABLE=false`  
• `BASE_CHAIN_ID=8453`  

Tokens & quote

• `VVV_TOKEN_ADDRESS=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf`  
• `DIEM_TOKEN_ADDRESS=0xf4d97f2da56e8c3098f3a8d538db630a2606a024`  
• `QUOTE_TOKEN_ADDRESS=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`  

DIEM pricing path

• `TRADE_PATH=<DIEM>,<WETH>,<USDC>`  
Default should be DIEM → WETH → USDC on Base.  

## 2) Verify env quickly

• Start API and open `/v1/env` or the Admin Env card.  
Expected: routers configured, tokens present, and a non‑empty `trade_path`.  

## 3) Functional checks

Backend price sanity

• `uv run python apps/cli/main.py quotes:preview --units 1.0`  
Shows reserve caps and slippage across the configured path.  

Programmatic check

• `from services.marketdata.provider import MarketDataProvider; print(MarketDataProvider().prices(["VVV","DIEM","ETH","USDC"]))`  
Expect real values for VVV/DIEM/ETH with env set.  

## 4) Frontend

• The buy page is already wired to `/v1/market/prices` and renders `prices`.  

## 5) Hardening (optional)

• Keep the `1.0` fallback to avoid breaking existing UI.  
If desired, switch to `null` and render “—” for clearer UX when env is incomplete.  

---

## Acceptance checklist

1) Env set and detected.  
2) Quotes preview shows a reasonable route.  
3) Buy page shows non‑1.0 prices for VVV/DIEM.  

---

## One‑liner env block (Base)

```
DEX_PROVIDERS=uniswap_v2,aerodrome
UNISWAP_V2_ROUTER_ADDRESS=0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24
AERODROME_ROUTER_ADDRESS=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43
BASE_CHAIN_ID=8453

VVV_TOKEN_ADDRESS=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf
DIEM_TOKEN_ADDRESS=0xf4d97f2da56e8c3098f3a8d538db630a2606a024
QUOTE_TOKEN_ADDRESS=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913

# DIEM -> WETH -> USDC
TRADE_PATH=0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
```

