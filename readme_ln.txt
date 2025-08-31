1	# VVV Agents Monorepo
2	
3	A scaffolded framework for a multi-agent system integrating LangGraph/LangChain orchestration with Coinbase AgentKit and Venice AI. It follows the implementation blueprint in `implementation-plan` and establishes a clean structure to build wallet, staking, DIEM, autonomous key issuance, and brokered capacity services.
4	
5	## Structure
6	
7	```
8	apps/
9	  broker-api/        # Capacity Broker API (stub)
10	  control-plane/     # Admin UI placeholder
11	  cli/               # Operator CLI (argparse-based)
12	services/
13	  wallet/            # Wallet providers (Smart Wallet + ETH account)
14	  staking/           # VVV staking client (stubs)
15	  diem/              # DIEM mint/burn/trade (stubs)
16	  venice_keys/       # Venice API key issuance manager
17	  marketdata/        # Price/quotes provider (stub)
18	  risk/              # Simple risk budget checks
19	agents/
20	  stake_master/      # Keeps staking optimal
21	  arbi_diem/         # DIEM arbitrage executor
22	  capacity_broker/   # Issues scoped keys and allocates capacity
23	  ai_treasurer/      # Treasury policies for VVV/DIEM
24	  quorum/            # Quorum voting + listen interval policy
25	graph/
26	  nodes/             # Node functions (observe/decide/execute)
27	  workflows/         # High-level workflows (composable)
28	libs/
29	  venice_sdk/        # Thin Venice client (autonomous key flow)
30	  agentkit_ext/      # AgentKit action wrappers (stubs)
31	  pricing/           # DIEM fair value helpers
32	  telemetry/         # Logging and basic tracing
33	infra/
34	  docker/            # Dockerfile placeholders
35	  k8s/               # K8s placeholders
36	  terraform/         # IaC placeholders
37	config/
38	  default.yml        # Central app configuration template
39	tests/               # Minimal import sanity tests
40	```
41	
42	## Quickstart
43	
44	- Python 3.10+
45	- Copy `.env.example` to `.env` and fill values as you integrate real services.
46	- Optional dependencies (install as you go): langchain, langgraph.
47	On-chain and wallets:
48	- `coinbase-agentkit` + `cdp-sdk` integrate CDP Smart Wallet (gasless) and Eth-account providers.
49	- Configure one of:
50	  - Dev EOA: set `WALLET_PROVIDER=eth_account`, `ETH_PRIVATE_KEY`, `BASE_RPC_URL`, `BASE_CHAIN_ID`.
51	  - Smart Wallet: set `WALLET_PROVIDER=smart_wallet`, `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`, `CDP_WALLET_SECRET`, `OWNER`, `NETWORK_ID` (base-mainnet|base-sepolia), optional `PAYMASTER_URL`.
52	  - Base gating enforced: only base-mainnet or base-sepolia are allowed.
53	To enable on-chain calls: set `BASE_RPC_URL` and provide ABIs in `abi/`.
54	
55	Run the CLI help:
56	
57	```
58	python apps/cli/main.py --help
59	```
60	
61	Run the sample workflow (dry run):
62	
63	```
64	python apps/cli/main.py run:quorum --dry-run
65	```
66	
67	Broker API (requires FastAPI + Uvicorn):
68	
69	```
70	uvicorn app:app --app-dir apps/broker-api --reload
71	```
72	
73	Notes:
74	- Entry points add the repo root to `sys.path` for simple local imports.
75	- On-chain calls now use AgentKit wallet providers underneath; configure env and ABIs:
76	  - `VVV_TOKEN_ADDRESS`, `VVV_STAKING_ADDRESS`, `DIEM_TOKEN_ADDRESS`.
77	  - DEX: dual providers supported (Uniswap V2 and Aerodrome). Set:
78	    - `DEX_PROVIDERS=uniswap_v2,aerodrome`
79	    - `UNISWAP_V2_ROUTER_ADDRESS`, `AERODROME_ROUTER_ADDRESS`, and optional `AERODROME_STABLE`
80	    - Legacy `ROUTER_ADDRESS` remains as a fallback for Uniswap V2
81	  - ABIs: `abi/erc20.json` (provided), plus project-specific `abi/staking.json`, `abi/diem.json`.
82	  - Trading supports:
83	    - Uniswap V2 router ABI (`abi/uniswap_v2_router.json`) with `TRADE_PATH`
84	    - Aerodrome router ABI (`abi/aerodrome_router.json`) single-hop with `AERODROME_STABLE`
85	- Venice client now performs real HTTP requests; endpoints are configurable via env.
86	- Keep secrets in `.env` and never commit them.
87	
88	## Configuration
89	
90	See `config/default.yml` and `.env.example` for environment variables and defaults.
91	
92	## License
93	
94	Proprietary. Do not distribute.
95	Addresses on Base:
96	- Defaults target Base mainnet (`NETWORK_ID=base-mainnet`). Fill official addresses in `.env` or `config/addresses.base-mainnet.yml`:
97	  - `VVV_TOKEN_ADDRESS`: VVV token
98	  - `VVV_STAKING_ADDRESS`: VVV staking
99	  - `DIEM_TOKEN_ADDRESS`: DIEM token
100	  - `UNISWAP_V2_ROUTER_ADDRESS`: Uniswap V2 router on Base (prefilled)
101	  - `AERODROME_ROUTER_ADDRESS`: Aerodrome router on Base (fill after verifying address)
102	
103	Offline signing test:
104	- Run a local end-to-end signing test with no network calls:
105	
106	```
107	python apps/cli/main.py test:challenge-offline
108	```
109	
110	It generates an ephemeral wallet, signs a dummy Venice challenge, and echoes the payload as the Venice client would receive it.
111	
112	Compare DEX quotes (live, uses configured providers and `TRADE_PATH`):
113	
114	```
115	python apps/cli/main.py quotes:compare --amount 1000000
116	```
117	
118	This prints quotes from Uniswap V2 and Aerodrome (if both routers are set) and highlights the best output.
119	
120	## Replit
121	
122	- Repo includes `.replit` and `replit.nix` for one-click run on Replit.
123	- Steps: import to Replit, add Secrets from `.env.example`, click Run. Health at `/health`.
124	- Deployments: create a Web Service from the Deployments panel (details in `infra/replit/README.md`).
