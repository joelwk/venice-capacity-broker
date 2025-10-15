#!/usr/bin/env bash
# Smoke test for Venice Broker buy flow
# Run after applying environment fixes

set -e

BASE_URL="${BROKER_BASE_URL:-http://localhost:8000}"
ADMIN_TOKEN="${BROKER_ADMIN_TOKEN:-}"

echo "==================================================================="
echo "Venice Broker Smoke Test"
echo "==================================================================="
echo "Base URL: $BASE_URL"
echo ""

# Color helpers
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

pass_count=0
fail_count=0

check_endpoint() {
    local name="$1"
    local endpoint="$2"
    local expected_status="${3:-200}"
    local extra_args="${4:-}"
    
    echo -n "[$name] "
    
    # shellcheck disable=SC2086
    response=$(curl -s -w "\n%{http_code}" $extra_args "$BASE_URL$endpoint" 2>&1)
    body=$(echo "$response" | head -n -1)
    status=$(echo "$response" | tail -n 1)
    
    if [ "$status" = "$expected_status" ]; then
        echo -e "${GREEN}✓${NC} HTTP $status"
        ((pass_count++))
        return 0
    else
        echo -e "${RED}✗${NC} HTTP $status (expected $expected_status)"
        echo "   Response: ${body:0:100}"
        ((fail_count++))
        return 1
    fi
}

check_json_field() {
    local name="$1"
    local endpoint="$2"
    local field_path="$3"
    local extra_args="${4:-}"
    
    echo -n "[$name] "
    
    # shellcheck disable=SC2086
    response=$(curl -s $extra_args "$BASE_URL$endpoint" 2>&1)
    
    # Use jq if available, else grep
    if command -v jq &> /dev/null; then
        value=$(echo "$response" | jq -r "$field_path" 2>/dev/null || echo "null")
    else
        # Fallback: basic grep
        value=$(echo "$response" | grep -o "\"$(echo "$field_path" | sed 's/\..*//')\"" || echo "")
    fi
    
    if [ "$value" != "null" ] && [ "$value" != "" ]; then
        echo -e "${GREEN}✓${NC} $field_path present"
        ((pass_count++))
        return 0
    else
        echo -e "${RED}✗${NC} $field_path missing or null"
        ((fail_count++))
        return 1
    fi
}

echo "-------------------------------------------------------------------"
echo "1. Basic Health Checks"
echo "-------------------------------------------------------------------"

check_endpoint "Health" "/health" "200"
check_endpoint "Metrics" "/metrics" "200"

echo ""
echo "-------------------------------------------------------------------"
echo "2. Environment & Configuration"
echo "-------------------------------------------------------------------"

check_endpoint "Env Status" "/v1/env" "200"
check_json_field "Payments Config" "/v1/env" ".payments.treasury_address"
check_json_field "Features: Quotes" "/v1/env" ".features.quotes"
check_json_field "Features: Purchases" "/v1/env" ".features.purchases"

echo ""
echo "-------------------------------------------------------------------"
echo "3. Market Data & Pricing"
echo "-------------------------------------------------------------------"

check_endpoint "Market Prices" "/v1/market/prices?symbols=DIEM,ETH,USDC" "200"
check_json_field "DIEM Price" "/v1/market/prices?symbols=DIEM,ETH,USDC" ".prices.DIEM"
check_json_field "ETH Price" "/v1/market/prices?symbols=DIEM,ETH,USDC" ".prices.ETH"

check_endpoint "Combined Env+Prices" "/v1/env-and-prices?symbols=DIEM,ETH,USDC" "200"

echo ""
echo "-------------------------------------------------------------------"
echo "4. Quote Generation (requires valid pricing)"
echo "-------------------------------------------------------------------"

check_endpoint "Quote: 0.1 DIEM USDC" "/v1/quotes?units=0.1&asset=USDC" "200"
check_endpoint "Quote: 0.1 DIEM ETH" "/v1/quotes?units=0.1&asset=ETH" "200"

echo ""
echo "==================================================================="
echo "Summary"
echo "==================================================================="
echo -e "Passed: ${GREEN}$pass_count${NC}"
echo -e "Failed: ${RED}$fail_count${NC}"
echo ""

if [ "$fail_count" -eq 0 ]; then
    echo -e "${GREEN}All smoke tests passed!${NC}"
    exit 0
else
    echo -e "${RED}Some tests failed. Check the output above.${NC}"
    echo ""
    echo "Common fixes:"
    echo "  1. Verify Replit Secrets contain all keys from config/broker-fixes.env.template"
    echo "  2. Restart the deployment after updating secrets"
    echo "  3. Check runtime.log for DEX timeout/pricing errors"
    echo "  4. Run: python scripts/validate_broker_env.py --export"
    exit 1
fi

