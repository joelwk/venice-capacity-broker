**1. Project Setup and Environment**  
*(Validated: Fully aligned after wallet refactor; env vars hydrate AgentKit factories and the wallet CLI.)*

- Environment variables – define a `.env` that includes broker and agent settings.  

  - Network & RPC: set `BASE_RPC_URL` to a reliable Base L2 RPC (e.g., Ankr or Infura). Prefer `NETWORK_ID=base-mainnet` (or `base-sepolia` for testnets); `_require_base_network()` also accepts `BASE_CHAIN_ID=8453` or `84532`.  
    **Validated: `_require_base_network()` enforces supported identifiers and raises when misconfigured.**

  - Token addresses: provide DIEM, VVV, USDC, WETH (optional WBTC). For Base mainnet use `DIEM_TOKEN_ADDRESS=0xf4d97f2da56e8c3098f3a8d538db630a2606a024`, `VVV_TOKEN_ADDRESS=0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf`, `QUOTE_TOKEN_ADDRESS=0x833589fcd6edb6e08f4c7c32d4f71b54bda02913`.  

  - DEX addresses: configure `DEX_PROVIDERS` with Uniswap V3 (`0x2626664c2603336e57b271c5c0b26f421741e481`), Uniswap V2 (`0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24`), and Aerodrome (`0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43`).  

  - Trade paths: set `TRADE_PATH` and `TRADE_PATHS` to valid DIEM→USDC and VVV→USDC hops (e.g., DIEM→WETH@0.3%→USDC). Skip DIEM→VVV→WETH because no pool exists.  

  - Wallet provider: set `WALLET_PROVIDER` (smart_wallet or eth_account), `ETH_PRIVATE_KEY`, `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`, optional `CDP_WALLET_SECRET`, plus cold wallet helpers `COLD_WALLET_PRIVATE_KEY` and `COLD_WALLET_ADDRESS`.  
    **Validated: AgentKit factory normalises addresses, emits `WalletError`, and enforces required keys.**

  - Risk parameters: tune `SLIPPAGE_BPS`, `RISK_MAX_SLIPPAGE_BPS`, `RISK_MAX_POOL_TAKE_BPS`, `ARBI_DIEM_MINT_UNITS`, `STAKEMASTER_HEARTBEAT_INTERVAL_HOURS`.  

- Secrets management – store keys in a secure vault (AWS Secrets Manager, GCP Secret Manager, etc.). Avoid embedding secrets in code.  
  **Improvement: add runtime key encryption (e.g., Fernet from `cryptography`) for in-memory handling.**

- Testing infrastructure – prepare a Base devnet or fork for integration tests.  
  **Validated: `tests/test_wallet_provider.py` covers adapter behaviour, cold→hot transfers, and sweeps.**

**2. Wallet Management**  
*(Validated: Adapter, cold/hot flows, CLI, and tests implemented end to end.)*

- Wallet layer (`services/wallet/provider.py`, `libs/agentkit_ext/agentkit_wallet.py`) exposes address lookup, signing, and transaction helpers.  
  **Validated: adapter normalises addresses, raises `WalletError`, and falls back to owner signing for smart wallets.**

- AgentKitWalletAdapter – wraps AgentKit providers and translates failures.  
  **Status: Complete; `send_transaction()` now enforces checksum outputs and accepts optional gas overrides.**

- AgentKit provider – `_require_base_network()` selects SmartWallet vs EOA and hydrates config fields defensively.  
  **Status: Complete; retry/backoff remains a future hardening task.**

- **Cold / hot separation** – `transfer_from_cold_to_hot()` and `sweep_profits_to_cold()` use EIP-1559 builders, configurable gas buffers, and `LocalAccountSigner`. Workflow: fund hot wallet from cold signer, operate agents, sweep excess above `min_balance`, record tx hash.  

- **Wallet CLI** – `scripts/wallet_cli.py` (argparse) offers `address`, `sign`, `send`, `transfer-cold`, `sweep`. Commands are exposed in `apps/cli/main.py` as `venice:wallet:*`, aligning with the implementation plan.  

- **Additional Best Practices Integration**:  
  - **Backup/Recovery**: expose AgentKit `export_wallet()` via future CLI work and note secure mnemonic storage.  
  - **Testing/QA**: extend integration coverage on Base Sepolia once dry-run harness lands; keep unit tests deterministic.  
  - **Observability**: route wallet actions through `libs.telemetry` (tx hash, gas, value) in a follow-up.  
  - **Prod Hardening**: default to SmartWallet in production env validation; add tx rate limits via `libs.ratelimit`.

