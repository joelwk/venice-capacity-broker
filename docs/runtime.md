Resolved 143 packages in 1ms
Audited 121 packages in 114ms
[env] Validating environment via scripts/validate_broker_env.py 
Stage readiness
============================================================
Core Infrastructure: READY

Broker API: READY

Single-loop Orchestrator (dry-run): READY
  note: Default loop now runs StakeMaster -> quorum-gated ArbiDiem -> CapacityBroker -> AI Treasurer guidance. Disable quorum by setting QUORUM_ENABLE=0.

Single-loop Orchestrator (live trading): READY
  note: Progressive-live gating (STAKEMASTER_PROGRESSIVE_ENABLE) is expected before enabling live trades.
  note: ArbiDiem guardrails still need calibration; review RISK_* thresholds before relying on unattended live execution.

Token Watcher Helper: READY
  note: Watcher auto-starts in stack runs when AUTOSTART_TOKEN_WATCHER_ALLOW_NO_KEY=1; set AUTOSTART_TOKEN_WATCHER=0 to skip.
  note: Telemetry overlap is acceptable in v1; disable if another watcher already publishes the same signals.

[ok] Environment configuration looks good!
[stack] refreshing pool catalog -> /home/runner/workspace/.local/bin/uv run python apps/cli/main.py market:pools:watch --once
2025-11-01 10:34:40 | INFO | marketdata.pools | pool watcher starting: factories=uniswap_v2,aerodrome_vol,aerodrome_stable,uniswap_v3 interval=120s backfill=200 span=500
2025-11-01 10:36:12 | INFO | marketdata.pools | uniswap_v2 discovered 874 new pools (scanned 19373 blocks)
[stack] started broker-api (pid=211) -> /home/runner/workspace/.local/bin/uv run uvicorn apps.broker_api.app:app --host 0.0.0.0 --port 8000
[stack] started agent-loop (pid=212) -> /home/runner/workspace/.local/bin/uv run python /home/runner/workspace/apps/cli/main.py run:loop --sleep 15 --max-cycles 0 --enable-live
[stack] started token-watcher (pid=213) -> /home/runner/workspace/.local/bin/uv run python services/marketdata/token_watcher.py
2025-11-01 10:36:45 | INFO | marketdata.token_watcher | tracking 3 token(s): 0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf, 0xf4d97f2da56e8c3098f3a8d538db630a2606a024, 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913; interval=300s
2025-11-01 10:36:46 | INFO | BROKER[auth] | security: admin token configured; admin endpoints require Bearer <redacted>
2025-11-01 10:36:48 | INFO | marketdata.provider | marketdata warm cache thread started
2025-11-01 10:36:48 | INFO | BROKER[store] | broker.store: using SQL backend
2025-11-01 10:36:48 | INFO | marketdata.provider | marketdata warm cache thread started
INFO:     Started server process [220]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
INFO:     172.31.86.194:51792 - "GET / HTTP/1.1" 307 Temporary Redirect
INFO:     172.31.86.194:51792 - "GET /buy.html HTTP/1.1" 200 OK
INFO:     172.31.86.194:51792 - "GET /admin/style.css HTTP/1.1" 200 OK
2025-11-01 10:36:51 | ERROR | marketdata.provider | Missing required environment variable(s) for market data: DIEM_TOKEN_ADDRESS, QUOTE_TOKEN_ADDRESS
market data provider creation failed
Traceback (most recent call last):
  File "/home/runner/workspace/apps/broker_api/marketdata.py", line 58, in get_marketdata_provider
    _provider_instance = MarketDataProvider()
  File "/home/runner/workspace/services/marketdata/provider.py", line 887, in __init__
    self._ensure_required_env()
  File "/home/runner/workspace/services/marketdata/provider.py", line 186, in _ensure_required_env
    raise EnvironmentError(message)
OSError: Missing required environment variable(s) for market data: DIEM_TOKEN_ADDRESS, QUOTE_TOKEN_ADDRESS
marketdata warmup failed: 500: provider creation failed: Missing required environment variable(s) for market data: DIEM_TOKEN_ADDRESS, QUOTE_TOKEN_ADDRESS
2025-11-01 10:36:51 | ERROR | marketdata.provider | Missing required environment variable(s) for market data: DIEM_TOKEN_ADDRESS, QUOTE_TOKEN_ADDRESS
market data provider creation failed
Traceback (most recent call last):
  File "/home/runner/workspace/apps/broker_api/marketdata.py", line 58, in get_marketdata_provider
    _provider_instance = MarketDataProvider()
  File "/home/runner/workspace/services/marketdata/provider.py", line 887, in __init__
    self._ensure_required_env()
  File "/home/runner/workspace/services/marketdata/provider.py", line 186, in _ensure_required_env
    raise EnvironmentError(message)
OSError: Missing required environment variable(s) for market data: DIEM_TOKEN_ADDRESS, QUOTE_TOKEN_ADDRESS
env-and-prices warmup failed: 500: provider creation failed: Missing required environment variable(s) for market data: DIEM_TOKEN_ADDRESS, QUOTE_TOKEN_ADDRESS
INFO:     172.31.86.194:51800 - "GET /admin/buy.js HTTP/1.1" 200 OK
2025-11-01 10:36:51 | ERROR | marketdata.provider | Missing required environment variable(s) for market data: DIEM_TOKEN_ADDRESS, QUOTE_TOKEN_ADDRESS
market data provider creation failed
Traceback (most recent call last):
  File "/home/runner/workspace/apps/broker_api/marketdata.py", line 58, in get_marketdata_provider
    _provider_instance = MarketDataProvider()
  File "/home/runner/workspace/services/marketdata/provider.py", line 887, in __init__
    self._ensure_required_env()
  File "/home/runner/workspace/services/marketdata/provider.py", line 186, in _ensure_required_env
    raise EnvironmentError(message)
OSError: Missing required environment variable(s) for market data: DIEM_TOKEN_ADDRESS, QUOTE_TOKEN_ADDRESS
INFO:     172.31.86.194:51792 - "GET /v1/env-and-prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 500 Internal Server Error
INFO:     172.31.86.194:51792 - "GET /v1/env HTTP/1.1" 200 OK
2025-11-01 10:36:51 | ERROR | marketdata.provider | Missing required environment variable(s) for market data: DIEM_TOKEN_ADDRESS, QUOTE_TOKEN_ADDRESS
market data provider creation failed
Traceback (most recent call last):
  File "/home/runner/workspace/apps/broker_api/marketdata.py", line 58, in get_marketdata_provider
    _provider_instance = MarketDataProvider()
  File "/home/runner/workspace/services/marketdata/provider.py", line 887, in __init__
    self._ensure_required_env()
  File "/home/runner/workspace/services/marketdata/provider.py", line 186, in _ensure_required_env
    raise EnvironmentError(message)
OSError: Missing required environment variable(s) for market data: DIEM_TOKEN_ADDRESS, QUOTE_TOKEN_ADDRESS
INFO:     172.31.86.194:51792 - "GET /v1/market/prices?symbols=VVV,DIEM,ETH,USDC,WBTC HTTP/1.1" 500 Internal Server Error
make: *** [Makefile:131: run-stack] Error 143