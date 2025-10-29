# Etherscan v2 Reference (Base)

This guide documents how the repo uses the Etherscan v2 API to verify routers, discover pools, and fetch reserves on Base.

It replaces legacy snippets that assumed chain specific endpoints.

## Canonical Addresses

Reference `config/broker-fixes.env.template` for the current Base token, staking, and router addresses.

Copy those values into `.env` and secret managers so brokers, agents, and discovery jobs stay aligned.

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
