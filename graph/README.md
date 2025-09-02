Run Graph Examples

- The LangGraph nodes implement a minimal pipeline with a DIEM controller that computes a premium against a fair value helper.
- Environment controls:
  - `DIEM_FAIR_ALPHA`: float (e.g., 0.6)
  - `DIEM_PREMIUM_THRESHOLD`: ratio (e.g., 1.08)

Example

- Run a single pass with optional broker routing:
  `python apps/cli/main.py run:graph --messages "[{\"role\":\"user\",\"content\":\"hello\"}]"`

Premium examples (with env overrides)

- Set env for premium policy and run once:
  - `DIEM_FAIR_ALPHA=0.6`
  - `DIEM_PREMIUM_THRESHOLD=1.08`

- Example A — premium below threshold (hold)
  - Suppose observed DIEM price is 1.20 and with `alpha=0.6` the helper yields `fair_per_day≈2.13`.
  - Premium = `price / fair_per_day ≈ 0.56` < `threshold=1.08` → decision `hold`.
  - Sample log line (from `diem_controller_node`):
    - `diem_controller_rationale: {"price": 1.2, "fair_per_day": 2.13, "alpha": 0.6, "premium": 0.56, "threshold": 1.08, "decision": "hold"}`

- Example B — premium above threshold (mint/sell)
  - Suppose observed DIEM price is 2.50 with the same `fair_per_day≈2.13`.
  - Premium = `2.50 / 2.13 ≈ 1.17` ≥ `threshold=1.08` → decision `mint_sell`.
  - Sample log line:
    - `diem_controller_rationale: {"price": 2.5, "fair_per_day": 2.13, "alpha": 0.6, "premium": 1.17, "threshold": 1.08, "decision": "mint_sell"}`

Premium Rationale

- The controller logs a span with rationale and a standardized premium debug span using `debug_premium_span`:
  - Attributes: `price`, `fair_per_day`, `alpha`, `premium`, `threshold`, `decision`.
  - Additional debug span `vvv.node.diem_premium.debug` includes env, inputs, and computed values.
- Why compute premium:
  - Premium vs fair anchors mint/sell policy consistent with VVV/DIEM tokenomics and the active-staker discipline. When observed price materially exceeds fair value, the system favors mint+sell to recycle capacity and reinforce quorum incentives.

Rationale spans (how it works)
- `diem_controller_node` computes `fair_per_day` using `libs.pricing.diem.fair_value_per_diem(alpha) / 365` and derives `premium = price / fair_per_day`.
- It annotates two spans when tracing is enabled (`LANGCHAIN_TRACING_V2=true`):
  - `vvv.node.diem_controller.attrs`: the full rationale dict described above.
  - `vvv.node.diem_premium.debug`: emitted by `debug_premium_span`, capturing env (`DIEM_FAIR_ALPHA`, `DIEM_PREMIUM_THRESHOLD`), inputs (price), and derived values (`fair_per_day`, `premium`).
- These spans make it easy to inspect why a `mint_sell` vs `hold` decision was taken in LangSmith.

Broker Traffic & Counters
- The graph pipeline can call Venice directly when `broker_request` is provided, but rate‑limit counters live behind the Broker API.
- To generate limiter counters and compact them into SQL on Replit or local:
  - Ensure `BROKER_BASE_URL` and `BROKER_ADMIN_TOKEN` are set (and SQL if desired).
  - Drive traffic via the Broker: `make chat-admin TENANT=t1 [MESSAGE=Hello]` or use the probe in `scripts/limit_probe.py`.
  - Compact counters: `make db-compact` (or `python apps/cli/main.py data:compact-counters --force`).
  - Inspect: `make db-counters TENANT=t1 [LIMIT=20]` or call `GET /v1/debug/counters?tenant_id=t1&limit=20`.
- KV autodetection: Redis when `REDIS_URL`/`KV_REDIS_URL` is set; Replit DB when `KV_URL`/`REPLIT_DB_URL` is set; otherwise in‑memory. `make env-status` summarizes the detected backend.
