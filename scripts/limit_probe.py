from __future__ import annotations

import argparse
import json
import os
import threading
import time
from queue import Queue
from typing import Dict

import requests


def _broker_base_url() -> str:
    base = os.getenv("BROKER_BASE_URL")
    if base:
        return base.rstrip("/")
    host = os.getenv("BROKER_API_HOST", "127.0.0.1")
    port = os.getenv("BROKER_API_PORT", "8000")
    return f"http://{host}:{port}"


def worker(q: Queue, out: Dict[str, int], tenant_subkey: str):
    url = f"{_broker_base_url()}/v1/chat"
    headers = {"Authorization": f"Bearer {tenant_subkey}"}
    payload = {"messages": [{"role": "user", "content": "ping"}]}
    while True:
        try:
            q.get_nowait()
        except Exception:
            break
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=10)
            if r.status_code == 200:
                out["ok"] += 1
            elif r.status_code == 429:
                out["rate_limited"] += 1
            else:
                out["other"] += 1
        except Exception:
            out["other"] += 1


def main():
    p = argparse.ArgumentParser(description="Limiter probe against /v1/chat")
    p.add_argument("--tenant", required=True, help="Tenant subkey to use for Authorization")
    p.add_argument("--duration", type=int, default=30, help="Duration in seconds")
    p.add_argument("--rps", type=int, default=10, help="Approx target requests per second")
    args = p.parse_args()

    total = int(args.duration * args.rps)
    q: Queue = Queue()
    for _ in range(total):
        q.put(1)
    out = {"ok": 0, "rate_limited": 0, "other": 0}

    threads = []
    # Fire requests in a bursty but controlled manner
    start = time.time()
    def feeder():
        # Evenly distribute over duration
        interval = 1.0 / max(1, int(args.rps))
        while not q.empty():
            t = threading.Thread(target=worker, args=(q, out, args.tenant))
            t.start()
            threads.append(t)
            time.sleep(interval)

    feeder_thread = threading.Thread(target=feeder)
    feeder_thread.start()
    feeder_thread.join()
    for t in threads:
        t.join()
    dur = max(1e-6, time.time() - start)
    summary = {
        "attempted": total,
        "ok": out["ok"],
        "rate_limited": out["rate_limited"],
        "other": out["other"],
        "rps": round(out["ok"] / dur, 2),
    }
    print(json.dumps(summary))
    # Prom-style lines
    print(f"probe_requests_total {total}")
    print(f"probe_success_total {out['ok']}")
    print(f"probe_rate_limited_total {out['rate_limited']}")


if __name__ == "__main__":
    main()

