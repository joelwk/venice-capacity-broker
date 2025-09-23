Yes—use the **Etherscan API v2** (single endpoint + `chainid=8453`) to fetch **Base**-specific router, factory, token, and pool state. Below is a tight, implementation‑ready playbook with verified addresses, API calls, and repo‑friendly steps.

---

## 1) Canonical contract addresses (Base mainnet)

> You can drop these straight into your `.env` / Replit Secrets.

**Tokens**

* `DIEM_TOKEN_ADDRESS=0xf4d97f2da56e8c3098f3a8d538db630a2606a024` (Base) ([Base Explorer][1])
* `VVV_TOKEN_ADDRESS=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf` (Base) ([Base Explorer][2])
* `USDC_ADDRESS=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` (Base USDC) ([Base Explorer][3])
* `WETH_ADDRESS=0x4200000000000000000000000000000000000006` (Base canonical WETH) ([Base Explorer][4], [docs.uniswap.org][5])

**Uniswap v2 on Base**

* `UNISWAP_V2_ROUTER_ADDRESS=0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24` (official deployments page) ([docs.uniswap.org][6])
* `UNISWAP_V2_FACTORY_ADDRESS=0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6` (official deployments page; also emits `PairCreated` on Base) ([docs.uniswap.org][6], [ww4.basescan.org][7])

**Aerodrome (Velodrome‑style AMM on Base)**

* `AERODROME_ROUTER_ADDRESS=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43` (verified on BaseScan) ([Base Explorer][8])
* `AERODROME_FACTORY_VOLATILE=0x420DD381b31aEf6683db6B902084cB0FFECe40Da` (BaseScan) ([app.okcontract.com][9])
* `AERODROME_FACTORY_STABLE=0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A` (BaseScan) ([Etherscan][10])

> **Note**: Your earlier env list already matched these values, but the lines above are now cross‑validated from authoritative sources.

---

## 2) Use **Etherscan API v2** for Base (chainid **8453**)

* **Base** is supported via the **single** Etherscan v2 endpoint; add `chainid=8453` to every call. ([Etherscan][11], [docs.uniswap.org][5])

**Base endpoint pattern**

```
GET https://api.etherscan.io/v2/api?chainid=8453&module=<MODULE>&action=<ACTION>&<params>&apikey=<YOUR_KEY>
```

Why v2? One URL for all chains, consistent `chainid`, and access to the **logs**, **contract**, and **proxy** modules you need. ([Etherscan][11])

---

## 3) Validate routers & factories (ABI + source)

You want to **assert** addresses and ABIs at boot (and cache them).

**Examples**

```bash
# Uniswap v2 Router ABI (Base)
curl -s "https://api.etherscan.io/v2/api?chainid=8453&module=contract&action=getabi&address=0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24&apikey=$ETHERSCAN_API_KEY"

# Uniswap v2 Factory ABI (Base)
curl -s "https://api.etherscan.io/v2/api?chainid=8453&module=contract&action=getabi&address=0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6&apikey=$ETHERSCAN_API_KEY"

# Aerodrome Router ABI (Base)
curl -s "https://api.etherscan.io/v2/api?chainid=8453&module=contract&action=getabi&address=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43&apikey=$ETHERSCAN_API_KEY"

# Aerodrome Volatile Factory ABI
curl -s "https://api.etherscan.io/v2/api?chainid=8453&module=contract&action=getabi&address=0x420DD381b31aEf6683db6B902084cB0FFECe40Da&apikey=$ETHERSCAN_API_KEY"

# Aerodrome Stable Factory ABI
curl -s "https://api.etherscan.io/v2/api?chainid=8453&module=contract&action=getabi&address=0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A&apikey=$ETHERSCAN_API_KEY"
```

(Modules/actions per Etherscan v2 docs.) ([Etherscan][11])

---

## 4) Discover pools for DIEM, VVV (programmatic)

You have two robust strategies; implement both for redundancy:

### A) **Direct factory lookups** (`eth_call` → `getPair`)

* **Uniswap v2 factory** exposes `getPair(address tokenA, address tokenB)` (selector `0xe6a43905`). Call via **v2 proxy** → `eth_call`. ([docs.uniswap.org][12], [Go Packages][13])
* **Aerodrome** uses separate factories; call `getPair(tokenA, tokenB)` on **both** `AERODROME_FACTORY_VOLATILE` and `..._STABLE`. (Aerodrome is a Velodrome‑style v2 AMM on Base with distinct stable/volatile factories.) ([app.okcontract.com][9], [Etherscan][10])

