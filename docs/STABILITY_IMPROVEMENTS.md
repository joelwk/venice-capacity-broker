# Frontend Stability Improvements

## Overview

This document describes the stability improvements made to address intermittent initialization and quote request failures in the broker frontend.

## Problems Identified

1. **Market Data Warm-up Race Conditions**: The 5-second warmup timeout was too short, causing failures when market data hadn't initialized yet
2. **Lock Contention**: The 2-second lock timeout was too short, causing timeouts during concurrent requests
3. **Frontend Initialization Failures**: App would fail completely if prices couldn't load, blocking quote requests
4. **No Graceful Degradation**: Backend would fail completely if warmup hadn't completed instead of attempting inline fetch
5. **Limited Retry Logic**: Frontend only retried quotes 2 times with 15s timeout

## Solutions Implemented

### Backend Changes (`services/pricing/service.py`)

1. **Configurable Timeouts**:
   - Warmup timeout increased from 5s to 10s (configurable via `PRICING_WARMUP_TIMEOUT_SECONDS`)
   - Lock timeout increased from 2s to 5s (configurable via `PRICING_LOCK_TIMEOUT_SECONDS`)
   - Added `PRICING_WARMUP_RETRY_ENABLED` flag for graceful degradation

2. **Graceful Degradation**:
   - If warmup hasn't completed after timeout, attempt inline price fetch instead of failing
   - Set ready flag after successful inline fetch to allow future requests
   - Better logging for warmup completion and failures

3. **Improved Lock Handling**:
   - Longer timeout allows more time for lock acquisition
   - Better error handling when lock acquisition fails
   - Ensures ready flag is set after successful fetches

### Backend Router Changes (`apps/broker_api/routers/quotes.py`)

1. **Retry Logic**:
   - Added automatic retry for warmup errors (up to 2 retries)
   - Exponential backoff between retries (0.5s, 1s delays)
   - Better error messages distinguishing retryable vs non-retryable errors

2. **Error Handling**:
   - Distinguishes between warmup errors (retryable) and other errors
   - Provides clear error messages indicating when retries are happening

### Frontend Changes (`apps/control-plane/buy.js`)

1. **Resilient Initialization**:
   - Separates critical (env/treasury) from non-critical (prices) initialization
   - App continues to function even if prices fail to load
   - Automatic background retry with exponential backoff for failed price loads
   - Better error messages distinguishing critical vs non-critical failures

2. **Improved Retry Logic**:
   - Increased quote retry attempts from 2 to 3
   - Increased quote timeout from 15s to 20s
   - Better logging of retry attempts
   - Improved detection of warmup errors

3. **Better Error Messages**:
   - User-friendly messages explaining warmup delays
   - Distinguishes between critical and non-critical errors
   - Provides actionable feedback to users

## Configuration

### Environment Variables

- `PRICING_WARMUP_TIMEOUT_SECONDS`: Warmup timeout (default: 10.0 seconds)
- `PRICING_LOCK_TIMEOUT_SECONDS`: Lock acquisition timeout (default: 5.0 seconds)
- `PRICING_WARMUP_RETRY_ENABLED`: Enable graceful degradation on warmup failures (default: true)

## Expected Behavior

### Before Fixes
- **Success Rate**: ~20% (2/10 refreshes)
- **Failure Mode**: Complete app failure on initialization errors
- **User Experience**: "Initialization error. Please refresh the page."

### After Fixes
- **Success Rate**: Expected >95% (with automatic retries)
- **Failure Mode**: Graceful degradation - app continues with limited features
- **User Experience**: Clear messages explaining status, automatic retries happen in background

## Testing Recommendations

1. **Load Testing**: Test with multiple concurrent quote requests during startup
2. **Failure Injection**: Test behavior when market data service is slow or unavailable
3. **Timeout Scenarios**: Verify retry logic handles various timeout scenarios correctly
4. **Lock Contention**: Test behavior under high concurrent load

## Monitoring

Key metrics to monitor:
- Quote request success rate
- Warmup completion time
- Lock acquisition timeouts
- Retry attempt counts
- Frontend initialization failures

## Future Improvements

1. **Health Check Endpoint**: Add `/health` endpoint to check market data readiness
2. **Circuit Breaker**: Implement circuit breaker pattern for repeated failures
3. **Caching**: Add price caching with TTL to reduce backend load
4. **WebSocket**: Consider WebSocket for real-time price updates instead of polling

