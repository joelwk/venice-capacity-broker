#!/usr/bin/env python
"""Debug market data pricing to understand why we're getting base unit prices."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.marketdata.provider import MarketDataProvider


def main():
    print("=== Debugging Market Data Prices ===\n")

    # Create provider
    md = MarketDataProvider()

    # Test individual methods
    print("1. Testing _address_for_symbol:")
    vvv_addr = md._address_for_symbol("VVV")
    diem_addr = md._address_for_symbol("DIEM")
    print(f"   VVV address: {vvv_addr}")
    print(f"   DIEM address: {diem_addr}")

    print("\n2. Testing decimals:")
    if vvv_addr:
        vvv_dec = md._erc20_decimals(vvv_addr)
        print(f"   VVV decimals: {vvv_dec}")
    if diem_addr:
        diem_dec = md._erc20_decimals(diem_addr)
        print(f"   DIEM decimals: {diem_dec}")

    print("\n3. Testing TRADE_PATH:")
    try:
        path = md._path_from_env()
        print(f"   TRADE_PATH: {path}")
    except Exception as e:
        print(f"   Error getting TRADE_PATH: {e}")

    print("\n4. Testing quote token:")
    try:
        quote = md._quote_token_address()
        print(f"   QUOTE_TOKEN_ADDRESS: {quote}")
    except Exception as e:
        print(f"   Error getting QUOTE_TOKEN_ADDRESS: {e}")

    print("\n5. Testing prices method:")
    symbols = ["VVV", "DIEM", "ETH", "USDC"]
    prices = md.prices(symbols)

    for sym, price in prices.items():
        print(f"   {sym}: {price}")
        # Check if it looks like a base unit price
        if sym in ["VVV", "DIEM"] and price < 0.01:
            print(
                f"      -> Suspicious! If this is base units, real price would be: ${price * 1e18:,.2f}"
            )

    print("\n6. Testing best_price directly for VVV:")
    if vvv_addr and quote:
        try:
            bp = md.best_price([vvv_addr, quote], amount_in_decimal=1.0)
            print("   VVV->USDC best_price result:")
            print(f"     Provider: {bp.get('provider')}")
            print(f"     Price: {bp.get('price')}")
            print(f"     Decimals: {bp.get('decimals')}")
        except Exception as e:
            print(f"   Error: {e}")

    print("\n7. Testing DIEM price with fallback:")
    try:
        diem_price = md.diem_price_with_fallback()
        print(f"   DIEM price with fallback: {diem_price}")
    except Exception as e:
        print(f"   Error: {e}")


if __name__ == "__main__":
    main()
