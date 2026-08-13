from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import Counter
from statistics import mean
from typing import Any

import httpx


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = max(0, min(len(vals) - 1, int(round((p / 100.0) * (len(vals) - 1)))))
    return float(vals[k])


def _parse_bool(val: str | None) -> bool:
    if val is None:
        return True
    lower = str(val).strip().lower()
    if lower in {"1", "true", "yes", "on"}:
        return True
    if lower in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {val}")


WORKLOAD_MESSAGES = {
    "hello": "Hello from probe",
    "sql_light": "Explain how you would query the users table safely.",
    "sql_heavy": "Draft a SQL query that joins shipments and orders and filters the last 30 days.",
}


def build_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Probe /v1/chat throughput vs 429 responses"
    )
    ap.add_argument(
        "--base-url",
        default=os.getenv("BROKER_BASE_URL", "http://127.0.0.1:8000"),
        help="Broker base URL",
    )
    ap.add_argument(
        "--rps", type=float, default=10.0, help="Target requests per second"
    )
    ap.add_argument("--duration", type=int, default=30, help="Duration seconds")
    ap.add_argument(
        "--concurrency", type=int, default=20, help="Max in-flight requests"
    )
    ap.add_argument(
        "--model", default=None, help="Optional model to include in chat payload"
    )
    ap.add_argument("--message", default="hello", help="User message content")
    ap.add_argument(
        "--workload",
        choices=list(WORKLOAD_MESSAGES.keys()),
        default="hello",
        help="Select a canned workload payload",
    )
    ap.add_argument(
        "--think",
        type=_parse_bool,
        default=True,
        help="Wrap the payload in <think> tags to exercise reasoning",
    )
    ap.add_argument(
        "--auth-bearer",
        default=os.getenv("PROBE_AUTH_BEARER"),
        help="Authorization bearer token. If unset, will try admin mode (BROKER_ADMIN_TOKEN + --tenant-id)",
    )
    ap.add_argument(
        "--tenant-id", default=None, help="Tenant id (required for admin token mode)"
    )
    ap.add_argument(
        "--admin-token",
        default=os.getenv("BROKER_ADMIN_TOKEN"),
        help="Admin token (used with --tenant-id to act-as tenant)",
    )
    ap.add_argument(
        "--no-idempotency",
        action="store_true",
        help="Do not set Idempotency-Key header",
    )
    ap.add_argument(
        "--status-histogram",
        action="store_true",
        help="Print status histogram and errors to aid diagnostics",
    )
    ap.add_argument(
        "--timeout", type=float, default=30.0, help="Request timeout seconds"
    )
    return ap.parse_args()


async def _run_probe(args: argparse.Namespace) -> dict[str, Any]:
    url = args.base_url.rstrip("/") + "/v1/chat"
    attempted = 0
    ok = 0
    rl = 0
    other = 0
    lat_ok: list[float] = []

    sem = asyncio.Semaphore(args.concurrency)

    headers_base: dict[str, str] = {"Content-Type": "application/json"}

    use_admin = False
    if not args.auth_bearer and args.admin_token and args.tenant_id:
        use_admin = True
        headers_base["Authorization"] = f"Bearer {args.admin_token}"
    elif args.auth_bearer:
        headers_base["Authorization"] = f"Bearer {args.auth_bearer}"
    else:
        raise SystemExit(
            "Provide --auth-bearer (tenant subkey) or --admin-token with --tenant-id"
        )

    content = WORKLOAD_MESSAGES.get(args.workload, str(args.message))
    if not args.think:
        payload_content = content
    else:
        payload_content = f"<think>\n{content}\n</think>"
    payload_base: dict[str, Any] = {
        "messages": [{"role": "user", "content": payload_content}]
    }
    if args.model:
        payload_base["model"] = args.model

    status_histogram = Counter(
        {
            "200": 0,
            "429": 0,
            "4xx": 0,
            "5xx": 0,
            "timeout": 0,
            "connect_error": 0,
            "other": 0,
        }
    )
    errors_sample: list[str] = []

    def record_error(message: str) -> None:
        if len(errors_sample) >= 5:
            return
        errors_sample.append(message.strip())

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        start = time.perf_counter()

        async def one(i: int) -> None:
            nonlocal attempted, ok, rl, other, status_histogram, errors_sample
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
                    headers["Idempotency-Key"] = f"p-{i}-{int(time.time() * 1000)}"
                t0 = time.perf_counter()
                try:
                    resp = await client.post(url, headers=headers, json=payload_base)
                    dt = (time.perf_counter() - t0) * 1000.0
                    code = resp.status_code
                    if code == 429:
                        rl += 1
                        status_histogram["429"] += 1
                        record_error(f"429: {resp.text[:120]}")
                    elif 200 <= code < 300:
                        ok += 1
                        lat_ok.append(dt)
                        status_histogram["200"] += 1
                    elif 400 <= code < 500:
                        other += 1
                        status_histogram["4xx"] += 1
                        record_error(f"{code}: {resp.text[:120]}")
                    elif 500 <= code < 600:
                        other += 1
                        status_histogram["5xx"] += 1
                        record_error(f"{code}: {resp.text[:120]}")
                    else:
                        other += 1
                        status_histogram["other"] += 1
                        record_error(f"{code}: {resp.text[:120]}")
                except httpx.ReadTimeout as exc:
                    other += 1
                    status_histogram["timeout"] += 1
                    record_error(f"timeout: {exc}")
                except httpx.ConnectError as exc:
                    other += 1
                    status_histogram["connect_error"] += 1
                    record_error(f"connect_error: {exc}")
                except Exception as exc:
                    other += 1
                    status_histogram["other"] += 1
                    record_error(f"exception: {exc}")

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
    summary["status_histogram"] = dict(status_histogram)
    summary["errors_sample"] = errors_sample
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
            summary["latency_ms_avg"],
            summary["latency_ms_p50"],
            summary["latency_ms_p90"],
            summary["latency_ms_p99"],
        )
    )
    if args.status_histogram:
        print(
            "probe_status_histogram",
            json.dumps(summary["status_histogram"], separators=(",", ":")),
        )
        if summary["errors_sample"]:
            print("probe_errors_sample", json.dumps(summary["errors_sample"]))
    print(json.dumps(summary, separators=(",", ":")))


if __name__ == "__main__":
    main()
