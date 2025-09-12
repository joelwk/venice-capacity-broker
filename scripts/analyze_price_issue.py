#!/usr/bin/env python
"""Analyze the price issue to understand the scaling problem."""

def main():
    print("=== Analyzing Price Scaling Issue ===\n")
    
    # Given data from user
    given_prices = {
        "VVV": 0.000012,
        "DIEM": 1.030026151674e-12,
        "ETH": 4401.821161,
        "USDC": 1
    }
    
    # Expected prices from web search
    expected_prices = {
        "VVV": 2.63,
        "DIEM": 227.26,
        "ETH": 4401.82,
        "USDC": 1.0
    }
    
    # Given ratios
    given_ratios = {
        "VVV_DIEM": 11650189.638872357,
        "ETH_DIEM": 4273504273504274,
        "USDC_DIEM": 970849136572.6964
    }
    
    print("1. Calculating required scaling factors:")
    for token, given in given_prices.items():
        if token == "USDC":
            continue
        expected = expected_prices[token]
        scaling = expected / given if given > 0 else 0
        print(f"   {token}: {given:.2e} → ${expected:.2f} (scale: {scaling:.2e})")
    
    print("\n2. Checking if prices might be inverted (USDC per token):")
    for token, given in given_prices.items():
        if token == "USDC":
            continue
        if given > 0:
            inverted = 1 / given
            print(f"   {token}: 1/{given:.2e} = {inverted:.2e}")
    
    print("\n3. Analyzing ratios:")
    print("   Given ratios:")
    for ratio, value in given_ratios.items():
        print(f"      {ratio}: {value:,.2f}")
    
    print("\n   Expected ratios from market prices:")
    for token in ["VVV", "ETH", "USDC"]:
        if token != "DIEM":
            ratio = expected_prices[token] / expected_prices["DIEM"]
            print(f"      {token}_DIEM: {ratio:.6f}")
    
    print("\n4. Testing hypothesis: prices are in some exotic unit")
    # The ratios suggest the relationship between tokens is preserved,
    # but the absolute scale is wrong
    
    # Let's see if we can find a common scaling factor
    # Using USDC as anchor (since it's always 1.0)
    usdc_diem_ratio_given = given_ratios["USDC_DIEM"]
    usdc_diem_ratio_expected = expected_prices["USDC"] / expected_prices["DIEM"]
    
    print(f"\n   USDC/DIEM ratio:")
    print(f"      Given: {usdc_diem_ratio_given:,.2f}")
    print(f"      Expected: {usdc_diem_ratio_expected:.6f}")
    print(f"      Ratio of ratios: {usdc_diem_ratio_given / usdc_diem_ratio_expected:,.2e}")
    
    # This ratio of ratios might be our scaling factor
    scaling_factor = usdc_diem_ratio_given / usdc_diem_ratio_expected
    
    print(f"\n5. Testing scaling factor {scaling_factor:.2e} on prices:")
    for token, given in given_prices.items():
        if token == "USDC":
            continue
        scaled = given * scaling_factor
        expected = expected_prices[token]
        error = abs(scaled - expected) / expected * 100
        print(f"   {token}: {given:.2e} × {scaling_factor:.2e} = ${scaled:.2f} (expected ${expected:.2f}, error {error:.1f}%)")

if __name__ == "__main__":
    main()
