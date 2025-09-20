#!/usr/bin/env python
"""Test market data pricing with proper environment setup."""

import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Set up environment variables before importing
os.environ['VVV_TOKEN_ADDRESS'] = '0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf'
os.environ['DIEM_TOKEN_ADDRESS'] = '0xf4d97f2da56e8c3098f3a8d538db630a2606a024'
os.environ['QUOTE_TOKEN_ADDRESS'] = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
os.environ['WETH_ADDRESS'] = '0x4200000000000000000000000000000000000006'

# Multi-hop path: DIEM -> WETH -> USDC
os.environ['TRADE_PATH'] = '0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'

# Base RPC
os.environ['BASE_RPC_URL'] = 'https://mainnet.base.org'
os.environ['BASE_CHAIN_ID'] = '8453'

from services.marketdata.provider import MarketDataProvider

def main():
    print("=== Testing Market Data Prices with Environment ===\n")
    
    # Create provider
    md = MarketDataProvider()
    
    print("1. Configuration check:")
    print(f"   VVV_TOKEN_ADDRESS: {os.getenv('VVV_TOKEN_ADDRESS')}")
    print(f"   DIEM_TOKEN_ADDRESS: {os.getenv('DIEM_TOKEN_ADDRESS')}")
    print(f"   QUOTE_TOKEN_ADDRESS: {os.getenv('QUOTE_TOKEN_ADDRESS')}")
    print(f"   TRADE_PATH: {os.getenv('TRADE_PATH')}")
    
    print("\n2. Testing prices method:")
    symbols = ["VVV", "DIEM", "ETH", "USDC"]
    try:
        prices = md.prices(symbols)
        
        print("\nRaw prices returned:")
        for sym, price in prices.items():
            print(f"   {sym}: {price}")
            # Check if it looks like a base unit price
            if sym in ["VVV", "DIEM"] and price < 0.01:
                print(f"      -> Suspicious! If this is base units (18 decimals), real price would be: ${price * 1e18:,.2f}")
                print(f"      -> If base units (6 decimals for USDC equivalent): ${price * 1e6:,.2f}")
        
        print("\n3. Computing ratios:")
        if "DIEM" in prices and prices["DIEM"] > 0:
            for sym, price in prices.items():
                if sym != "DIEM":
                    ratio = price / prices["DIEM"]
                    print(f"   {sym}_DIEM ratio: {ratio:,.2f}")
                    
    except Exception as e:
        print(f"   Error getting prices: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n4. Direct DIEM price test:")
    try:
        diem_price = md.diem_price_with_fallback()
        print(f"   DIEM price with fallback: {diem_price}")
        if diem_price and diem_price < 0.01:
            print(f"      -> If base units (18 decimals): ${diem_price * 1e18:,.2f}")
    except Exception as e:
        print(f"   Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
