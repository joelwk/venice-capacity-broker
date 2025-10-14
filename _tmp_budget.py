import importlib.util
import os
import pathlib

from fastapi.testclient import TestClient

os.environ['QUOTES_ENABLED'] = 'true'
os.environ['PRICE_ENGINE'] = 'market'
os.environ['PURCHASE_UNITS_KIND'] = 'diem'
os.environ['BASE_RPC_URL'] = 'http://localhost:8545'
os.environ['TREASURY_ADDRESS'] = '0xabc0000000000000000000000000000000000001'

app_path = pathlib.Path('apps/broker-api/app.py').resolve()
spec = importlib.util.spec_from_file_location('budget_module', str(app_path))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

engine = mod._pricing.engine
print('engine', engine)

def _fake_prices():
    return (200.0, {'DIEM': 200.0, 'ETH': 4000.0, 'USDC': 1.0})

engine._resolve_prices = _fake_prices

client = TestClient(mod.app)
resp = client.get('/v1/quotes', params={'budget': 10, 'asset': 'ETH'})
print(resp.status_code, resp.text)