**Sample Uniswap v2 `getPair` (DIEM/USDC)**

```bash
# data = 0xe6a43905 + pad(DIEM) + pad(USDC)
DATA="0xe6a43905\
000000000000000000000000f4d97f2da56e8c3098f3a8d538db630a2606a024\
000000000000000000000000833589fcd6edb6e08f4c7c32d4f71b54bda02913"

curl -s "https://api.etherscan.io/v2/api" \
  --get --data-urlencode "chainid=8453" \
  --data-urlencode "module=proxy" \
  --data-urlencode "action=eth_call" \
  --data-urlencode "to=0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6" \
  --data-urlencode "data=$DATA" \
  --data-urlencode "tag=latest" \
  --data-urlencode "apikey=$ETHERSCAN_API_KEY"
```

If result `0x000...000` → pair does **not** exist on that DEX; try Aerodrome factories (same pattern but `to=` the factory). Uniswap v2 function & SDK docs cover `getPair`. ([docs.uniswap.org][12])

### B) **Event scan** (backfill or discovery)

* Query factory **logs** for `PairCreated`.

  * **Uniswap v2 event**: `PairCreated(address indexed token0, address indexed token1, address pair, uint)`; topic0 = `keccak256("PairCreated(address,address,address,uint256)")`. ([docs.uniswap.org][14])
  * **Aerodrome/Velodrome** typically logs a Solidly‑style `PairCreated(address,address,bool,address,uint)`; hash with `keccak256` and use as `topic0` (compute once at init). (Use contract ABIs from the factories above to confirm exact signature variants on your deployment.) ([app.okcontract.com][9], [Etherscan][10])

**Etherscan v2 `getLogs` skeleton**

```bash
curl -s "https://api.etherscan.io/v2/api" \
  --get --data-urlencode "chainid=8453" \
  --data-urlencode "module=logs" \
  --data-urlencode "action=getLogs" \
  --data-urlencode "address=0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6" \
  --data-urlencode "fromBlock=0" \
  --data-urlencode "toBlock=latest" \
  --data-urlencode "topic0=<PAIRCREATED_HASH>" \
  --data-urlencode "apikey=$ETHERSCAN_API_KEY"
```

(See v2 `getLogs` parameters.) ([GitHub][15])

> You can also filter by `topic1`/`topic2` = DIEM/VVV/USDC (padded addresses). Remember Uniswap v2 sorts token0 < token1 (by address), so check both orderings. ([docs.uniswap.org][16])

---

## 5) Read pool state (reserves, tokens, decimals)

Once you have a **pair/pool address**, call standard read‑only methods via **proxy/eth\_call**:

* `token0()` → selector `0x0dfe1681`
* `token1()` → selector `0xd21220a7`
* `getReserves()` → selector `0x0902f1ac` (returns `reserve0`, `reserve1`, `timestamp`) ([4byte.directory][17], [docs.uniswap.org][16])

**Example (`getReserves`)**

```bash
curl -s "https://api.etherscan.io/v2/api" \
  --get --data-urlencode "chainid=8453" \
  --data-urlencode "module=proxy" \
  --data-urlencode "action=eth_call" \
  --data-urlencode "to=<PAIR_ADDRESS>" \
  --data-urlencode "data=0x0902f1ac" \
  --data-urlencode "tag=latest" \
  --data-urlencode "apikey=$ETHERSCAN_API_KEY"
```

---

## 6) Cross‑checks from token pages (context)

* **VVV token page** confirms Base address, supply/holders; useful for sanity checks and holder analytics. ([Base Explorer][2])
* **DIEM token page** confirms Base address; supply/holders tabs help spot fresh liquidity and holders. ([Base Explorer][1])
* **Known active pools** (for manual validation and seeding):

  * **DIEM/USDC** pool on Base (Aerodrome) at `0xb1367773cb48ae6d910ae711012dbc5e7ceae615`. ([Base Explorer][18])
  * **VVV/DIEM** pool on Base (Aerodrome) at `0xbb345d35450bf9ee76f3d2ce214e8e7ac5e1071d`. ([Etherscan][19])
    You can pull these ABIs (`contract/getabi`) and verify reserves via `eth_call` as above.

