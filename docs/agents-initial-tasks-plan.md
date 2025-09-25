# Initial Multi-Agent Tasks Plan (v1 loop)

1. Establish a reusable orchestrator context that loads wallet, staking, market data, DIEM service, and Venice key manager once per cycle.
2. Run StakeMaster first in each cycle to refresh staking status, claim if live mode is enabled, and emit heartbeat telemetry results into the shared context.
3. Fetch market signals, compute DIEM mint rate, and pass utilization/volatility inputs into ArbiDiem; execute trades only when live mode and quorum allow, otherwise record a dry-run decision.
4. Invoke the CapacityBroker after trading decisions: reconcile tenant usage, issue or revoke scoped keys against the parent key, and respect consumptionLimit and expiry policy defaults from env.
5. Add an optional quorum coordinator hook that can veto or delay ArbiDiem actions; default to a simple weight-based majority using existing Quorum helpers.
6. Persist per-cycle outcomes (staking, arbitrage, broker actions) via `MemoryStore` and feed critiques from `ReflectionEngine`/`ReflexGuardian` before emitting telemetry so LangGraph and CLI commands can inspect the sequence.
7. Update CLI surfaces such as the run loop command and tests to exercise the new orchestrator path while keeping a dry-run default for safe local execution.
