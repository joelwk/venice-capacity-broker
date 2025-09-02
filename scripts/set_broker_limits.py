from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import requests


def _base_url() -> str:
    base = os.getenv("BROKER_BASE_URL")
    if base:
        return base.rstrip("/")
    host = os.getenv("BROKER_API_HOST", "127.0.0.1")
    port = os.getenv("BROKER_API_PORT", "8000")
    return f"http://{host}:{port}"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Set per-tenant broker limits via admin API")
    p.add_argument("--tenant", required=True, help="Tenant id")
    p.add_argument("--window", type=int, required=False, help="Window seconds")
    p.add_argument("--max", dest="maxreq", type=int, required=False, help="Max requests per window")
    p.add_argument("--label", type=str, required=False, help="Label (e.g., premium, basic)")
    args = p.parse_args(argv)

    admin = os.getenv("BROKER_ADMIN_TOKEN")
    if not admin:
        raise SystemExit("ERROR: BROKER_ADMIN_TOKEN must be set")

    base = _base_url()
    url = f"{base}/v1/tenants/{args.tenant}/broker-limits"
    payload: dict[str, Any] = {}
    if args.window is not None:
        payload["windowSeconds"] = int(args.window)
    if args.maxreq is not None:
        payload["maxRequests"] = int(args.maxreq)
    if args.label is not None:
        payload["label"] = args.label

    r = requests.post(url, headers={"Authorization": f"Bearer {admin}", "Content-Type": "application/json"}, json=payload, timeout=20)
    if not r.ok:
        print(r.status_code)
        print(r.text)
        sys.exit(1)
    try:
        print(json.dumps(r.json(), indent=2))
    except Exception:
        print(r.text)


if __name__ == "__main__":
    main()

