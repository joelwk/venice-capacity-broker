# Test Failures Analysis

## Summary
**14 tests failed** out of 135 total tests (112 passed, 9 skipped)

## Failure Categories

### 1. Authentication Issues (6 failures)
**Root cause**: `require_admin()` function reads `BROKER_REQUIRE_ADMIN_TOKEN` at module load time, but tests set this env var after module import. The module-level constants are evaluated once.

**Affected tests**:
- `test_counters_filters_limit_and_asc` - Expected 200, got 401
- `test_admin_limits_auth_and_defaults` - Expected 200, got 401  
- `test_admin_limits_post_valid_invalid_and_idempotent` - Expected 200, got 401
- `test_rotate_revoke_old_key` - Expected 200, got 401 (sets `BROKER_REQUIRE_ADMIN_TOKEN=false`)
- `test_admin_revoke_endpoint` - Expected 200, got 401 (sets `BROKER_REQUIRE_ADMIN_TOKEN=false`)

**Fix**: Make `require_admin()` read env vars dynamically instead of at module load time.

### 2. Missing Idempotency Header (1 failure)
**Root cause**: Chat endpoint (`/v1/chat`) doesn't check for `Idempotency-Key` header or set `X-Idempotency-Accepted` header.

**Affected test**:
- `test_idempotency_and_purge_cli` - Missing `X-Idempotency-Accepted: true` header

**Fix**: Add idempotency key handling to chat endpoint.

### 3. Module Reload Issues (5 failures)
**Root cause**: Tests reload the `apps.broker_api.app` module but:
- `app_module.app` attribute may not exist after reload
- `app_module._get_marketdata_provider` attribute may not exist after reload

**Affected tests**:
- `test_settlement_preview_with_slippage_and_pool_take` - `AttributeError: 'FastAPI' object has no attribute 'app'`
- `test_settlement_preview_fallback_with_risk_hints` - Same error
- `test_settlement_preview_exceeds_slippage_cap` - Same error  
- `test_settlement_preview_exceeds_pool_take_cap` - Same error
- `test_env_and_prices_cache_includes_meta` - `AttributeError: module has no attribute '_get_marketdata_provider'`
- `test_market_prices_cache_flags_meta` - Same error

**Fix**: Tests should access `app_module.app` directly (not `app_module.app.app`), or ensure module attributes are set correctly after reload.

### 4. HTTP Status Code Mismatch (1 failure)
**Root cause**: FastAPI returns 422 (Unprocessable Entity) for missing required query parameters, which is correct behavior. Test expects 400.

**Affected test**:
- `test_counters_validates_tenant_and_bucket_seconds` - Expected 400, got 422

**Fix**: Update test to expect 422 instead of 400.

### 5. ETH Price Route Issue (1 failure)
**Root cause**: ETH price route starts with DIEM address instead of WETH address. Should use canonical route logic.

**Affected test**:
- `test_eth_price_canonical_route_avoids_vvv` - Route starts with DIEM instead of WETH

**Fix**: Check canonical route logic in `MarketDataProvider.best_price()` method.

## Priority Fixes

1. **High**: Fix admin auth to read env vars dynamically
2. **High**: Fix module reload issues in settlement tests
3. **Medium**: Add idempotency header support
4. **Low**: Update test expectation for HTTP status code
5. **Low**: Fix ETH price route logic

