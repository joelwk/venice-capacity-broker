import os
import pytest

DEFAULT_TOKEN_ENV = {
	'DIEM_TOKEN_ADDRESS': '0xf4d97f2da56e8c3098f3a8d538db630a2606a024',
	'VVV_TOKEN_ADDRESS': '0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf',
	'QUOTE_TOKEN_ADDRESS': '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913',
	'WETH_ADDRESS': '0x4200000000000000000000000000000000000006',
}

# Disable console capture during tests to avoid conflicts with pytest's capture mechanism
os.environ.setdefault('LOG_CAPTURE_CONSOLE', '0')

os.environ.setdefault('BROKER_REQUIRE_ADMIN_TOKEN', 'false')
os.environ.setdefault('BROKER_ADMIN_TOKEN', 'test-admin')
os.environ.setdefault('VENICE_PARENT_KEY', 'parent-test')
os.environ.setdefault('ALLOW_JSON_FALLBACK', '1')
os.environ.setdefault('ALLOW_SQLITE_FALLBACK', '1')
os.environ.setdefault('ALLOW_INMEMORY_KV_FALLBACK', '1')

# Ensure developer-specific database/KV env vars don't leak into tests.
for _unset in (
    'SQL_DATABASE_URL',
    'DATABASE_URL',
    'POSTGRES_HOST',
    'POSTGRES_USER',
    'POSTGRES_PASSWORD',
    'POSTGRES_DB',
    'KV_URL',
    'REPLIT_DB_URL',
    'REDIS_URL',
    'KV_REDIS_URL',
):
    os.environ.pop(_unset, None)


@pytest.fixture(autouse=True)
def _reset_env_vars(monkeypatch):
	for name in (
	    'SQL_DATABASE_URL',
	    'DATABASE_URL',
	    'POSTGRES_HOST',
	    'POSTGRES_USER',
	    'POSTGRES_PASSWORD',
	    'POSTGRES_DB',
	    'KV_URL',
	    'REPLIT_DB_URL',
	    'REDIS_URL',
	    'KV_REDIS_URL',
	):
		monkeypatch.delenv(name, raising=False)
	for key, value in DEFAULT_TOKEN_ENV.items():
		monkeypatch.setenv(key, value)
