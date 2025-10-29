# Etherscan v2 Reference (Base)

This guide documents how the repo uses the Etherscan v2 API to verify routers, discover pools, and fetch reserves on Base.

It replaces legacy snippets that assumed chain specific endpoints.

## Canonical Addresses

Tokens: `DIEM_TOKEN_ADDRESS=0xf4d97f2da56e8c3098f3a8d538db630a2606a024`, `VVV_TOKEN_ADDRESS=0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf`, `USDC_ADDRESS=0x833589fcd6edb6e08f4c7c32d4f71b54bda02913`, `WETH_ADDRESS=0x4200000000000000000000000000000000000006`.

Uniswap v2: router `0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24`, factory `0x8909dc15e40173ff4699343b6eb8132c65e18ec6`.

Aerodrome: router `0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43`, volatile factory `0x420dd381b31aef6683db6b902084cb0ffece40da`, stable factory `0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a`.

Store these values in `.env` and Replit secrets for brokers, agents, and discovery jobs.

## Etherscan v2 Usage

Endpoint template: `https://api.etherscan.io/v2/api?chainid=8453&module=<MODULE>&action=<ACTION>&…`.

Common actions: `module=contract&action=getabi` to fetch verified ABIs, `module=logs&action=getlogs` for `PairCreated`, and `module=proxy&action=eth_call` for `getPair` or `getReserves`.

Always append your API key via `apikey=<YOUR_KEY>` and respect rate limits by caching ABIs and recent responses.

## Pool Discovery Workflow

1. Query `getPair(tokenA, tokenB)` on the Uniswap v2 factory and both Aerodrome factories using `eth_call`.

2. If the pair address is non zero, fetch reserves with `getReserves()` (selector `0x0902f1ac`).

3. When direct pairs are absent, search `PairCreated` logs and assemble multi hop paths (e.g., DIEM → WETH → USDC).

4. Cache discoveries in SQL via `services/marketdata/token_watcher.py` so the orchestrator can reuse results.

## Troubleshooting

Use `uv run python apps/cli/main.py market:pools:watch --once` to refresh the catalog after routing changes.

`uv run python apps/cli/main.py market:routes:suggest --base DIEM --quote USDC` prints best known paths.

Set `DEX_DEBUG=1` or `DIEM_DEBUG_ROUTES=1` during diagnosis to log route selection and sanity clamp decisions.

## References

Etherscan v2 quickstart: https://docs.etherscan.io/etherscan-v2/v2-quickstart.

Uniswap v2 deployment list: https://docs.uniswap.org/contracts/v2/reference/smart-contracts/v2-deployments.

Aerodrome contracts: https://app.okcontract.com/abi/aerodrome/router_base/base.
