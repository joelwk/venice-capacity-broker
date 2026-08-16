# Venice Capacity Broker — Documentation

> **Version:** 1.0 (Consolidated)  
> **Status:** ✅ Production-Ready

Welcome to the **Venice Capacity Broker** documentation. This system combines autonomous agents, on-chain arbitrage, and multi-tenant API brokering to monetize Venice AI inference capacity.

---

## Documentation Map

### Getting Started

1. **[Architecture Overview](ARCHITECTURE.md)** - Understand the system design and agent responsibilities
2. **[Configuration](CONFIGURATION.md)** - Set up your environment (Base, Venice API, DEX, agents)
3. **[Deployment](DEPLOYMENT.md)** - Deploy via Docker Compose or Replit

After setup, follow the **[Operations Guide](OPERATIONS.md)** for daily checklists and monitoring.

### Operations & Maintenance

- **[Operations Guide](OPERATIONS.md)** - Daily checklists, dashboards, incident runbooks
- **[Troubleshooting](TROUBLESHOOTING.md)** - Solve common issues (Venice 404s, DEX gaps, price guards)
- **[Admin Panel](ADMIN.md)** - Web UI for tenant management and metrics
- **[Testing & QA](TESTING_QA.md)** - Test suites, probes, and quality gates

### Technical Deep Dives

- **[DIEM Technical Guide](DIEM_TECHNICAL_GUIDE.md)** - Fair value model, liquidity analysis, and configuration
- **[API Reference](API_REFERENCE.md)** - Complete broker API documentation (1800+ lines)
- **[Security & Keys](SECURITY_KEYS.md)** - Parent/sub-key hygiene, rotation policy, wallet guidance

### Reference

- **[Tokenomics](venice-diem-tokenomics.md)** - VVV & DIEM overview (high-level concepts)
- **[Changes](CHANGES.md)** - System evolution log (what changed and why)

---

## Quick Start

```bash
# 1. Clone and set up environment
cp .env.example .env
# Edit .env with your Venice API key, Base RPC, wallet private key

# 2. Run startup probe
uv run python apps/cli/main.py startup:probe

# 3. Start dry-run mode
uv run python apps/cli/main.py run:loop --dry-run --sleep 15 --max-cycles 3

# 4. Run tests
uv run pytest -q

# 5. Deploy (when ready)
docker-compose up -d
```

See **[Deployment](DEPLOYMENT.md)** for detailed instructions.

---

## Key Features

✅ **Autonomous Agents** - StakeMaster, ArbiDiem, CapacityBroker, AI Treasurer  
✅ **Multi-Agent Quorum** - Risk-gated decisions with weighted voting  
✅ **DIEM Fair Value Model** - Finite-horizon PV with adoption scaling  
✅ **DEX Integration** - Uniswap V2, Aerodrome multi-hop routing  
✅ **Broker API** - Multi-tenant API reselling with utilization markup and failsafe pause  
✅ **Public storefront** - Spot quotes plus optional limit bids (`BIDS_ENABLED`) that settle into the same verify path  
✅ **Reflex Guardian** - Emergency halt on volatility spikes or drawdowns  
✅ **Progressive-Live Mode** - Safe on-ramp from dry-run to live trading  

---

## Architecture at a Glance

```
┌─────────────────────────────────────────────────────────────┐
│                    Single-Loop Orchestrator                 │
├─────────────────────────────────────────────────────────────┤
│  StakeMaster → Quorum → ArbiDiem → Broker → AI Treasurer  │
│       ↓           ↓         ↓         ↓          ↓          │
│    VVV Stake   Risk Gate  DIEM Arb  Tenants   Portfolio    │
└─────────────────────────────────────────────────────────────┘
                           ↓
              ┌────────────────────────┐
              │  Reflex Guardian       │
              │  - Volatility checks   │
              │  - Drawdown limits     │
              │  - Heartbeat monitor   │
              └────────────────────────┘
```

See **[Architecture](ARCHITECTURE.md)** for detailed design.

---

## Documentation Structure

```
docs/
├── README.md                    # You are here
├── ARCHITECTURE.md              # System design
├── CONFIGURATION.md             # Environment setup
├── OPERATIONS.md                # Daily operations
├── DEPLOYMENT.md                # Deployment guide
├── TROUBLESHOOTING.md          # Problem solving
├── TESTING_QA.md               # Testing standards
├── SECURITY_KEYS.md            # Security practices
├── ADMIN.md                     # Admin panel
├── API_REFERENCE.md            # API docs
├── CHANGES.md                   # System evolution
├── venice-diem-tokenomics.md   # Tokenomics overview
│
├── Technical Guides
│   ├── DIEM_TECHNICAL_GUIDE.md         # Comprehensive DIEM guide
│   ├── DIEM_COMPOSITE_CONFIG.md        # Configuration reference
│   ├── DIEM_DYNAMIC_ROUTE_DISCOVERY.md # Technical reference
│   └── DIEM_DEX_OPERATOR_RUNBOOK.md    # Operational guide
│
├── Bridge Path Documentation
│   ├── BRIDGE_FACTORY_REGISTRATION_RUNBOOK.md    # Operational guide
│   └── PRODUCTION_CONFIG_BRIDGE_PATH.md          # Configuration
│
└── Deployment & Operations
    ├── DOCKER_DEPLOYMENT.md            # Docker deployment
    ├── REPLIT_DEPLOYMENT.md            # Replit deployment
    ├── DEPLOYMENT_VALIDATION.md        # Deployment validation
    ├── PROGRESSIVE_LIVE_VERIFICATION.md # Progressive live verification
    ├── DOCKER_VENICE_DIAGNOSTICS.md    # Docker diagnostics
    ├── CONFIG_SCENARIOS.md             # Configuration scenarios
    └── PLATFORM_ARCHITECTURE.md       # Platform architecture
```

---

## Need Help?

- **Configuration issues?** → [CONFIGURATION.md](CONFIGURATION.md)
- **System not starting?** → [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- **Want to understand DIEM pricing?** → [DIEM_TECHNICAL_GUIDE.md](DIEM_TECHNICAL_GUIDE.md)
- **API questions?** → [API_REFERENCE.md](API_REFERENCE.md)
- **Operational guidance?** → [OPERATIONS.md](OPERATIONS.md)

---

## External References

- **Root [AGENTS.md](../AGENTS.md)** - Production agent catalog (source of truth)
- **Venice Blog** - [VVV and DIEM tokenomics](https://venice.ai/blog)
- **Base Network** - [Official documentation](https://docs.base.org)
- **OpenAI Agents SDK** - [Design patterns](https://platform.openai.com/docs/guides/agents-sdk)

---

**Note:** Temporary analysis dumps and dated archives are omitted from the public snapshot. Use git history only if you kept a private backup.


