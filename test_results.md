2025-10-31 06:54:20.404 | [broker-startup] Validating capacity broker environment
2025-10-31 06:54:20.678 | Stage readiness
2025-10-31 06:54:20.678 | ============================================================
2025-10-31 06:54:20.678 | Core Infrastructure: READY
2025-10-31 06:54:20.678 | 
2025-10-31 06:54:20.678 | Broker API: READY
2025-10-31 06:54:20.678 | 
2025-10-31 06:54:20.678 | Single-loop Orchestrator (dry-run): READY
2025-10-31 06:54:20.678 |   note: Default loop now runs StakeMaster -> quorum-gated ArbiDiem -> CapacityBroker -> AI Treasurer guidance. Disable quorum by setting QUORUM_ENABLE=0.
2025-10-31 06:54:20.678 | 
2025-10-31 06:54:20.678 | Single-loop Orchestrator (live trading): READY
2025-10-31 06:54:20.678 |   note: Progressive-live gating (STAKEMASTER_PROGRESSIVE_ENABLE) is expected before enabling live trades.
2025-10-31 06:54:20.678 |   note: ArbiDiem guardrails still need calibration; review RISK_* thresholds before relying on unattended live execution.
2025-10-31 06:54:20.678 | 
2025-10-31 06:54:20.678 | Token Watcher Helper: READY
2025-10-31 06:54:20.678 |   note: Watcher auto-starts in stack runs when AUTOSTART_TOKEN_WATCHER_ALLOW_NO_KEY=1; set AUTOSTART_TOKEN_WATCHER=0 to skip.
2025-10-31 06:54:20.678 |   note: Telemetry overlap is acceptable in v1; disable if another watcher already publishes the same signals.
2025-10-31 06:54:20.678 | 
2025-10-31 06:54:20.678 | [ok] Environment configuration looks good!
2025-10-31 06:54:20.686 | [broker-startup] Applying database migrations
2025-10-31 06:54:21.493 | 2025-10-31 10:54:21 INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
2025-10-31 06:54:21.493 | 2025-10-31 10:54:21 INFO  [alembic.runtime.migration] Will assume transactional DDL.
2025-10-31 06:54:21.575 | [broker-startup] Refreshing DEX pool catalog (market:pools:watch --once)
2025-10-31 06:54:23.120 | 2025-10-31 10:54:23 | INFO | marketdata.pools | pool watcher starting: factories=uniswap_v2,aerodrome_vol,aerodrome_stable,uniswap_v3 interval=120s backfill=5000 span=2000
2025-10-31 06:54:28.863 | 2025-10-31 10:54:28 | INFO | marketdata.pools | uniswap_v2 discovered 619 new pools (scanned 21975 blocks)
2025-10-31 06:54:37.125 | 2025-10-31 10:54:37 | WARNING | marketdata.pools | get_logs error for uniswap_v3: 429 Client Error: Too Many Requests for url: https://mainnet.base.org/
2025-10-31 06:54:37.219 | [broker-startup] Running tests with Redis backing (REDIS_URL=redis://redis:6379/0)
2025-10-31 06:54:37.219 | [broker-startup] Launching test suite in background (non-blocking)
2025-10-31 06:54:37.219 | [broker-startup] Starting broker API
2025-10-31 06:54:38.146 | 2025-10-31 10:54:38 | INFO | BROKER[auth] | security: admin token configured; admin endpoints require Bearer <redacted>
2025-10-31 06:54:38.204 | 2025-10-31 10:54:38 | INFO | BROKER[store] | broker.store: using SQL backend
2025-10-31 06:54:38.541 | 2025-10-31 10:54:38 | INFO | marketdata.provider | marketdata warm cache thread started
2025-10-31 06:55:13.819 | INFO:     Started server process [1]
2025-10-31 06:55:13.819 | INFO:     Waiting for application startup.
2025-10-31 06:55:13.821 | INFO:     Application startup complete.
2025-10-31 06:55:13.823 | INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
2025-10-31 07:07:25.285 | ...........................................s..sssss..................INFO:     172.18.0.1:51980 - "GET / HTTP/1.1" 307 Temporary Redirect
2025-10-31 07:07:25.295 | INFO:     172.18.0.1:51980 - "GET /buy.html HTTP/1.1" 200 OK
2025-10-31 07:07:25.310 | INFO:     172.18.0.1:51980 - "GET /admin/buy.js HTTP/1.1" 200 OK
2025-10-31 07:07:25.770 | .INFO:     172.18.0.1:51986 - "GET /favicon.ico HTTP/1.1" 404 Not Found
2025-10-31 07:07:41.035 | .. [ 53%]
2025-10-31 07:09:11.854 | ................ss..INFO:     172.18.0.1:46620 - "GET /v1/env HTTP/1.1" 200 OK
2025-10-31 07:09:37.208 | F[broker-startup] Background tests exited rc=124
2025-10-31 07:11:42.598 | INFO:     172.18.0.1:57752 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:13:10.191 | INFO:     172.18.0.1:48060 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:13:12.193 | INFO:     172.18.0.1:48060 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:16:14.896 | INFO:     172.18.0.1:45050 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:20:14.983 | INFO:     172.18.0.1:60720 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:21:25.314 | INFO:     172.18.0.1:54144 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:21:27.198 | INFO:     172.18.0.1:54144 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:22:12.186 | INFO:     172.18.0.1:37446 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:23:17.701 | INFO:     172.18.0.1:51720 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:25:09.618 | INFO:     172.18.0.1:42574 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:25:12.197 | INFO:     172.18.0.1:42574 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:26:17.518 | INFO:     172.18.0.1:45558 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:27:45.126 | INFO:     172.18.0.1:41158 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:28:14.983 | INFO:     172.18.0.1:39572 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:29:21.003 | INFO:     172.18.0.1:36508 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:30:04.221 | INFO:     172.18.0.1:47992 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 200 OK
2025-10-31 07:31:08.869 | INFO:     172.18.0.1:44906 - "GET /buy.html HTTP/1.1" 200 OK
2025-10-31 07:31:45.561 | INFO:     172.18.0.1:44750 - "GET /docs HTTP/1.1" 200 OK
2025-10-31 07:31:46.091 | INFO:     172.18.0.1:44750 - "GET /openapi.json HTTP/1.1" 200 OK
2025-10-31 07:31:51.486 | INFO:     172.18.0.1:50852 - "GET /admin/buy.js HTTP/1.1" 200 OK