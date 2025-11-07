# Venice Capacity Broker - Architecture Overview

**Last Updated:** October 29, 2025

## Table of Contents

1. [System Overview](#system-overview)
2. [Repository Structure](#repository-structure)
3. [Broker API Architecture](#broker-api-architecture)
4. [Agent Orchestration](#agent-orchestration)
5. [Key Services](#key-services)
6. [Deployment](#deployment)
7. [References](#references)

## System Overview

The Venice Capacity Broker is a production-ready autonomous system that:

- **Stakes VVV tokens** to earn daily DIEM allocation and emissions
- **Arbitrages DIEM** against market prices using risk-gated workflows
- **Issues scoped API keys** to resell compute capacity
- **Manages multi-tenant** quota, rate limits, and billing
- **Provides self-service** buy flow for DIEM capacity purchase

The system runs as a single-loop orchestrator with quorum voting, reflex guardrails, and AI Treasurer guidance.

## Repository Structure

```
venice/
├── apps/                    # Application entrypoints
│   ├── broker_api/          # Modular FastAPI application
│   │   ├── app.py           # App factory with dependency injection
│   │   ├── auth.py          # Authentication & authorization
│   │   ├── config.py        # Configuration helpers
│   │   ├── cache.py         # Response caching
│   │   ├── store.py         # Tenant store factory
│   │   ├── lifespan.py      # Lifecycle management
│   │   ├── rate_limit.py    # Rate limiting setup
│   │   ├── marketdata.py    # Market data provider
│   │   ├── routers/         # API route handlers
│   │   │   ├── admin.py
│   │   │   ├── tenants.py
│   │   │   ├── quotes.py
│   │   │   ├── purchases.py
│   │   │   ├── bids.py
│   │   │   ├── clearing.py
│   │   │   ├── settlement.py
│   │   │   └── venice.py
│   │   └── services/        # Business logic helpers
│   │       ├── pricing.py
│   │       ├── clearing.py
│   │       └── bids.py
│   ├── cli/                 # Operator CLI
│   │   └── main.py
│   └── control-plane/       # Admin UI (static)
│
├── agents/                  # Autonomous agents
│   ├── stake_master/        # VVV staking agent
│   ├── arbi_diem/           # DIEM arbitrage agent
│   ├── capacity_broker/     # Key issuance agent
│   ├── ai_treasurer/        # Treasury management agent
│   ├── quorum/              # Voting coordination
│   └── reflex/              # Risk guardrails
│
├── services/                # Core services
│   ├── staking/             # VVV staking client
│   ├── diem/                # DIEM mint/burn
│   ├── marketdata/          # Price aggregation
│   ├── venice_keys/         # Key management
│   ├── memory/              # Decision persistence
│   ├── risk/                # Risk policy
│   ├── wallet/              # Wallet providers
│   └── pricing/             # Pricing service
│
├── graph/                   # Orchestration
│   ├── workflows/           # Single-loop orchestrator
│   ├── nodes/               # Graph node functions
│   └── langgraph/           # LangGraph integration
│
├── libs/                    # Shared libraries
│   ├── telemetry/           # Logging & metrics
│   ├── dex/                 # DEX aggregation
│   ├── kv/                  # Key-value stores
│   ├── ratelimit/           # Rate limiting
│   └── venice_sdk/          # Venice API client
│
├── db/                      # Database layer
│   ├── models.py            # SQLModel schemas
│   ├── session.py           # Session management
│   └── migrations/          # Alembic migrations
│
├── tests/                   # Test suite
├── docs/                    # Documentation
├── infra/                   # Infrastructure configs
│   ├── docker/
│   ├── replit/
│   ├── k8s/
│   └── terraform/
└── config/                  # Configuration files
```

## Broker API Architecture

The Broker API follows a **modular router architecture** with clean separation of concerns:

### Core Components

**App Factory** (`app.py`)
- Creates FastAPI instance with lifespan management
- Builds dependency injection container
- Wires modular routers
- Configures CORS and static routes

**Authentication** (`auth.py`)
- Bearer token extraction
- Admin authentication
- Tenant authorization via subkey
- Startup validation

**Configuration** (`config.py`)
- Environment-driven defaults
- Expiry timestamp computation
- Recursive field extraction

**Caching** (`cache.py`)
- Price response caching with TTL
- Env-and-prices combined cache
- Configurable capacity limits

**Rate Limiting** (`rate_limit.py`)
- KV-backed sliding window limiter
- Per-tenant override support
- SQL compaction for analytics

**Market Data** (`marketdata.py`)
- Thread-safe singleton provider
- DEX quote aggregation
- Etherscan discovery

**Lifecycle** (`lifespan.py`)
- Async context manager
- Market data warming on startup
- Cache preloading

### Service Helpers

**Pricing** (`services/pricing.py`)
- Quote generation
- Settlement pricing

**Clearing** (`services/clearing.py`)
- DIEM clearing price computation
- Configurable basis point bands

**Bids** (`services/bids.py`)
- EIP-712 signature verification
- Asset price conversion
- Bid status classification

### Router Organization

Each router handles a specific domain:

- **admin** - System monitoring, quotes/purchases listings
- **tenants** - Tenant CRUD, rotation, limits
- **quotes** - Quote generation for capacity purchase
- **purchases** - Payment verification, key issuance
- **bids** - Limit order management
- **clearing** - Clearing price and SSE streams
- **settlement** - DEX preview and settlement
- **venice** - Venice API proxy endpoints

## Agent Orchestration

### Single-Loop Orchestrator

The orchestrator runs in `graph/workflows/orchestrator.py` with this sequence:

1. **StakeMaster** - Manages VVV staking, claims rewards
2. **Quorum Coordinator** - Aggregates model votes
3. **ArbiDiem** - Executes risk-gated DIEM arbitrage
4. **CapacityBroker** - Issues scoped API keys
5. **AI Treasurer** - Logs guidance for operators

### Quorum Models

Five specialized models vote on agent decisions:

- **YieldModel** - Evaluates staking yields
- **ArbModel** - Identifies arbitrage opportunities
- **RiskModel** - Enforces risk limits (weight 2.0)
- **DemandModel** - Predicts capacity demand
- **TreasuryModel** - Advises on capital allocation

### Reflex Guardian

`agents/reflex/guardian.py` halts execution on:
- Price drawdowns > 12%
- Volatility > 450 bps
- Inactive staking heartbeat

### Run Modes

See `docs/DEPLOYMENT.md` for the canonical commands and guidance on dry-run, progressive-live, and enable-live.

## Key Services

### Staking Service

- VVV staking contract interaction
- Reward claiming and compounding
- Cooldown scheduling

### DIEM Service

- Mint/burn operations with sVVV locking
- Capacity gate enforcement
- Mint rate curve tracking

### Market Data Provider

- Multi-DEX quote aggregation (Uniswap V2, Aerodrome)
- Venice signal integration
- Price sanity clamping
- Reserve cap computation

### Venice Keys Manager

- Parent/sub-key management
- Consumption limit enforcement
- Automatic revocation on abuse

### Memory Store

- Decision logging to JSONL
- Reflection engine critiques
- Long-term recall for agents

### Risk Policy

- Utilization-based sizing
- Volatility caps
- Slippage and pool-take limits
- Liquidity-aware adjustments

## Deployment

Deployment steps, environment layering, and post-deploy checks have moved to `docs/DEPLOYMENT.md`.

For a single source of truth on environment variables, see `docs/CONFIGURATION.md`.

## References

### Documentation

- **Agent Implementation** - `docs/appendices/design/implementation-plan-agents.md`
- **Broker Implementation** - `docs/appendices/design/implementation-plan-broker.md`
- **Deployment Guide** - `docs/DEPLOYMENT.md`
- **Configuration** - `docs/CONFIGURATION.md`
- **Tokenomics** - `docs/venice-diem-tokenomics.md`
- **Agent Catalog** - `AGENTS.md`
- **Migration History** - `BROKER_MIGRATION.md`

### Key Files

- **Orchestrator** - `graph/workflows/orchestrator.py`
- **CLI** - `apps/cli/main.py`
- **Broker App** - `apps/broker_api/app.py`
- **Market Data** - `services/marketdata/provider.py`
- **Risk Policy** - `services/risk/policy.py`

### External Resources

- [Venice AI](https://venice.ai) - VVV and DIEM platform
- [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) - Agent patterns
- [LangGraph](https://langchain-ai.github.io/langgraph/) - Multi-agent orchestration

---

**Architecture Status:** Production-ready with modular design  
**Last Major Update:** October 29, 2025 - Completed broker API migration to modular architecture