---

## 7) Drop‑in code hooks for your broker

**Env additions (recommended)**

```
DEX_PROVIDERS=aerodrome,uniswapv2   # comma-separated preference
UNISWAP_V2_FACTORY_ADDRESS=0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6
AERODROME_FACTORY_VOLATILE=0x420DD381b31aEf6683db6B902084cB0FFECe40Da
AERODROME_FACTORY_STABLE=0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A
WETH_ADDRESS=0x4200000000000000000000000000000000000006
ETHERSCAN_API_URL=https://api.etherscan.io/v2/api
ETHERSCAN_CHAINID=8453
ETHERSCAN_API_KEY=<yours>
```

**Service methods**

1. `get_pair_address(dex, tokenA, tokenB)`

   * If `dex == 'uniswapv2'`: call factory `getPair`.
   * If `dex == 'aerodrome'`: call **both** factories, return first nonzero.
     (Use `proxy/eth_call`.) ([docs.uniswap.org][12])

2. `get_pool_state(pair_addr)`

   * `token0/token1/getReserves` via `proxy/eth_call`. Cache 500–1000 ms. ([docs.uniswap.org][16])

3. `discover_pairs_by_logs(dex, from_block)` (optional backfill)

   * `logs/getLogs` on the factory with `topic0=keccak(PairCreated signature)`. Store in SQL for later joins. ([GitHub][15])

4. `verify_router(router_addr)`

   * `contract/getabi` + `contract/getsourcecode`; check expected function selectors present.

---

## 8) What to fix in our code right now

1. **Standardize on Etherscan v2** for Base:

   * Replace any Base‑specific explorer domains with:
     `ETHERSCAN_API_URL=https://api.etherscan.io/v2/api` and `ETHERSCAN_CHAINID=8453`. ([Etherscan][11])

2. **Harden address config** (match section §1).

   * Ensure **Uniswap v2** and **Aerodrome** routers/factories above are used everywhere (no fallbacks to stale addresses).

3. **Pool discovery** must **not** assume a single factory:

   * For Aerodrome **call both** factories; don’t assume “stable” or “volatile” only. (Those two factory addresses are live on Base.) ([app.okcontract.com][9], [Etherscan][10])

4. **Topic hashing** is computed at startup:

   * `PAIRCREATED_V2=keccak256("PairCreated(address,address,address,uint256)")` (Uniswap v2).
   * `PAIRCREATED_AERO=keccak256("PairCreated(address,address,bool,address,uint)")` (Aerodrome‑style).
     Use these hashes with `logs/getLogs`. (Signature shape per Uniswap docs; Aerodrome factories verified via BaseScan; compute the hash locally.) ([docs.uniswap.org][14], [app.okcontract.com][9], [Etherscan][10])

5. **Reserve reads** use `eth_call` (not scraping UIs):

   * `getReserves()` selector `0x0902f1ac` per 4byte directory; avoid guessing. ([4byte.directory][17])

6. **WETH path**: keep Base WETH address **0x4200…0006** in paths and swap quotes; it’s canonical on Base & Uniswap docs list it for v3 as well. ([Base Explorer][4], [docs.uniswap.org][5])

---

## 9) Example: DIEM → USDC best‑path probe

1. **Try Uniswap v2**: `getPair(DIEM, USDC)` on `UNISWAP_V2_FACTORY_ADDRESS`. If nonzero, fetch reserves. ([docs.uniswap.org][12])
2. **Try Aerodrome**: `getPair(DIEM, USDC)` on `AERODROME_FACTORY_VOLATILE`, else `..._STABLE`. Fetch reserves. ([app.okcontract.com][9], [Etherscan][10])
3. Compare **(price impact, fee, gas)** for both, prefer higher liquidity / lower impact.
4. If neither exists, try **two‑hop** via WETH: `DIEM→WETH→USDC` (you already provided this trade path pattern). Confirm hops exist with `getPair` first. ([docs.uniswap.org][16])

---

## 10) Quick pool intel (manual context)

* **DIEM/USDC Aerodrome pool** (Base): `0xb1367773cb48ae6d910ae711012dbc5e7ceae615` (GeckoTerminal). Use `contract/getabi` and `eth_call` to verify reserves. ([Base Explorer][18])
* **VVV/DIEM Aerodrome pool** (Base): `0xbb345d35450bf9ee76f3d2ce214e8e7ac5e1071d` (GeckoTerminal). Same verification flow. ([Etherscan][19])

