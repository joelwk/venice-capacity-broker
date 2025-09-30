import ast
from pathlib import Path

cycles = []
for line in Path("runtime.log").read_text().splitlines():
    if line.startswith("single-loop cycle: "):
        payload = ast.literal_eval(line.split(": ", 1)[1])
        cycles.append(payload)

print(f"total cycles: {len(cycles)}")
for idx, cycle in enumerate(cycles, 1):
    arbi = cycle["arbi"]
    stake = cycle["stake"]
    prog = cycle.get("progressive", {})
    prog_state = prog.get("state", {})
    print(
        f"cycle {idx}: price={arbi['price']} action={arbi['action']} dry_run={arbi['dry_run']} "
        f"outcome={arbi['outcome']} reason={arbi['why'].get('reason')} slippage_bps={arbi['why'].get('slippage_bps')} "
        f"slippage_ok={arbi['why'].get('slippage_ok')} heartbeat_sent={stake['heartbeat']['sent']} "
        f"hb_error={stake['heartbeat'].get('error')} progressive_counter={prog_state.get('counter')} "
        f"live={prog_state.get('live')} last_error={prog_state.get('last_heartbeat_error')}"
    )
