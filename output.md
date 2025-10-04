~/workspace$ uv run python apps/cli/main.py market:pools:watch --once
pool watcher starting: factories=uniswap_v2,aerodrome_vol,aerodrome_stable,uniswap_v3 interval=120s backfill=5000 span=2000
uniswap_v2 discovered 1225 new pools (scanned 42145 blocks)
~/workspace$ uv run python apps/cli/main.py startup:probe
DEX verify (chain 8453)
Path: 0xf4d97f2da56e8c3098f3a8d538db630a2606a024 -> 0x4200000000000000000000000000000000000006 -> 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913
Fees: -, -

Hop 1: DIEM -> WETH
 - UniswapV2: pair=0xe638245fcb1a175411674bffce8d8ce7208e3c84 reserves=235,4750 ts=1755883017
 - Aerodrome Volatile: (no pair)
 - Aerodrome Stable: (no pair)

Hop 2: WETH -> USDC
 - UniswapV2: pair=0x88a43bbdf9d098eec7bceda4e2494615dfd9bb9c reserves=795497726394323206425,3563384655846 ts=1759577119
 - Aerodrome Volatile: (no pair)
 - Aerodrome Stable: (no pair)

Cache by_tokens:
 - DIEM->0x4200000000000000000000000000000000000006: pair=0xe638245fcb1a175411674bffce8d8ce7208e3c84 has_reserves=True
 - 0x4200000000000000000000000000000000000006->USDC: pair=0x88a43bbdf9d098eec7bceda4e2494615dfd9bb9c has_reserves=True
marketdata warm cache thread started
trade path verification empty
~/workspace$ uv run python scripts/print_env.py 
{
  "web3": {
    "RPC_URL": "https://mainnet.base.org",
    "BASE_RPC_URL": "https://mainnet.base.org",
    "BASE_CHAIN_ID": "8453"
  },
  "dex": {
    "DEX_PROVIDERS": "[{\"name\":\"uniswap_v3\",\"router\":\"0xE592427A0AEce92De3Edee1F18E0157C05861564\",\"quoter\":\"0x61fFE014bA17989E743c5F6cB21bF9697530B21e\",\"default_fee\":3000},{\"name\":\"aerodrome\",\"router\":\"0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43\",\"stable\":false},{\"name\":\"uniswap_v2\",\"router\":\"0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24\"}]",
    "UNISWAP_V2_ROUTER_ADDRESS": "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24",
    "AERODROME_ROUTER_ADDRESS": "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43",
    "AERODROME_STABLE": "false",
    "UNISWAP_V2_FACTORY_ADDRESS": "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
    "AERODROME_FACTORY_VOLATILE": "0x420DD381b31aEf6683db6B902084cB0FFECe40Da",
    "AERODROME_FACTORY_STABLE": "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A"
  },
  "tokens": {
    "QUOTE_TOKEN_ADDRESS": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "DIEM_TOKEN_ADDRESS": "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
    "VVV_TOKEN_ADDRESS": "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",
    "WETH_ADDRESS": "0x4200000000000000000000000000000000000006",
    "BRIDGE_TOKEN_ADDRESS": "0x4200000000000000000000000000000000000006"
  },
  "pricing": {
    "TRADE_PATH": "0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fcd6edb6e08f4c7c32d4f71b54bdA02913",
    "RISK_MAX_SLIPPAGE_BPS": "150",
    "RISK_MAX_POOL_TAKE_BPS": "25"
  },
  "wallet": {
    "WALLET_PROVIDER": "eth_account",
    "NETWORK_ID": "base-mainnet",
    "PAYMASTER_URL": "",
    "ETH_PRIVATE_KEY": "a060\u2026ce53",
    "OWNER": null
  },
  "cdp": {
    "CDP_API_KEY_ID": "7817\u2026d83e",
    "CDP_API_KEY_SECRET": "ecDs\u2026nQ==",
    "CDP_WALLET_SECRET": "MIGH\u2026ZOXZ"
  },
  "venice": {
    "VENICE_API_BASE_URL": "https://api.venice.ai/api/v1",
    "VENICE_API_KEY": "59ex\u2026yFF_"
  },
  "secrets": {
    "CDP_API_KEY_ID": "7817\u2026d83e",
    "CDP_API_KEY_SECRET": "ecDs\u2026nQ==",
    "CDP_WALLET_SECRET": "MIGH\u2026ZOXZ",
    "ETH_PRIVATE_KEY": "a060\u2026ce53",
    "OWNER": null
  },
  "abi": {
    "erc20.json": true,
    "uniswap_v2_router.json": true,
    "aerodrome_router.json": true,
    "diem.json": true
  },
  "notes": {
    "loaded_dotenv": true,
    "cwd": "/home/runner/workspace",
    "repo_root": "/home/runner/workspace"
  }
}
~/workspace$ 