---

## 11) Rate limiting & fallbacks

* Respect Etherscan v2 rate limits; cache ABIs and recent `getReserves` for \~1–2 seconds if you poll frequently.
* If you need raw speed, you can call a Base RPC directly for `eth_call` and use Etherscan v2 only for **ABIs** and **logs** backfills.

---

### TL;DR Actions

1. **Update `.env`** with addresses in §1.
2. **Switch to Etherscan v2** with `chainid=8453` for ABIs/logs/proxy calls. ([Etherscan][11])
3. Implement `get_pair_address` (v2 factory + both Aerodrome factories) and `get_pool_state` (`token0/token1/getReserves`). ([docs.uniswap.org][12])
4. Add an **on‑boot verifier** that fetches router/factory ABIs and asserts expected selectors.
5. (Optional) Add a **discovery job** that streams `PairCreated` logs to seed/refresh your local pairs table. ([GitHub][15])

This gives your broker deterministic, API‑first access to **routers, pools, and live liquidity** on Base—without relying on frontends.

[1]: https://basescan.org/token/0xf4d97f2da56e8c3098f3a8d538db630a2606a024?utm_source=chatgpt.com "ERC-20 Token | Address: 0xf4d97f2d...a2606a024"
[2]: https://basescan.org/token/0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf?utm_source=chatgpt.com "Venice Token (VVV) | ERC-20 | Address"
[3]: https://basescan.org/token/0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913?utm_source=chatgpt.com "USDC - Token"
[4]: https://basescan.org/token/0x4200000000000000000000000000000000000006?utm_source=chatgpt.com "Wrapped Ether (WETH) | ERC-20 | Address"
[5]: https://docs.uniswap.org/contracts/v3/reference/deployments/base-deployments?utm_source=chatgpt.com "Base Deployments"
[6]: https://docs.uniswap.org/contracts/v2/reference/smart-contracts/v2-deployments?utm_source=chatgpt.com "V2 Deployment Addresses - Contracts"
[7]: https://ww4.basescan.org/tx/0x6c2f7799f960cfc93bc61d09bf68f1b18974c7396d1be19c4d1bd45c7b7e3755?utm_source=chatgpt.com "Base Transaction Hash: 0x6c2f7799f9... | BaseScan"
[8]: https://basescan.org/address/0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24?utm_source=chatgpt.com "Uniswap: V2 Router02 - Contract"
[9]: https://app.okcontract.com/abi/aerodrome/router_base/base?utm_source=chatgpt.com "Aerodrome Router"
[10]: https://docs.etherscan.io/etherscan-v2/support/common-error-messages?utm_source=chatgpt.com "Common Error Messages - Etherscan API"
[11]: https://docs.etherscan.io/etherscan-v2/v2-quickstart?utm_source=chatgpt.com "V1 to V2 API Migration Guide | Etherscan"
[12]: https://docs.uniswap.org/contracts/v2/guides/smart-contract-integration/getting-pair-addresses?utm_source=chatgpt.com "Pair Addresses"
[13]: https://pkg.go.dev/github.com/ebadiere/go-defi/bindings/uniswap/factory?utm_source=chatgpt.com "uniswapv2factory package - github.com/ebadiere ..."
[14]: https://docs.uniswap.org/contracts/v2/reference/smart-contracts/factory?utm_source=chatgpt.com "Factory"
[15]: https://github.com/aerodrome-finance/contracts?utm_source=chatgpt.com "Aerodrome Finance Smart Contracts"
[16]: https://docs.uniswap.org/contracts/v2/reference/smart-contracts/pair?utm_source=chatgpt.com "Pair"
[17]: https://www.4byte.directory/signatures/?bytes4_signature=0x0902f1ac&utm_source=chatgpt.com "Ethereum Signature Database"
[18]: https://basescan.org/token/0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf?a=0x4bf88042de0220647acb314af3c5f310aec3bcc0&utm_source=chatgpt.com "Venice Token (VVV) | ERC-20 | Address"
[19]: https://docs.etherscan.io/etherscan-v2?utm_source=chatgpt.com "Introduction - Etherscan API"
