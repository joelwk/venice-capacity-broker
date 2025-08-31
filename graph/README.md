Run Graph Examples

- The LangGraph nodes implement a minimal pipeline with a DIEM controller that computes a premium against a fair value helper.
- Environment controls:
  - `DIEM_FAIR_ALPHA`: float (e.g., 0.6)
  - `DIEM_PREMIUM_THRESHOLD`: ratio (e.g., 1.08)

Example

- Run a single pass with optional broker routing:
  `python apps/cli/main.py run:graph --messages "[{\"role\":\"user\",\"content\":\"hello\"}]"`

Premium Rationale

- The controller logs a span with rationale and a standardized premium debug span using `debug_premium_span`:
  - Attributes: `price`, `fair_per_day`, `alpha`, `premium`, `threshold`, `decision`.
  - Additional debug span `vvv.node.diem_premium.debug` includes env, inputs, and computed values.
- Why compute premium:
  - Premium vs fair anchors mint/sell policy consistent with VVV/DIEM tokenomics and the active-staker discipline. When observed price materially exceeds fair value, the system favors mint+sell to recycle capacity and reinforce quorum incentives.

