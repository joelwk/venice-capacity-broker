ttaching to broker-1
broker-1  | [broker-startup] Validating capacity broker environment
broker-1  | Stage readiness
broker-1  | ============================================================
broker-1  | Core Infrastructure: READY                                                                                                                                                       
broker-1  |                                                                                                                                                                                  
broker-1  | Broker API: DEGRADED                                                                                                                                                             
broker-1  |                                                                                                                                                                                  
broker-1  | Single-loop Orchestrator (dry-run): READY                                                                                                                                        
broker-1  |   note: Quorum coordinator remains staged for post-v1; current loop runs StakeMaster -> ArbiDiem -> CapacityBroker.                                                              
broker-1  |                                                                                                                                                                                  
broker-1  | Single-loop Orchestrator (live trading): READY                                                                                                                                   
broker-1  |   note: Progressive-live gating (STAKEMASTER_PROGRESSIVE_ENABLE) is expected before enabling live trades.                                                                        
broker-1  |                                                                                                                                                                                  
broker-1  | Token Watcher Helper: READY                                                                                                                                                      
broker-1  |   note: Helper runs under docker compose profile 'helpers'; enable with `docker compose --profile helpers up` when needed.                                                       
broker-1  |   note: Watcher is optional in v1; leave disabled if orchestrator already covers your telemetry requirements.                                                                    
broker-1  |                                                                                                                                                                                  
broker-1  | ✓ Environment configuration looks good!                                                                                                                                          
broker-1  | [broker-startup] Applying database migrations                                                                                                                                    
broker-1  | 2025-10-16 17:12:06 INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.                                                                                               
broker-1  | 2025-10-16 17:12:06 INFO  [alembic.runtime.migration] Will assume transactional DDL.
broker-1  | [broker-startup] Running test suite (KV redis disabled for deterministic unit tests)                                                                                             
broker-1  | ..............F...FFFFF.........................FF.F.....F.......FF..... [ 78%]
broker-1  | ..........FFF.......                                                                    [100%]
broker-1  | =================================== FAILURES ===================================
broker-1  | _____________________ test_rate_limit_resets_without_redis _____________________                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f7e2ff770d0>                                                                                                         
broker-1  | tmp_path = PosixPath('/tmp/pytest-of-root/pytest-0/test_rate_limit_resets_without0')                                                                                             
broker-1  |                                                                                                                                                                                  
broker-1  |     def test_rate_limit_resets_without_redis(monkeypatch, tmp_path):                                                                                                             
broker-1  |         # Configure fast 1s window and 1 request per window                                                                                                                      
broker-1  |         store_file = tmp_path / "tenants2.json"                                                                                                                                  
broker-1  |         os.environ["BROKER_STORE_FILE"] = str(store_file)                                                                                                                        
broker-1  |         os.environ["RATE_LIMITS_ENABLED"] = "true"                                                                                                                               
broker-1  |         os.environ["RATE_LIMIT_WINDOW_SECONDS"] = "1"                                                                                                                            
broker-1  |         os.environ["RATE_LIMIT_MAX_REQUESTS"] = "1"                                                                                                                              
broker-1  |         os.environ.pop("REDIS_URL", None)                                                                                                                                        
broker-1  |         monkeypatch.setenv("SQL_DATABASE_URL", "sqlite:///./test-rate-limit.db")                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  |         from pathlib import Path                                                                                                                                                 
broker-1  |         import importlib.util                                                                                                                                                    
broker-1  |         import time                                                                                                                                                              
broker-1  |                                                                                                                                                                                  
broker-1  |         app_path = Path("apps/broker-api/app.py").resolve()                                                                                                                      
broker-1  |         spec = importlib.util.spec_from_file_location("broker_api_app_rl2", str(app_path))                                                                                       
broker-1  |         assert spec and spec.loader
broker-1  |         broker_app = importlib.util.module_from_spec(spec)                                                                                                                       
broker-1  |         spec.loader.exec_module(broker_app)  # type: ignore[attr-defined]                                                                                                        
broker-1  |                                                                                                                                                                                  
broker-1  |         from collections import deque                                                                                                                                            
broker-1  |                                                                                                                                                                                  
broker-1  |         times = deque([100.0, 100.1, 101.5])                                                                                                                                     
broker-1  |                                                                                                                                                                                  
broker-1  |         def fake_now() -> float:                                                                                                                                                 
broker-1  |             if len(times) > 1:                                                                                                                                                   
broker-1  |                 return times.popleft()                                                                                                                                           
broker-1  |             return times[0]                                                                                                                                                      
broker-1  |                                                                                                                                                                                  
broker-1  |         broker_app._limiter._now = fake_now  # type: ignore[attr-defined]
broker-1  |         monkeypatch.setattr("time.sleep", lambda _seconds: None)                                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  |         def fake_chat(self, messages, model=None, **kw):  # noqa: ANN001                                                                                                         
broker-1  |             return {"status": "ok", "echo": messages}                                                                                                                            
broker-1  |                                                                                                                                                                                  
broker-1  |         from libs import venice_sdk                                                                                                                                              
broker-1  |                                                                                                                                                                                  
broker-1  |         monkeypatch.setattr(venice_sdk.client.VeniceClient, "chat_completions", fake_chat, raising=True)                                                                         
broker-1  |                                                                                                                                                                                  
broker-1  |         # Insert tenant                                                                                                                                                          
broker-1  |         Tenant = broker_app.Tenant  # type: ignore[attr-defined]                                                                                                                 
broker-1  |         tenant = Tenant(id="t2", label="T2", subkey="sub-2", quota=0)                                                                                                            
broker-1  |         broker_app.store.upsert(tenant)                                                                                                                                          
broker-1  |                                                                                                                                                                                  
broker-1  |         from fastapi.testclient import TestClient                                                                                                                                
broker-1  |     
broker-1  |         client = TestClient(broker_app.app)                                                                                                                                      
broker-1  |         headers = {"Authorization": "Bearer sub-2"}                                                                                                                              
broker-1  |         payload = {"messages": [{"role": "user", "content": "hi"}]}                                                                                                              
broker-1  |                                                                                                                                                                                  
broker-1  |         r1 = client.post("/v1/chat", headers={**headers, "Idempotency-Key": "a1"}, json=payload)                                                                                 
broker-1  |         assert r1.status_code == 200                                                                                                                                             
broker-1  |         r2 = client.post("/v1/chat", headers={**headers, "Idempotency-Key": "a2"}, json=payload)                                                                                 
broker-1  |         assert r2.status_code == 429                                                                                                                                             
broker-1  |         # Wait for window to roll (monkeypatched to no-op but advances via fake_now)                                                                                             
broker-1  |         time.sleep(1.2)                                                                                                                                                          
broker-1  |         r3 = client.post("/v1/chat", headers={**headers, "Idempotency-Key": "a3"}, json=payload)                                                                                 
broker-1  | >       assert r3.status_code == 200                                                                                                                                             
broker-1  | E       assert 429 == 200                                                                                                                                                        
broker-1  | E        +  where 429 = <Response [429 Too Many Requests]>.status_code
broker-1  |                                                                                                                                                                                  
broker-1  | tests/test_broker_limits.py:132: AssertionError                                                                                                                                  
broker-1  | ----------------------------- Captured stdout call -----------------------------                                                                                                 
broker-1  | 2025-10-16 17:13:06 | INFO | BROKER[api] | admin ui: mounted at /admin from /app/apps/control-plane                                                                              
broker-1  | 2025-10-16 17:13:06 | INFO | BROKER[api] | security: admin token configured; admin endpoints require Bearer <redacted>                                                           
broker-1  | 2025-10-16 17:13:06 | INFO | BROKER[api] | env.web3 rpc_configured=True chain_id_set=True                                                                                        
broker-1  | 2025-10-16 17:13:06 | INFO | BROKER[api] | env.dex providers=uniswap_v3,aerodrome,uniswap_v2 v3.router=0x2626664c2603336e57b271c5c0b26f421741e481 v3.quoter=0x3d4e44eb1374240ce5f1b871ab261cd16335b76a uniswap_v2.router=0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24 aerodrome.router=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43 stable=False                                  
broker-1  | 2025-10-16 17:13:06 | INFO | BROKER[api] | env.pricing quote_token=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 diem_token=0xf4d97f2da56e8c3098f3a8d538db630a2606a024 vvv_token=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf trade_path=0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913          
broker-1  | 2025-10-16 17:13:06 | INFO | BROKER[api] | env.abi erc20=True uniswap_v2_router=True aerodrome_router=True diem=True
broker-1  | 2025-10-16 17:13:06 | INFO | BROKER[api] | rate-limiter: enabled (window=1s, max=1)                                                                                              
broker-1  | 2025-10-16 17:13:06 | INFO | BROKER[api] | metrics: starlette_exporter not installed; falling back to builtin                                                                    
broker-1  | ------------------------------ Captured log call -------------------------------                                                                                                 
broker-1  | INFO     broker.api:app.py:155 admin ui: mounted at /admin from /app/apps/control-plane                                                                                          
broker-1  | INFO     broker.api:app.py:293 security: admin token configured; admin endpoints require Bearer <redacted>                                                                       
broker-1  | INFO     broker.api:app.py:401 env.web3 rpc_configured=True chain_id_set=True
broker-1  | INFO     broker.api:app.py:406 env.dex providers=uniswap_v3,aerodrome,uniswap_v2 v3.router=0x2626664c2603336e57b271c5c0b26f421741e481 v3.quoter=0x3d4e44eb1374240ce5f1b871ab261cd16335b76a uniswap_v2.router=0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24 aerodrome.router=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43 stable=False                                              
broker-1  | INFO     broker.api:app.py:415 env.pricing quote_token=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 diem_token=0xf4d97f2da56e8c3098f3a8d538db630a2606a024 vvv_token=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf trade_path=0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913                      
broker-1  | INFO     broker.api:app.py:422 env.abi erc20=True uniswap_v2_router=True aerodrome_router=True diem=True
broker-1  | INFO     broker.api:app.py:453 rate-limiter: enabled (window=1s, max=1)                                                                                                          
broker-1  | INFO     broker.api:app.py:1525 metrics: starlette_exporter not installed; falling back to builtin                                                                               
broker-1  | ___________________ test_buyer_lifecycle_quote_verify_subkey ___________________                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f7e2cea6a10>                                                                                                         
broker-1  | tmp_path = PosixPath('/tmp/pytest-of-root/pytest-0/test_buyer_lifecycle_quote_ver0')                                                                                             
broker-1  |                                                                                                                                                                                  
broker-1  |     def test_buyer_lifecycle_quote_verify_subkey(monkeypatch, tmp_path):                                                                                                         
broker-1  |         # Enable features and static pricing for ETH                                                                                                                             
broker-1  |         monkeypatch.setenv("QUOTES_ENABLED", "true")                                                                                                                             
broker-1  |         monkeypatch.setenv("PURCHASES_ENABLED", "true")
broker-1  |         monkeypatch.setenv("PRICE_ENGINE", "static")                                                                                                                             
broker-1  |         monkeypatch.setenv("PRICE_UNIT_ETH_WEI", str(10**15))  # 0.001 ETH per unit                                                                                              
broker-1  |         monkeypatch.setenv("PRICE_QUOTE_TTL_SECONDS", "120")                                                                                                                     
broker-1  |         # Minimal chain/payment config                                                                                                                                           
broker-1  |         monkeypatch.setenv("BASE_RPC_URL", "http://localhost:8545")                                                                                                              
broker-1  |         monkeypatch.setenv("TREASURY_ADDRESS", "0xabc0000000000000000000000000000000000001")                                                                                     
broker-1  |         # Provide a temporary SQLite database so SQLModel path is available even when                                                                                            
broker-1  |         # Postgres/SQL_DATABASE_URL is not configured in CI environments.                                                                                                        
broker-1  |         db_path = tmp_path / "buyer.db"                                                                                                                                          
broker-1  |         store_path = tmp_path / "store.json"                                                                                                                                     
broker-1  |         monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")                                                                                                           
broker-1  |         monkeypatch.setenv("BROKER_STORE_FILE", str(store_path))                                                                                                                 
broker-1  |         db_path.unlink(missing_ok=True)                                                                                                                                          
broker-1  |         store_path.unlink(missing_ok=True)                                                                                                                                       
broker-1  |     
broker-1  |         mod = _load_broker_app_module()                                                                                                                                          
broker-1  |                                                                                                                                                                                  
broker-1  |         # Stub RPC calls to confirm ETH payment                                                                                                                                  
broker-1  |         buyer_wallet = "0xdef0000000000000000000000000000000000002"                                                                                                              
broker-1  |         tx_payload = {                                                                                                                                                           
broker-1  |             "to": "0xabc0000000000000000000000000000000000001",                                                                                                                  
broker-1  |             "from": buyer_wallet,                                                                                                                                                
broker-1  |             "value": hex(5 * 10**15),  # placeholder; updated once quote is fetched                                                                                              
broker-1  |         }                                                                                                                                                                        
broker-1  |         tx_hash = "0x" + "1" * 64                                                                                                                                                
broker-1  |                                                                                                                                                                                  
broker-1  |         def _fake_rpc(url: str, method: str, params: list):  # noqa: ANN001                                                                                                      
broker-1  |             if method == "eth_getTransactionReceipt":                                                                                                                            
broker-1  |                 return {"status": hex(1), "blockNumber": hex(12345), "logs": []}
broker-1  |             if method == "eth_getTransactionByHash":                                                                                                                             
broker-1  |                 return tx_payload                                                                                                                                                
broker-1  |             raise AssertionError(f"unexpected rpc call: {method}")                                                                                                               
broker-1  |                                                                                                                                                                                  
broker-1  |         monkeypatch.setattr(mod, "_rpc_call", _fake_rpc, raising=True)                                                                                                           
broker-1  |                                                                                                                                                                                  
broker-1  |         # Stub subkey issuance to avoid Venice network                                                                                                                           
broker-1  |         def _fake_issue(parent_key: str, label: str, consumption_limit: int, expires_at: str | None = None):  # noqa: ANN001                                                     
broker-1  |             return {"apiKey": "sk-test-123", "id": "kid-1"}                                                                                                                      
broker-1  |                                                                                                                                                                                  
broker-1  |         monkeypatch.setattr(mod.keys, "issue_scoped_key", _fake_issue, raising=True)
broker-1  |                                                                                                                                                                                  
broker-1  |         # Use FastAPI TestClient                                                                                                                                                 
broker-1  |         from fastapi.testclient import TestClient                                                                                                                                
broker-1  |                                                                                                                                                                                  
broker-1  |         client = TestClient(mod.app)                                                                                                                                             
broker-1  |         # Get a quote for ETH                                                                                                                                                    
broker-1  |         q = client.get("/v1/quotes", params={"units": 5, "asset": "ETH"})                                                                                                        
broker-1  | >       assert q.status_code == 200, q.text                                                                                                                                      
broker-1  | E       AssertionError: {"detail":"Can't load plugin: sqlalchemy.dialects:sqlite"}                                                                                               
broker-1  | E       assert 400 == 200                                                                                                                                                        
broker-1  | E        +  where 400 = <Response [400 Bad Request]>.status_code                                                                                                                 
broker-1  | 
broker-1  | tests/test_buyer_e2e.py:90: AssertionError                                                                                                                                       
broker-1  | ----------------------------- Captured stdout call -----------------------------                                                                                                 
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | admin ui: mounted at /admin from /app/apps/control-plane                                                                              
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | security: admin token configured; admin endpoints require Bearer <redacted>                                                           
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.web3 rpc_configured=True chain_id_set=True                                                                                        
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.dex providers=uniswap_v3,aerodrome,uniswap_v2 v3.router=0x2626664c2603336e57b271c5c0b26f421741e481 v3.quoter=0x3d4e44eb1374240ce5f1b871ab261cd16335b76a uniswap_v2.router=0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24 aerodrome.router=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43 stable=False                                  
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.pricing quote_token=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 diem_token=0xf4d97f2da56e8c3098f3a8d538db630a2606a024 vvv_token=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf trade_path=0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913          
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.abi erc20=True uniswap_v2_router=True aerodrome_router=True diem=True
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | metrics: starlette_exporter not installed; falling back to builtin                                                                    
broker-1  | ------------------------------ Captured log call -------------------------------                                                                                                 
broker-1  | INFO     broker.api:app.py:155 admin ui: mounted at /admin from /app/apps/control-plane
broker-1  | INFO     broker.api:app.py:293 security: admin token configured; admin endpoints require Bearer <redacted>                                                                       
broker-1  | INFO     broker.api:app.py:401 env.web3 rpc_configured=True chain_id_set=True                                                                                                    
broker-1  | INFO     broker.api:app.py:406 env.dex providers=uniswap_v3,aerodrome,uniswap_v2 v3.router=0x2626664c2603336e57b271c5c0b26f421741e481 v3.quoter=0x3d4e44eb1374240ce5f1b871ab261cd16335b76a uniswap_v2.router=0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24 aerodrome.router=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43 stable=False                                              
broker-1  | INFO     broker.api:app.py:415 env.pricing quote_token=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 diem_token=0xf4d97f2da56e8c3098f3a8d538db630a2606a024 vvv_token=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf trade_path=0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913                      
broker-1  | INFO     broker.api:app.py:422 env.abi erc20=True uniswap_v2_router=True aerodrome_router=True diem=True
broker-1  | INFO     broker.api:app.py:1525 metrics: starlette_exporter not installed; falling back to builtin                                                                               
broker-1  | _____________________ test_purchase_fractional_units_limit _____________________                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f7e310fe390>                                                                                                         
broker-1  | tmp_path = PosixPath('/tmp/pytest-of-root/pytest-0/test_purchase_fractional_units0')                                                                                             
broker-1  |                                                                                                                                                                                  
broker-1  |     def test_purchase_fractional_units_limit(monkeypatch, tmp_path):
broker-1  |         monkeypatch.setenv("QUOTES_ENABLED", "true")                                                                                                                             
broker-1  |         monkeypatch.setenv("PURCHASES_ENABLED", "true")                                                                                                                          
broker-1  |         monkeypatch.setenv("PRICE_ENGINE", "static")                                                                                                                             
broker-1  |         monkeypatch.setenv("PRICE_UNIT_ETH_WEI", str(10**15))                                                                                                                    
broker-1  |         monkeypatch.setenv("PRICE_QUOTE_TTL_SECONDS", "120")                                                                                                                     
broker-1  |         monkeypatch.setenv("BASE_RPC_URL", "http://localhost:8545")                                                                                                              
broker-1  |         monkeypatch.setenv("TREASURY_ADDRESS", "0xabc0000000000000000000000000000000000001")                                                                                     
broker-1  |         db_path = tmp_path / "buyer-fractional.db"                                                                                                                               
broker-1  |         store_path = tmp_path / "store-fractional.json"                                                                                                                          
broker-1  |         monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")                                                                                                           
broker-1  |         monkeypatch.setenv("BROKER_STORE_FILE", str(store_path))                                                                                                                 
broker-1  |         db_path.unlink(missing_ok=True)                                                                                                                                          
broker-1  |         store_path.unlink(missing_ok=True)                                                                                                                                       
broker-1  |     
broker-1  |         mod = _load_broker_app_module()                                                                                                                                          
broker-1  |                                                                                                                                                                                  
broker-1  |         buyer_wallet = "0xdef0000000000000000000000000000000000002"                                                                                                              
broker-1  |         tx_payload = {                                                                                                                                                           
broker-1  |             "to": "0xabc0000000000000000000000000000000000001",                                                                                                                  
broker-1  |             "from": buyer_wallet,                                                                                                                                                
broker-1  |             "value": hex(10**15),                                                                                                                                                
broker-1  |         }                                                                                                                                                                        
broker-1  |         tx_hash = "0x" + "4" * 64                                                                                                                                                
broker-1  |                                                                                                                                                                                  
broker-1  |         def _fake_rpc(url: str, method: str, params: list):  # noqa: ANN001                                                                                                      
broker-1  |             if method == "eth_getTransactionReceipt":                                                                                                                            
broker-1  |                 return {"status": hex(1), "blockNumber": hex(12345), "logs": []}                                                                                                 
broker-1  |             if method == "eth_getTransactionByHash":
broker-1  |                 return tx_payload                                                                                                                                                
broker-1  |             raise AssertionError(f"unexpected rpc call: {method}")                                                                                                               
broker-1  |                                                                                                                                                                                  
broker-1  |         monkeypatch.setattr(mod, "_rpc_call", _fake_rpc, raising=True)                                                                                                           
broker-1  |                                                                                                                                                                                  
broker-1  |         recorded: dict[str, object] = {}                                                                                                                                         
broker-1  |                                                                                                                                                                                  
broker-1  |         def _fake_issue(parent_key: str, label: str, consumption_limit, expires_at: str | None = None):  # noqa: ANN001                                                          
broker-1  |             recorded["consumption_limit"] = consumption_limit                                                                                                                    
broker-1  |             return {"apiKey": "sk-fractional-123", "id": "kid-2"}                                                                                                                
broker-1  |                                                                                                                                                                                  
broker-1  |         monkeypatch.setattr(mod.keys, "issue_scoped_key", _fake_issue, raising=True)                                                                                             
broker-1  |                                                                                                                                                                                  
broker-1  |         from fastapi.testclient import TestClient                                                                                                                                
broker-1  |     
broker-1  |         client = TestClient(mod.app)                                                                                                                                             
broker-1  |         quote = client.get("/v1/quotes", params={"units": 0.01, "asset": "ETH"})                                                                                                 
broker-1  | >       assert quote.status_code == 200, quote.text                                                                                                                              
broker-1  | E       AssertionError: {"detail":"Can't load plugin: sqlalchemy.dialects:sqlite"}                                                                                               
broker-1  | E       assert 400 == 200                                                                                                                                                        
broker-1  | E        +  where 400 = <Response [400 Bad Request]>.status_code                                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | tests/test_buyer_e2e.py:159: AssertionError                                                                                                                                      
broker-1  | ----------------------------- Captured stdout call -----------------------------                                                                                                 
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | admin ui: mounted at /admin from /app/apps/control-plane                                                                              
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | security: admin token configured; admin endpoints require Bearer <redacted>                                                           
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.web3 rpc_configured=True chain_id_set=True                                                                                        
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.dex providers=uniswap_v3,aerodrome,uniswap_v2 v3.router=0x2626664c2603336e57b271c5c0b26f421741e481 v3.quoter=0x3d4e44eb1374240ce5f1b871ab261cd16335b76a uniswap_v2.router=0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24 aerodrome.router=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43 stable=False                                  
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.pricing quote_token=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 diem_token=0xf4d97f2da56e8c3098f3a8d538db630a2606a024 vvv_token=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf trade_path=0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913          
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.abi erc20=True uniswap_v2_router=True aerodrome_router=True diem=True
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | metrics: starlette_exporter not installed; falling back to builtin                                                                    
broker-1  | ------------------------------ Captured log call -------------------------------                                                                                                 
broker-1  | INFO     broker.api:app.py:155 admin ui: mounted at /admin from /app/apps/control-plane                                                                                          
broker-1  | INFO     broker.api:app.py:293 security: admin token configured; admin endpoints require Bearer <redacted>                                                                       
broker-1  | INFO     broker.api:app.py:401 env.web3 rpc_configured=True chain_id_set=True                                                                                                    
broker-1  | INFO     broker.api:app.py:406 env.dex providers=uniswap_v3,aerodrome,uniswap_v2 v3.router=0x2626664c2603336e57b271c5c0b26f421741e481 v3.quoter=0x3d4e44eb1374240ce5f1b871ab261cd16335b76a uniswap_v2.router=0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24 aerodrome.router=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43 stable=False                                              
broker-1  | INFO     broker.api:app.py:415 env.pricing quote_token=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 diem_token=0xf4d97f2da56e8c3098f3a8d538db630a2606a024 vvv_token=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf trade_path=0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913                      
broker-1  | INFO     broker.api:app.py:422 env.abi erc20=True uniswap_v2_router=True aerodrome_router=True diem=True
broker-1  | INFO     broker.api:app.py:1525 metrics: starlette_exporter not installed; falling back to builtin                                                                               
broker-1  | ____________________________ test_budget_quote_path ____________________________
broker-1  |                                                                                                                                                                                  
broker-1  | monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f7e2d8bf950>                                                                                                         
broker-1  | tmp_path = PosixPath('/tmp/pytest-of-root/pytest-0/test_budget_quote_path0')                                                                                                     
broker-1  |                                                                                                                                                                                  
broker-1  |     def test_budget_quote_path(monkeypatch, tmp_path):                                                                                                                           
broker-1  |         monkeypatch.setenv("QUOTES_ENABLED", "true")                                                                                                                             
broker-1  |         monkeypatch.setenv("PRICE_ENGINE", "market")                                                                                                                             
broker-1  |         monkeypatch.setenv("PURCHASE_UNITS_KIND", "diem")                                                                                                                        
broker-1  |         monkeypatch.setenv("BASE_RPC_URL", "http://localhost:8545")
broker-1  |         monkeypatch.setenv("TREASURY_ADDRESS", "0xabc0000000000000000000000000000000000001")                                                                                     
broker-1  |         monkeypatch.setenv("PRICE_DISCOUNT_DEFAULT_BPS", "500")                                                                                                                  
broker-1  |         monkeypatch.setenv("PRICE_DISCOUNT_BPS", "500")                                                                                                                          
broker-1  |         monkeypatch.delenv("PRICE_DISCOUNT_DEFAULT", raising=False)                                                                                                              
broker-1  |         db_path = tmp_path / "budget.db"                                                                                                                                         
broker-1  |         monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")                                                                                                           
broker-1  |         db_path.unlink(missing_ok=True)                                                                                                                                          
broker-1  |                                                                                                                                                                                  
broker-1  |         mod = _load_broker_app_module()                                                                                                                                          
broker-1  |     
broker-1  |         # Force deterministic pricing without on-chain calls.                                                                                                                    
broker-1  |         engine = mod._pricing.engine                                                                                                                                             
broker-1  |                                                                                                                                                                                  
broker-1  |         assert mod._pricing._base_discount_fraction('ETH') == pytest.approx(0.05, rel=1e-9)                                                                                      
broker-1  |                                                                                                                                                                                  
broker-1  |         def _fake_prices():                                                                                                                                                      
broker-1  |             # base_unit_usd, market prices                                                                                                                                       
broker-1  |             return (200.0, {"DIEM": 200.0, "ETH": 4000.0, "USDC": 1.0})                                                                                                          
broker-1  |                                                                                                                                                                                  
broker-1  |         monkeypatch.setattr(engine, "_resolve_prices", _fake_prices, raising=True)
broker-1  |                                                                                                                                                                                  
broker-1  |         class DummyMDP:                                                                                                                                                          
broker-1  |             def prices(self, symbols):  # noqa: ANN001                                                                                                                           
broker-1  |                 return {"DIEM": 200.0, "ETH": 4000.0, "USDC": 1.0}                                                                                                               
broker-1  |                                                                                                                                                                                  
broker-1  |             def last_prices_stats(self):  # noqa: D401                                                                                                                           
broker-1  |                 return {}                                                                                                                                                        
broker-1  |                                                                                                                                                                                  
broker-1  |             def price_health(self, symbol: str, max_age: float = 180.0):  # noqa: D401                                                                                           
broker-1  |                 return {"symbol": symbol, "valid": True, "source": "prefetch", "value": 200.0}
broker-1  |                                                                                                                                                                                  
broker-1  |         monkeypatch.setattr("services.marketdata.provider.MarketDataProvider", lambda: DummyMDP(), raising=True)                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  |         from fastapi.testclient import TestClient                                                                                                                                
broker-1  |                                                                                                                                                                                  
broker-1  |         client = TestClient(mod.app)                                                                                                                                             
broker-1  |         resp = client.get("/v1/quotes", params={"budget": 10, "asset": "ETH"})                                                                                                   
broker-1  | >       assert resp.status_code == 200, resp.text                                                                                                                                
broker-1  | E       AssertionError: {"detail":"Can't load plugin: sqlalchemy.dialects:sqlite"}                                                                                               
broker-1  | E       assert 400 == 200                                                                                                                                                        
broker-1  | E        +  where 400 = <Response [400 Bad Request]>.status_code
broker-1  |                                                                                                                                                                                  
broker-1  | tests/test_buyer_e2e.py:220: AssertionError                                                                                                                                      
broker-1  | ----------------------------- Captured stdout call -----------------------------                                                                                                 
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | admin ui: mounted at /admin from /app/apps/control-plane                                                                              
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | security: admin token configured; admin endpoints require Bearer <redacted>                                                           
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.web3 rpc_configured=True chain_id_set=True                                                                                        
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.dex providers=uniswap_v3,aerodrome,uniswap_v2 v3.router=0x2626664c2603336e57b271c5c0b26f421741e481 v3.quoter=0x3d4e44eb1374240ce5f1b871ab261cd16335b76a uniswap_v2.router=0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24 aerodrome.router=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43 stable=False                                  
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.pricing quote_token=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 diem_token=0xf4d97f2da56e8c3098f3a8d538db630a2606a024 vvv_token=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf trade_path=0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913          
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.abi erc20=True uniswap_v2_router=True aerodrome_router=True diem=True
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | metrics: starlette_exporter not installed; falling back to builtin
broker-1  | ------------------------------ Captured log call -------------------------------                                                                                                 
broker-1  | INFO     broker.api:app.py:155 admin ui: mounted at /admin from /app/apps/control-plane                                                                                          
broker-1  | INFO     broker.api:app.py:293 security: admin token configured; admin endpoints require Bearer <redacted>                                                                       
broker-1  | INFO     broker.api:app.py:401 env.web3 rpc_configured=True chain_id_set=True                                                                                                    
broker-1  | INFO     broker.api:app.py:406 env.dex providers=uniswap_v3,aerodrome,uniswap_v2 v3.router=0x2626664c2603336e57b271c5c0b26f421741e481 v3.quoter=0x3d4e44eb1374240ce5f1b871ab261cd16335b76a uniswap_v2.router=0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24 aerodrome.router=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43 stable=False                                              
broker-1  | INFO     broker.api:app.py:415 env.pricing quote_token=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 diem_token=0xf4d97f2da56e8c3098f3a8d538db630a2606a024 vvv_token=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf trade_path=0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913                      
broker-1  | INFO     broker.api:app.py:422 env.abi erc20=True uniswap_v2_router=True aerodrome_router=True diem=True
broker-1  | INFO     broker.api:app.py:1525 metrics: starlette_exporter not installed; falling back to builtin                                                                               
broker-1  | __________________ test_purchase_verify_rejects_wrong_sender ___________________                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f7e2ea15090>                                                                                                         
broker-1  | tmp_path = PosixPath('/tmp/pytest-of-root/pytest-0/test_purchase_verify_rejects_w0')
broker-1  |                                                                                                                                                                                  
broker-1  |     def test_purchase_verify_rejects_wrong_sender(monkeypatch, tmp_path):                                                                                                        
broker-1  |         monkeypatch.setenv("QUOTES_ENABLED", "true")                                                                                                                             
broker-1  |         monkeypatch.setenv("PURCHASES_ENABLED", "true")                                                                                                                          
broker-1  |         monkeypatch.setenv("PRICE_ENGINE", "static")                                                                                                                             
broker-1  |         monkeypatch.setenv("PRICE_UNIT_ETH_WEI", str(10**15))                                                                                                                    
broker-1  |         monkeypatch.setenv("PRICE_QUOTE_TTL_SECONDS", "120")                                                                                                                     
broker-1  |         monkeypatch.setenv("BASE_RPC_URL", "http://localhost:8545")                                                                                                              
broker-1  |         monkeypatch.setenv("TREASURY_ADDRESS", "0xabc0000000000000000000000000000000000001")                                                                                     
broker-1  |         db_path = tmp_path / "buyer-invalid.db"                                                                                                                                  
broker-1  |         store_path = tmp_path / "store-invalid.json"                                                                                                                             
broker-1  |         monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")                                                                                                           
broker-1  |         monkeypatch.setenv("BROKER_STORE_FILE", str(store_path))                                                                                                                 
broker-1  |         db_path.unlink(missing_ok=True)                                                                                                                                          
broker-1  |         store_path.unlink(missing_ok=True)                                                                                                                                       
broker-1  |                                                                                                                                                                                  
broker-1  |         mod = _load_broker_app_module()                                                                                                                                          
broker-1  |                                                                                                                                                                                  
broker-1  |         buyer_wallet = "0xdef0000000000000000000000000000000000002"                                                                                                              
broker-1  |         sender_wallet = "0xabc0000000000000000000000000000000000003"                                                                                                             
broker-1  |         tx_payload = {                                                                                                                                                           
broker-1  |             "to": "0xabc0000000000000000000000000000000000001",                                                                                                                  
broker-1  |             "from": sender_wallet,                                                                                                                                               
broker-1  |             "value": hex(5 * 10**15),                                                                                                                                            
broker-1  |         }
broker-1  |         tx_hash = "0x" + "2" * 64                                                                                                                                                
broker-1  |                                                                                                                                                                                  
broker-1  |         def _fake_rpc(url: str, method: str, params: list):  # noqa: ANN001                                                                                                      
broker-1  |             if method == "eth_getTransactionReceipt":                                                                                                                            
broker-1  |                 return {"status": hex(1), "blockNumber": hex(12345), "logs": [], "from": sender_wallet}                                                                          
broker-1  |             if method == "eth_getTransactionByHash":                                                                                                                             
broker-1  |                 return tx_payload                                                                                                                                                
broker-1  |             raise AssertionError(f"unexpected rpc call: {method}")                                                                                                               
broker-1  |                                                                                                                                                                                  
broker-1  |         monkeypatch.setattr(mod, "_rpc_call", _fake_rpc, raising=True)                                                                                                           
broker-1  |                                                                                                                                                                                  
broker-1  |         from fastapi.testclient import TestClient
broker-1  |                                                                                                                                                                                  
broker-1  |         client = TestClient(mod.app)                                                                                                                                             
broker-1  |         quote = client.get("/v1/quotes", params={"units": 1, "asset": "ETH"})                                                                                                    
broker-1  | >       assert quote.status_code == 200, quote.text                                                                                                                              
broker-1  | E       AssertionError: {"detail":"Can't load plugin: sqlalchemy.dialects:sqlite"}                                                                                               
broker-1  | E       assert 400 == 200                                                                                                                                                        
broker-1  | E        +  where 400 = <Response [400 Bad Request]>.status_code                                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | tests/test_buyer_e2e.py:281: AssertionError                                                                                                                                      
broker-1  | ----------------------------- Captured stdout call -----------------------------                                                                                                 
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | admin ui: mounted at /admin from /app/apps/control-plane                                                                              
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | security: admin token configured; admin endpoints require Bearer <redacted>                                                           
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.web3 rpc_configured=True chain_id_set=True                                                                                        
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.dex providers=uniswap_v3,aerodrome,uniswap_v2 v3.router=0x2626664c2603336e57b271c5c0b26f421741e481 v3.quoter=0x3d4e44eb1374240ce5f1b871ab261cd16335b76a uniswap_v2.router=0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24 aerodrome.router=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43 stable=False                                  
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.pricing quote_token=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 diem_token=0xf4d97f2da56e8c3098f3a8d538db630a2606a024 vvv_token=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf trade_path=0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913          
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | env.abi erc20=True uniswap_v2_router=True aerodrome_router=True diem=True
broker-1  | 2025-10-16 17:13:17 | INFO | BROKER[api] | metrics: starlette_exporter not installed; falling back to builtin                                                                    
broker-1  | ------------------------------ Captured log call -------------------------------                                                                                                 
broker-1  | INFO     broker.api:app.py:155 admin ui: mounted at /admin from /app/apps/control-plane                                                                                          
broker-1  | INFO     broker.api:app.py:293 security: admin token configured; admin endpoints require Bearer <redacted>                                                                       
broker-1  | INFO     broker.api:app.py:401 env.web3 rpc_configured=True chain_id_set=True                                                                                                    
broker-1  | INFO     broker.api:app.py:406 env.dex providers=uniswap_v3,aerodrome,uniswap_v2 v3.router=0x2626664c2603336e57b271c5c0b26f421741e481 v3.quoter=0x3d4e44eb1374240ce5f1b871ab261cd16335b76a uniswap_v2.router=0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24 aerodrome.router=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43 stable=False                                              
broker-1  | INFO     broker.api:app.py:415 env.pricing quote_token=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 diem_token=0xf4d97f2da56e8c3098f3a8d538db630a2606a024 vvv_token=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf trade_path=0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913                      
broker-1  | INFO     broker.api:app.py:422 env.abi erc20=True uniswap_v2_router=True aerodrome_router=True diem=True
broker-1  | INFO     broker.api:app.py:1525 metrics: starlette_exporter not installed; falling back to builtin                                                                               
broker-1  | ___________________ test_purchase_verify_reuses_existing_key ___________________                                                                                                 
broker-1  | 
broker-1  | monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f7e30052410>                                                                                                         
broker-1  | tmp_path = PosixPath('/tmp/pytest-of-root/pytest-0/test_purchase_verify_reuses_ex0')                                                                                             
broker-1  |                                                                                                                                                                                  
broker-1  |     def test_purchase_verify_reuses_existing_key(monkeypatch, tmp_path):                                                                                                         
broker-1  |         monkeypatch.setenv("QUOTES_ENABLED", "true")                                                                                                                             
broker-1  |         monkeypatch.setenv("PURCHASES_ENABLED", "true")                                                                                                                          
broker-1  |         monkeypatch.setenv("PRICE_ENGINE", "static")                                                                                                                             
broker-1  |         monkeypatch.setenv("PRICE_UNIT_ETH_WEI", str(10**15))                                                                                                                    
broker-1  |         monkeypatch.setenv("PRICE_QUOTE_TTL_SECONDS", "120")                                                                                                                     
broker-1  |         monkeypatch.setenv("BASE_RPC_URL", "http://localhost:8545")                                                                                                              
broker-1  |         monkeypatch.setenv("TREASURY_ADDRESS", "0xabc0000000000000000000000000000000000001")                                                                                     
broker-1  |         db_path = tmp_path / "buyer-reuse.db"                                                                                                                                    
broker-1  |         store_path = tmp_path / "store-reuse.json"                                                                                                                               
broker-1  |         monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")
broker-1  |         monkeypatch.setenv("BROKER_STORE_FILE", str(store_path))                                                                                                                 
broker-1  |         db_path.unlink(missing_ok=True)                                                                                                                                          
broker-1  |         store_path.unlink(missing_ok=True)                                                                                                                                       
broker-1  |                                                                                                                                                                                  
broker-1  |         mod = _load_broker_app_module()                                                                                                                                          
broker-1  |                                                                                                                                                                                  
broker-1  |         buyer_wallet = "0xdef0000000000000000000000000000000000002"                                                                                                              
broker-1  |         tx_payload = {                                                                                                                                                           
broker-1  |             "to": "0xabc0000000000000000000000000000000000001",                                                                                                                  
broker-1  |             "from": buyer_wallet,                                                                                                                                                
broker-1  |             "value": hex(5 * 10**15),                                                                                                                                            
broker-1  |         }                                                                                                                                                                        
broker-1  |         tx_hash = "0x" + "3" * 64                                                                                                                                                
broker-1  |     
broker-1  |         def _fake_rpc(url: str, method: str, params: list):  # noqa: ANN001                                                                                                      
broker-1  |             if method == "eth_getTransactionReceipt":                                                                                                                            
broker-1  |                 return {"status": hex(1), "blockNumber": hex(12345), "logs": [], "from": buyer_wallet}                                                                           
broker-1  |             if method == "eth_getTransactionByHash":                                                                                                                             
broker-1  |                 return tx_payload                                                                                                                                                
broker-1  |             raise AssertionError(f"unexpected rpc call: {method}")                                                                                                               
broker-1  |                                                                                                                                                                                  
broker-1  |         monkeypatch.setattr(mod, "_rpc_call", _fake_rpc, raising=True)                                                                                                           
broker-1  |                                                                                                                                                                                  
broker-1  |         calls: list[int] = []                                                                                                                                                    
broker-1  |                                                                                                                                                                                  
broker-1  |         def _fake_issue(parent_key: str, label: str, consumption_limit: int, expires_at: str | None = None):  # noqa: ANN001                                                     
broker-1  |             calls.append(1)
broker-1  |             return {"apiKey": "sk-existing-123", "id": "kid-1"}                                                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  |         monkeypatch.setattr(mod.keys, "issue_scoped_key", _fake_issue, raising=True)                                                                                             
broker-1  |                                                                                                                                                                                  
broker-1  |         from fastapi.testclient import TestClient                                                                                                                                
broker-1  |                                                                                                                                                                                  
broker-1  |         client = TestClient(mod.app)                                                                                                                                             
broker-1  |         quote = client.get("/v1/quotes", params={"units": 1, "asset": "ETH"})                                                                                                    
broker-1  | >       assert quote.status_code == 200, quote.text                                                                                                                              
broker-1  | E       AssertionError: {"detail":"Can't load plugin: sqlalchemy.dialects:sqlite"}                                                                                               
broker-1  | E       assert 400 == 200                                                                                                                                                        
broker-1  | E        +  where 400 = <Response [400 Bad Request]>.status_code                                                                                                                 
broker-1  | 
broker-1  | tests/test_buyer_e2e.py:345: AssertionError                                                                                                                                      
broker-1  | ----------------------------- Captured stdout call -----------------------------                                                                                                 
broker-1  | 2025-10-16 17:13:18 | INFO | BROKER[api] | admin ui: mounted at /admin from /app/apps/control-plane                                                                              
broker-1  | 2025-10-16 17:13:18 | INFO | BROKER[api] | security: admin token configured; admin endpoints require Bearer <redacted>                                                           
broker-1  | 2025-10-16 17:13:18 | INFO | BROKER[api] | env.web3 rpc_configured=True chain_id_set=True                                                                                        
broker-1  | 2025-10-16 17:13:18 | INFO | BROKER[api] | env.dex providers=uniswap_v3,aerodrome,uniswap_v2 v3.router=0x2626664c2603336e57b271c5c0b26f421741e481 v3.quoter=0x3d4e44eb1374240ce5f1b871ab261cd16335b76a uniswap_v2.router=0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24 aerodrome.router=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43 stable=False                                  
broker-1  | 2025-10-16 17:13:18 | INFO | BROKER[api] | env.pricing quote_token=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 diem_token=0xf4d97f2da56e8c3098f3a8d538db630a2606a024 vvv_token=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf trade_path=0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913          
broker-1  | 2025-10-16 17:13:18 | INFO | BROKER[api] | env.abi erc20=True uniswap_v2_router=True aerodrome_router=True diem=True
broker-1  | 2025-10-16 17:13:18 | INFO | BROKER[api] | metrics: starlette_exporter not installed; falling back to builtin                                                                    
broker-1  | ------------------------------ Captured log call -------------------------------                                                                                                 
broker-1  | INFO     broker.api:app.py:155 admin ui: mounted at /admin from /app/apps/control-plane                                                                                          
broker-1  | INFO     broker.api:app.py:293 security: admin token configured; admin endpoints require Bearer <redacted>                                                                       
broker-1  | INFO     broker.api:app.py:401 env.web3 rpc_configured=True chain_id_set=True                                                                                                    
broker-1  | INFO     broker.api:app.py:406 env.dex providers=uniswap_v3,aerodrome,uniswap_v2 v3.router=0x2626664c2603336e57b271c5c0b26f421741e481 v3.quoter=0x3d4e44eb1374240ce5f1b871ab261cd16335b76a uniswap_v2.router=0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24 aerodrome.router=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43 stable=False                                              
broker-1  | INFO     broker.api:app.py:415 env.pricing quote_token=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 diem_token=0xf4d97f2da56e8c3098f3a8d538db630a2606a024 vvv_token=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf trade_path=0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913                      
broker-1  | INFO     broker.api:app.py:422 env.abi erc20=True uniswap_v2_router=True aerodrome_router=True diem=True
broker-1  | INFO     broker.api:app.py:1525 metrics: starlette_exporter not installed; falling back to builtin                                                                               
broker-1  | __________________ test_diem_controller_thresholds_mint_sell ___________________                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f7e2fee0250>                                                                                                         
broker-1  |                                                                                                                                                                                  
broker-1  |     def test_diem_controller_thresholds_mint_sell(monkeypatch):                                                                                                                  
broker-1  |         # Configure threshold so that premium triggers mint_sell                                                                                                                 
broker-1  |         os.environ["DIEM_FAIR_ALPHA"] = "0.2"  # ignored by stubbed fair_value_per_diem                                                                                          
broker-1  |         os.environ["DIEM_PREMIUM_THRESHOLD"] = "1.10"                                                                                                                            
broker-1  |                                                                                                                                                                                  
broker-1  |         # Stub MarketDataProvider.prices to return DIEM price                                                                                                                    
broker-1  |         md_mod = import_module("services.marketdata.provider")
broker-1  |                                                                                                                                                                                  
broker-1  |         class FakeMD:                                                                                                                                                            
broker-1  |             def prices(self, symbols):  # noqa: ANN001                                                                                                                           
broker-1  |                 return {"DIEM": 1.2}                                                                                                                                             
broker-1  |                                                                                                                                                                                  
broker-1  |         monkeypatch.setattr(md_mod, "MarketDataProvider", FakeMD, raising=True)                                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  |         # Stub fair_value_per_diem so that fair_per_day = 1.0                                                                                                                    
broker-1  |         diem_mod = import_module("libs.pricing.diem")                                                                                                                            
broker-1  |         monkeypatch.setattr(diem_mod, "fair_value_per_diem", lambda alpha: 365.0, raising=True)                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  | >       nodes = import_module("graph.langgraph.nodes")                                                                                                                           
broker-1  |                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
broker-1  |                                                                                                                                                                                  
broker-1  | tests/test_diem_thresholds.py:31:                                                                                                                                                
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  | /usr/local/lib/python3.11/importlib/__init__.py:126: in import_module                                                                                                            
broker-1  |     return _bootstrap._gcd_import(name[level:], package, level)                                                                                                                  
broker-1  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^                                                                                                                  
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked                                                                                                                   
broker-1  |     ???
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked                                                                                                                   
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  | name = 'graph', import_ = <function _gcd_import at 0x7f7e358c7d80>                                                                                                               
broker-1  |                                                                                                                                                                                  
broker-1  | >   ???                                                                                                                                                                          
broker-1  | E   ModuleNotFoundError: No module named 'graph'                                                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | <frozen importlib._bootstrap>:1140: ModuleNotFoundError                                                                                                                          
broker-1  | _____________________ test_diem_controller_thresholds_hold _____________________                                                                                                 
broker-1  | 
broker-1  | monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f7e2ff18210>                                                                                                         
broker-1  |                                                                                                                                                                                  
broker-1  |     def test_diem_controller_thresholds_hold(monkeypatch):                                                                                                                       
broker-1  |         os.environ["DIEM_FAIR_ALPHA"] = "0.2"                                                                                                                                    
broker-1  |         os.environ["DIEM_PREMIUM_THRESHOLD"] = "1.10"                                                                                                                            
broker-1  |                                                                                                                                                                                  
broker-1  |         md_mod = import_module("services.marketdata.provider")                                                                                                                   
broker-1  |                                                                                                                                                                                  
broker-1  |         class FakeMD:                                                                                                                                                            
broker-1  |             def prices(self, symbols):  # noqa: ANN001                                                                                                                           
broker-1  |                 return {"DIEM": 1.05}                                                                                                                                            
broker-1  |     
broker-1  |         monkeypatch.setattr(md_mod, "MarketDataProvider", FakeMD, raising=True)                                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  |         diem_mod = import_module("libs.pricing.diem")                                                                                                                            
broker-1  |         monkeypatch.setattr(diem_mod, "fair_value_per_diem", lambda alpha: 365.0, raising=True)                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  | >       nodes = import_module("graph.langgraph.nodes")                                                                                                                           
broker-1  |                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^                                                                                                                           
broker-1  |                                                                                                                                                                                  
broker-1  | tests/test_diem_thresholds.py:52:                                                                                                                                                
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  | /usr/local/lib/python3.11/importlib/__init__.py:126: in import_module                                                                                                            
broker-1  |     return _bootstrap._gcd_import(name[level:], package, level)                                                                                                                  
broker-1  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked                                                                                                                   
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked                                                                                                                   
broker-1  |     ???
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  | name = 'graph', import_ = <function _gcd_import at 0x7f7e358c7d80>                                                                                                               
broker-1  |                                                                                                                                                                                  
broker-1  | >   ???                                                                                                                                                                          
broker-1  | E   ModuleNotFoundError: No module named 'graph'                                                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | <frozen importlib._bootstrap>:1140: ModuleNotFoundError
broker-1  | _________________________________ test_imports _________________________________                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  |     def test_imports():                                                                                                                                                          
broker-1  |         modules = [                                                                                                                                                              
broker-1  |             "libs.telemetry.logger",                                                                                                                                             
broker-1  |             "libs.venice_sdk.client",                                                                                                                                            
broker-1  |             "services.wallet.provider",                                                                                                                                          
broker-1  |             "services.staking.client",                                                                                                                                           
broker-1  |             "services.venice_keys.manager",                                                                                                                                      
broker-1  |             "libs.dex.providers",                                                                                                                                                
broker-1  |             "agents.quorum.core",                                                                                                                                                
broker-1  |             "graph.workflows.revenue_streams",                                                                                                                                   
broker-1  |         ]                                                                                                                                                                        
broker-1  |         for m in modules:
broker-1  | >           importlib.import_module(m)                                                                                                                                           
broker-1  |                                                                                                                                                                                  
broker-1  | tests/test_imports.py:16:                                                                                                                                                        
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  | /usr/local/lib/python3.11/importlib/__init__.py:126: in import_module                                                                                                            
broker-1  |     return _bootstrap._gcd_import(name[level:], package, level)                                                                                                                  
broker-1  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^                                                                                                                  
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked                                                                                                                   
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  | name = 'graph', import_ = <function _gcd_import at 0x7f7e358c7d80>                                                                                                               
broker-1  |                                                                                                                                                                                  
broker-1  | >   ???                                                                                                                                                                          
broker-1  | E   ModuleNotFoundError: No module named 'graph'                                                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | <frozen importlib._bootstrap>:1140: ModuleNotFoundError                                                                                                                          
broker-1  | ________________________ test_suggest_routes_for_tokens ________________________                                                                                                 
broker-1  | 
broker-1  | tmp_path = PosixPath('/tmp/pytest-of-root/pytest-0/test_suggest_routes_for_tokens0')                                                                                             
broker-1  | monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f7e2cced6d0>                                                                                                         
broker-1  |                                                                                                                                                                                  
broker-1  |     def test_suggest_routes_for_tokens(tmp_path, monkeypatch):                                                                                                                   
broker-1  |         db_path = tmp_path / "pools.db"                                                                                                                                          
broker-1  |         monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")                                                                                                           
broker-1  | >       create_db_and_tables()                                                                                                                                                   
broker-1  |                                                                                                                                                                                  
broker-1  | tests/test_marketdata_pools.py:38:                                                                                                                                               
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  | db/session.py:256: in create_db_and_tables
broker-1  |     engine = get_engine()                                                                                                                                                        
broker-1  |              ^^^^^^^^^^^^                                                                                                                                                        
broker-1  | db/session.py:242: in get_engine                                                                                                                                                 
broker-1  |     return _call_engine_factory(url, **kwargs)                                                                                                                                   
broker-1  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^                                                                                                                                   
broker-1  | db/session.py:174: in _call_engine_factory                                                                                                                                       
broker-1  |     engine = engine_factory(target_url, **kwargs)  # type: ignore[misc]                                                                                                          
broker-1  |              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^                                                                                                                                
broker-1  | <string>:2: in create_engine                                                                                                                                                     
broker-1  |     ???                                                                                                                                                                          
broker-1  | /usr/local/lib/python3.11/site-packages/sqlalchemy/util/deprecations.py:281: in warned                                                                                           
broker-1  |     return fn(*args, **kwargs)  # type: ignore[no-any-return]                                                                                                                    
broker-1  |            ^^^^^^^^^^^^^^^^^^^
broker-1  | /usr/local/lib/python3.11/site-packages/sqlalchemy/engine/create.py:568: in create_engine                                                                                        
broker-1  |     entrypoint = u._get_entrypoint()                                                                                                                                             
broker-1  |                  ^^^^^^^^^^^^^^^^^^^                                                                                                                                             
broker-1  | /usr/local/lib/python3.11/site-packages/sqlalchemy/engine/url.py:772: in _get_entrypoint                                                                                         
broker-1  |     cls = registry.load(name)                                                                                                                                                    
broker-1  |           ^^^^^^^^^^^^^^^^^^^                                                                                                                                                    
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  | self = <sqlalchemy.util.langhelpers.PluginLoader object at 0x7f7e326214d0>                                                                                                       
broker-1  | name = 'sqlite'                                                                                                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  |     def load(self, name: str) -> Any:                                                                                                                                            
broker-1  |         if name in self.impls:
broker-1  |             return self.impls[name]()                                                                                                                                            
broker-1  |                                                                                                                                                                                  
broker-1  |         if self.auto_fn:                                                                                                                                                         
broker-1  |             loader = self.auto_fn(name)                                                                                                                                          
broker-1  |             if loader:                                                                                                                                                           
broker-1  |                 self.impls[name] = loader                                                                                                                                        
broker-1  |                 return loader()                                                                                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  |         for impl in compat.importlib_metadata_get(self.group):                                                                                                                   
broker-1  |             if impl.name == name:                                                                                                                                                
broker-1  |                 self.impls[name] = impl.load                                                                                                                                     
broker-1  |                 return impl.load()                                                                                                                                               
broker-1  |                                                                                                                                                                                  
broker-1  | >       raise exc.NoSuchModuleError(                                                                                                                                             
broker-1  |             "Can't load plugin: %s:%s" % (self.group, name)
broker-1  |         )                                                                                                                                                                        
broker-1  | E       sqlalchemy.exc.NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:sqlite                                                                                          
broker-1  |                                                                                                                                                                                  
broker-1  | /usr/local/lib/python3.11/site-packages/sqlalchemy/util/langhelpers.py:453: NoSuchModuleError                                                                                    
broker-1  | _____________ test_orchestrator_passes_portfolio_inventory_to_arbi _____________                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f7e2fe6ee10>                                                                                                         
broker-1  |                                                                                                                                                                                  
broker-1  |     def test_orchestrator_passes_portfolio_inventory_to_arbi(monkeypatch):                                                                                                       
broker-1  | >       orch_mod = import_module("graph.workflows.orchestrator")                                                                                                                 
broker-1  |                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
broker-1  |                                                                                                                                                                                  
broker-1  | tests/test_orchestrator_portfolio_cap.py:8:                                                                                                                                      
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  | /usr/local/lib/python3.11/importlib/__init__.py:126: in import_module                                                                                                            
broker-1  |     return _bootstrap._gcd_import(name[level:], package, level)                                                                                                                  
broker-1  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^                                                                                                                  
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked                                                                                                                   
broker-1  |     ???
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked                                                                                                                   
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  | name = 'graph', import_ = <function _gcd_import at 0x7f7e358c7d80>                                                                                                               
broker-1  |                                                                                                                                                                                  
broker-1  | >   ???                                                                                                                                                                          
broker-1  | E   ModuleNotFoundError: No module named 'graph'                                                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | <frozen importlib._bootstrap>:1140: ModuleNotFoundError
broker-1  | _________________ test_orchestrator_passes_util_and_volatility _________________                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f7e2ff0ffd0>                                                                                                         
broker-1  |                                                                                                                                                                                  
broker-1  |     def test_orchestrator_passes_util_and_volatility(monkeypatch):                                                                                                               
broker-1  | >       orch_mod = import_module("graph.workflows.orchestrator")                                                                                                                 
broker-1  |                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^                                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | tests/test_orchestrator_util_vol.py:7:                                                                                                                                           
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  | /usr/local/lib/python3.11/importlib/__init__.py:126: in import_module                                                                                                            
broker-1  |     return _bootstrap._gcd_import(name[level:], package, level)                                                                                                                  
broker-1  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^                                                                                                                  
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked                                                                                                                   
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  | name = 'graph', import_ = <function _gcd_import at 0x7f7e358c7d80>                                                                                                               
broker-1  |                                                                                                                                                                                  
broker-1  | >   ???                                                                                                                                                                          
broker-1  | E   ModuleNotFoundError: No module named 'graph'
broker-1  |                                                                                                                                                                                  
broker-1  | <frozen importlib._bootstrap>:1140: ModuleNotFoundError                                                                                                                          
broker-1  | ______________ test_single_loop_cycle_includes_stake_and_capacity ______________                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f7e3006d7d0>                                                                                                         
broker-1  |                                                                                                                                                                                  
broker-1  |     def test_single_loop_cycle_includes_stake_and_capacity(monkeypatch):                                                                                                         
broker-1  | >       orch_mod = import_module("graph.workflows.orchestrator")                                                                                                                 
broker-1  |                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^                                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | tests/test_single_loop_orchestrator.py:7:                                                                                                                                        
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  | /usr/local/lib/python3.11/importlib/__init__.py:126: in import_module
broker-1  |     return _bootstrap._gcd_import(name[level:], package, level)                                                                                                                  
broker-1  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^                                                                                                                  
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked                                                                                                                   
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked                                                                                                                   
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  | name = 'graph', import_ = <function _gcd_import at 0x7f7e358c7d80>                                                                                                               
broker-1  |                                                                                                                                                                                  
broker-1  | >   ???                                                                                                                                                                          
broker-1  | E   ModuleNotFoundError: No module named 'graph'                                                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | <frozen importlib._bootstrap>:1140: ModuleNotFoundError                                                                                                                          
broker-1  | ____________________ test_single_loop_quorum_blocks_actions ____________________                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f7e322f7510>
broker-1  |                                                                                                                                                                                  
broker-1  |     def test_single_loop_quorum_blocks_actions(monkeypatch):                                                                                                                     
broker-1  | >       orch_mod = import_module("graph.workflows.orchestrator")                                                                                                                 
broker-1  |                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^                                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | tests/test_single_loop_orchestrator.py:84:                                                                                                                                       
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  | /usr/local/lib/python3.11/importlib/__init__.py:126: in import_module                                                                                                            
broker-1  |     return _bootstrap._gcd_import(name[level:], package, level)
broker-1  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^                                                                                                                  
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked                                                                                                                   
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked                                                                                                                   
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load
broker-1  |     ???                                                                                                                                                                          
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  | name = 'graph', import_ = <function _gcd_import at 0x7f7e358c7d80>                                                                                                               
broker-1  |                                                                                                                                                                                  
broker-1  | >   ???                                                                                                                                                                          
broker-1  | E   ModuleNotFoundError: No module named 'graph'                                                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | <frozen importlib._bootstrap>:1140: ModuleNotFoundError                                                                                                                          
broker-1  | ________________ test_single_loop_treasurer_and_listen_interval ________________
broker-1  |                                                                                                                                                                                  
broker-1  | monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f7e315c2dd0>                                                                                                         
broker-1  |                                                                                                                                                                                  
broker-1  |     def test_single_loop_treasurer_and_listen_interval(monkeypatch):                                                                                                             
broker-1  | >       orch_mod = import_module("graph.workflows.orchestrator")                                                                                                                 
broker-1  |                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^                                                                                                                 
broker-1  |                                                                                                                                                                                  
broker-1  | tests/test_single_loop_orchestrator.py:153:                                                                                                                                      
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  | /usr/local/lib/python3.11/importlib/__init__.py:126: in import_module
broker-1  |     return _bootstrap._gcd_import(name[level:], package, level)                                                                                                                  
broker-1  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^                                                                                                                  
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked                                                                                                                   
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1126: in _find_and_load_unlocked                                                                                                                   
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:241: in _call_with_frames_removed                                                                                                                  
broker-1  |     ???                                                                                                                                                                          
broker-1  | <frozen importlib._bootstrap>:1204: in _gcd_import                                                                                                                               
broker-1  |     ???
broker-1  | <frozen importlib._bootstrap>:1176: in _find_and_load                                                                                                                            
broker-1  |     ???                                                                                                                                                                          
broker-1  | _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _                                                                                                  
broker-1  |                                                                                                                                                                                  
broker-1  | name = 'graph', import_ = <function _gcd_import at 0x7f7e358c7d80>                                                                                                               
broker-1  |                                                                                                                                                                                  
broker-1  | >   ???                                                                                                                                                                          
broker-1  | E   ModuleNotFoundError: No module named 'graph'
broker-1  |                                                                                                                                                                                  
broker-1  | <frozen importlib._bootstrap>:1140: ModuleNotFoundError                                                                                                                          
broker-1  | =============================== warnings summary ===============================                                                                                                 
broker-1  | ../usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644                                                                                                                   
broker-1  | tests/test_admin_limits.py::test_admin_limits_auth_and_defaults                                                                                                                  
broker-1  |   /usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644: SAWarning: This declarative base already contains a class with the same class name and module name as db.models.Tenant, and will be replaced in the string-lookup table.                                                                                                                                      
broker-1  |     DeclarativeMeta.__init__(cls, classname, bases, dict_, **kw)
broker-1  |                                                                                                                                                                                  
broker-1  | ../usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644                                                                                                                   
broker-1  | tests/test_admin_limits.py::test_admin_limits_auth_and_defaults
broker-1  |   /usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644: SAWarning: This declarative base already contains a class with the same class name and module name as db.models.Key, and will be replaced in the string-lookup table.                                                                                                                                         
broker-1  |     DeclarativeMeta.__init__(cls, classname, bases, dict_, **kw)
broker-1  |                                                                                                                                                                                  
broker-1  | ../usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644                                                                                                                   
broker-1  | tests/test_admin_limits.py::test_admin_limits_auth_and_defaults                                                                                                                  
broker-1  |   /usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644: SAWarning: This declarative base already contains a class with the same class name and module name as db.models.Plan, and will be replaced in the string-lookup table.                                                                                                                                        
broker-1  |     DeclarativeMeta.__init__(cls, classname, bases, dict_, **kw)
broker-1  |                                                                                                                                                                                  
broker-1  | ../usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644                                                                                                                   
broker-1  | tests/test_admin_limits.py::test_admin_limits_auth_and_defaults                                                                                                                  
broker-1  |   /usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644: SAWarning: This declarative base already contains a class with the same class name and module name as db.models.Counter, and will be replaced in the string-lookup table.                                                                                                                                     
broker-1  |     DeclarativeMeta.__init__(cls, classname, bases, dict_, **kw)
broker-1  |                                                                                                                                                                                  
broker-1  | ../usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644                                                                                                                   
broker-1  | tests/test_admin_limits.py::test_admin_limits_auth_and_defaults                                                                                                                  
broker-1  |   /usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644: SAWarning: This declarative base already contains a class with the same class name and module name as db.models.Quote, and will be replaced in the string-lookup table.                                                                                                                                       
broker-1  |     DeclarativeMeta.__init__(cls, classname, bases, dict_, **kw)
broker-1  |                                                                                                                                                                                  
broker-1  | ../usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644                                                                                                                   
broker-1  | tests/test_admin_limits.py::test_admin_limits_auth_and_defaults                                                                                                                  
broker-1  |   /usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644: SAWarning: This declarative base already contains a class with the same class name and module name as db.models.Purchase, and will be replaced in the string-lookup table.                                                                                                                                    
broker-1  |     DeclarativeMeta.__init__(cls, classname, bases, dict_, **kw)
broker-1  |                                                                                                                                                                                  
broker-1  | ../usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644                                                                                                                   
broker-1  | tests/test_admin_limits.py::test_admin_limits_auth_and_defaults                                                                                                                  
broker-1  |   /usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644: SAWarning: This declarative base already contains a class with the same class name and module name as db.models.AssetToken, and will be replaced in the string-lookup table.                                                                                                                                  
broker-1  |     DeclarativeMeta.__init__(cls, classname, bases, dict_, **kw)
broker-1  |                                                                                                                                                                                  
broker-1  | ../usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644                                                                                                                   
broker-1  | tests/test_admin_limits.py::test_admin_limits_auth_and_defaults                                                                                                                  
broker-1  |   /usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644: SAWarning: This declarative base already contains a class with the same class name and module name as db.models.TokenSnapshot, and will be replaced in the string-lookup table.                                                                                                                               
broker-1  |     DeclarativeMeta.__init__(cls, classname, bases, dict_, **kw)
broker-1  |                                                                                                                                                                                  
broker-1  | ../usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644                                                                                                                   
broker-1  | tests/test_admin_limits.py::test_admin_limits_auth_and_defaults                                                                                                                  
broker-1  |   /usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644: SAWarning: This declarative base already contains a class with the same class name and module name as db.models.Decision, and will be replaced in the string-lookup table.                                                                                                                                    
broker-1  |     DeclarativeMeta.__init__(cls, classname, bases, dict_, **kw)
broker-1  |                                                                                                                                                                                  
broker-1  | ../usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644                                                                                                                   
broker-1  | tests/test_admin_limits.py::test_admin_limits_auth_and_defaults                                                                                                                  
broker-1  |   /usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644: SAWarning: This declarative base already contains a class with the same class name and module name as db.models.PriceTick, and will be replaced in the string-lookup table.                                                                                                                                   
broker-1  |     DeclarativeMeta.__init__(cls, classname, bases, dict_, **kw)
broker-1  |                                                                                                                                                                                  
broker-1  | ../usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644                                                                                                                   
broker-1  | tests/test_admin_limits.py::test_admin_limits_auth_and_defaults                                                                                                                  
broker-1  |   /usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644: SAWarning: This declarative base already contains a class with the same class name and module name as db.models.DexFactoryCursor, and will be replaced in the string-lookup table.                                                                                                                            
broker-1  |     DeclarativeMeta.__init__(cls, classname, bases, dict_, **kw)
broker-1  |                                                                                                                                                                                  
broker-1  | ../usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644
broker-1  | tests/test_admin_limits.py::test_admin_limits_auth_and_defaults                                                                                                                  
broker-1  |   /usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644: SAWarning: This declarative base already contains a class with the same class name and module name as db.models.DexPool, and will be replaced in the string-lookup table.                                                                                                                                     
broker-1  |     DeclarativeMeta.__init__(cls, classname, bases, dict_, **kw)
broker-1  |                                                                                                                                                                                  
broker-1  | ../usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644                                                                                                                   
broker-1  | tests/test_admin_limits.py::test_admin_limits_auth_and_defaults                                                                                                                  
broker-1  |   /usr/local/lib/python3.11/site-packages/sqlmodel/main.py:644: SAWarning: This declarative base already contains a class with the same class name and module name as db.models.Bid, and will be replaced in the string-lookup table.                                                                                                                                         
broker-1  |     DeclarativeMeta.__init__(cls, classname, bases, dict_, **kw)
broker-1  |                                                                                                                                                                                  
broker-1  | ../usr/local/lib/python3.11/site-packages/websockets/legacy/__init__.py:6                                                                                                        
broker-1  |   /usr/local/lib/python3.11/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html for upgrade instructions                                                                                                                                                   
broker-1  |     warnings.warn(  # deprecated in 14.0 - 2024-11-09
broker-1  |                                                                                                                                                                                  
broker-1  | -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html                                                                                                          
broker-1  | =========================== short test summary info ============================                                                                                                 
broker-1  | FAILED tests/test_broker_limits.py::test_rate_limit_resets_without_redis - as...                                                                                                 
broker-1  | FAILED tests/test_buyer_e2e.py::test_buyer_lifecycle_quote_verify_subkey - As...                                                                                                 
broker-1  | FAILED tests/test_buyer_e2e.py::test_purchase_fractional_units_limit - Assert...                                                                                                 
broker-1  | FAILED tests/test_buyer_e2e.py::test_budget_quote_path - AssertionError: {"de...                                                                                                 
broker-1  | FAILED tests/test_buyer_e2e.py::test_purchase_verify_rejects_wrong_sender - A...                                                                                                 
broker-1  | FAILED tests/test_buyer_e2e.py::test_purchase_verify_reuses_existing_key - As...                                                                                                 
broker-1  | FAILED tests/test_diem_thresholds.py::test_diem_controller_thresholds_mint_sell                                                                                                  
broker-1  | FAILED tests/test_diem_thresholds.py::test_diem_controller_thresholds_hold - ...
broker-1  | FAILED tests/test_imports.py::test_imports - ModuleNotFoundError: No module n...                                                                                                 
broker-1  | FAILED tests/test_marketdata_pools.py::test_suggest_routes_for_tokens - sqlal...                                                                                                 
broker-1  | FAILED tests/test_orchestrator_portfolio_cap.py::test_orchestrator_passes_portfolio_inventory_to_arbi                                                                            
broker-1  | FAILED tests/test_orchestrator_util_vol.py::test_orchestrator_passes_util_and_volatility                                                                                         
broker-1  | FAILED tests/test_single_loop_orchestrator.py::test_single_loop_cycle_includes_stake_and_capacity                                                                                
broker-1  | FAILED tests/test_single_loop_orchestrator.py::test_single_loop_quorum_blocks_actions                                                                                            
broker-1  | FAILED tests/test_single_loop_orchestrator.py::test_single_loop_treasurer_and_listen_interval                                                                                    
broker-1  | 15 failed, 77 passed, 1 skipped, 27 warnings in 290.44s (0:04:50)                                                                                                                
broker-1 exited with code 1
(base) joelwk@joel-main:/mnt/c/Users/jwkon/Documents/datascience-projects/venice$ 

v View in Docker Desktop   o View Config   w Enable Watch