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
    p = argparse.ArgumentParser(description="Create a test tenant on the Broker API")
    p.add_argument(
        "--tenant-id",
        default=os.getenv("TEST_TENANT_ID", "t-test"),
        help="Tenant id (default: t-test)",
    )
    p.add_argument(
        "--label",
        default=os.getenv("TEST_TENANT_LABEL", "Test Tenant"),
        help="Tenant label",
    )
    p.add_argument(
        "--quota", type=int, default=None, help="Optional quota override (int)"
    )
    p.add_argument(
        "--expires-at",
        default=None,
        help="Optional ISO8601 expiry (e.g., 2025-12-31T23:59:00Z)",
    )
    p.add_argument(
        "--rotate",
        action="store_true",
        default=False,
        help="Rotate key if tenant exists",
    )
    p.add_argument(
        "--revoke-old",
        action="store_true",
        default=False,
        help="Revoke previous key on rotate",
    )
    p.add_argument(
        "--probe-chat",
        action="store_true",
        default=False,
        help="Send a hello message via admin act-as",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("CREATE_TEST_TENANT_TIMEOUT", "20")),
        help="HTTP timeout for the tenant creation request",
    )
    args = p.parse_args(argv)

    base = _base_url()
    admin = os.getenv("BROKER_ADMIN_TOKEN")
    if not admin:
        raise SystemExit("ERROR: BROKER_ADMIN_TOKEN must be set (Replit Secrets)")

    # Venice parent key must be present for subkey creation
    if not (os.getenv("VENICE_PARENT_KEY") or os.getenv("VENICE_API_KEY")):
        print(
            "WARNING: VENICE_PARENT_KEY or VENICE_API_KEY is not set — subkey issuance may fail",
            file=sys.stderr,
        )

    url = f"{base}/v1/tenants"
    payload: dict[str, Any] = {"tenant_id": args.tenant_id, "label": args.label}
    if args.quota is not None:
        payload["quota"] = int(args.quota)
    if args.expires_at:
        payload["expires_at"] = args.expires_at

    qp = []
    if args.rotate:
        qp.append("rotate=true")
        if args.revoke_old:
            qp.append("revoke_old=true")
    if qp:
        url = url + ("?" + "&".join(qp))

    headers = {"Authorization": f"Bearer {admin}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=args.timeout)
    if not r.ok:
        raise SystemExit(f"Create tenant failed: {r.status_code} {r.text}")

    try:
        obj = r.json()
    except Exception:
        obj = {"raw": r.text}
    print(json.dumps({"tenant": obj}, indent=2))

    if getattr(args, "probe_chat", False):
        chat_url = f"{base}/v1/chat"
        c_headers = {
            "Authorization": f"Bearer {admin}",
            "X-Tenant-Id": args.tenant_id,
            "Content-Type": "application/json",
        }
        max_tokens = int(os.getenv("BROKER_CHAT_PROBE_MAX_TOKENS", "128"))
        default_model = os.getenv("BROKER_DEFAULT_MODEL") or os.getenv(
            "VENICE_DEFAULT_MODEL"
        )
        c_body = {
            "messages": [{"role": "user", "content": "Hello from probe"}],
            "max_tokens": max_tokens,
        }
        if default_model:
            c_body["model"] = default_model
        cr = requests.post(chat_url, headers=c_headers, json=c_body, timeout=20)
        ok = cr.ok
        detail = None
        try:
            detail = cr.json()
        except Exception:
            detail = cr.text
        print(
            json.dumps(
                {"chat_status": cr.status_code, "ok": ok, "response": detail}, indent=2
            )
        )


if __name__ == "__main__":
    main()
