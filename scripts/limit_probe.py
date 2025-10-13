from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from statistics import mean
from typing import Any, Dict, List

import httpx


def _pct(vals: List[float], p: float) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = max(0, min(len(vals) - 1, int(round((p / 100.0) * (len(vals) - 1)))))
    return float(vals[k])


def build_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Probe /v1/chat throughput vs 429 responses")
    ap.add_argument("--base-url", default=os.getenv("BROKER_BASE_URL", "http://127.0.0.1:8000"), help="Broker base URL")
    ap.add_argument("--rps", type=float, default=10.0, help="Target requests per second")
    ap.add_argument("--duration", type=int, default=30, help="Duration seconds")
    ap.add_argument("--concurrency", type=int, default=20, help="Max in-flight requests")
    ap.add_argument("--model", default=None, help="Optional model to include in chat payload")
    ap.add_argument("--message", default="hello", help="User message content")
    ap.add_argument(
        "--auth-bearer",
        default=os.getenv("PROBE_AUTH_BEARER"),
        help="Authorization bearer token. If unset, will try admin mode (BROKER_ADMIN_TOKEN + --tenant-id)",
    )
    ap.add_argument("--tenant-id", default=None, help="Tenant id (required for admin token mode)")
    ap.add_argument(
        "--admin-token",
        default=os.getenv("BROKER_ADMIN_TOKEN"),
        help="Admin token (used with --tenant-id to act-as tenant)",
    )
    ap.add_argument("--no-idempotency", action="store_true", help="Do not set Idempotency-Key header")
    ap.add_argument("--timeout", type=float, default=10.0, help="Request timeout seconds")
    return ap.parse_args()


async def _run_probe(args: argparse.Namespace) -> Dict[str, Any]:
    url = args.base_url.rstrip("/") + "/v1/chat"
    attempted = 0
    ok = 0
    rl = 0
    other = 0
    lat_ok: List[float] = []

    sem = asyncio.Semaphore(args.concurrency)

    headers_base: Dict[str, str] = {"Content-Type": "application/json"}

    use_admin = False
    if not args.auth_bearer and args.admin_token and args.tenant_id:
        use_admin = True
        headers_base["Authorization"] = f"Bearer {args.admin_token}"
    elif args.auth_bearer:
        headers_base["Authorization"] = f"Bearer {args.auth_bearer}"
    else:
        raise SystemExit("Provide --auth-bearer (tenant subkey) or --admin-token with --tenant-id")

    payload_base: Dict[str, Any] = {
        "messages": [{"role": "user", "content": str(args.message)}]
    }
    if args.model:
        payload_base["model"] = args.model

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        start = time.perf_counter()

        async def one(i: int) -> None:
            nonlocal attempted, ok, rl, other
            # pace according to target rps
            target = start + i / float(args.rps)
            now = time.perf_counter()
            if target > now:
                await asyncio.sleep(target - now)
            async with sem:
                attempted += 1
                headers = dict(headers_base)
                if use_admin:
                    headers["X-Tenant-Id"] = args.tenant_id  # type: ignore[arg-type]
                if not args.no_idempotency:
                    # unique per request
                    headers["Idempotency-Key"] = f"p-{i}-{int(time.time()*1000)}"
                t0 = time.perf_counter()
                try:
                    resp = await client.post(url, headers=headers, json=payload_base)
                    dt = (time.perf_counter() - t0) * 1000.0
                    if resp.status_code == 429:
                        rl += 1
                    elif 200 <= resp.status_code < 300:
                        ok += 1
                        lat_ok.append(dt)
                    else:
                        other += 1
                except Exception:
                    other += 1

        total = int(args.rps * args.duration)
        tasks = [asyncio.create_task(one(i)) for i in range(total)]
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

    summary = {
        "attempted": attempted,
        "ok": ok,
        "rate_limited": rl,
        "other_errors": other,
        "attempted_rps": attempted / elapsed if elapsed > 0 else 0.0,
        "ok_rps": ok / elapsed if elapsed > 0 else 0.0,
        "latency_ms_avg": mean(lat_ok) if lat_ok else 0.0,
        "latency_ms_p50": _pct(lat_ok, 50.0),
        "latency_ms_p90": _pct(lat_ok, 90.0),
        "latency_ms_p99": _pct(lat_ok, 99.0),
        "duration_s": round(elapsed, 3),
        "base_url": args.base_url,
        "mode": "admin" if use_admin else "tenant",
        "window_seconds": os.getenv("RATE_LIMIT_WINDOW_SECONDS"),
        "max_requests": os.getenv("RATE_LIMIT_MAX_REQUESTS"),
        "redis_url": bool(os.getenv("REDIS_URL")),
    }
    return summary


def main() -> None:
    args = build_args()
    summary = asyncio.run(_run_probe(args))
    # Prom-style counters
    print(f"probe_requests_total {summary['attempted']}")
    print(f"probe_success_total {summary['ok']}")
    print(f"probe_rate_limited_total {summary['rate_limited']}")
    print(f"probe_other_errors_total {summary['other_errors']}")
    print(
        "probe_latency_ms_avg {}\nprobe_latency_ms_p50 {}\nprobe_latency_ms_p90 {}\nprobe_latency_ms_p99 {}".format(
            summary["latency_ms_avg"], summary["latency_ms_p50"], summary["latency_ms_p90"], summary["latency_ms_p99"]
        )
    )
    print(json.dumps(summary, separators=(",", ":")))


if __name__ == "__main__":
    main()

