#!/usr/bin/env python
from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass

import requests

RPC_ENDPOINTS: list[str] = [
    "https://mainnet.base.org/",
    "https://developer-access-mainnet.base.org/",
    "https://base.gateway.tenderly.co",
    "https://base-rpc.publicnode.com",
]

JSON_PAYLOAD = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}


@dataclass
class ProbeResult:
    url: str
    ok: bool
    latency_ms: float
    status_code: int | None
    error: str | None
    height: int | None


def probe(url: str) -> ProbeResult:
    start = time.perf_counter()
    status_code = None
    error = None
    height = None
    try:
        response = requests.post(
            url,
            data=json.dumps(JSON_PAYLOAD),
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        status_code = response.status_code
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result")
        if isinstance(result, str) and result.startswith("0x"):
            height = int(result, 16)
    except Exception as exc:  # pragma: no cover - best-effort script
        error = str(exc)
    latency_ms = (time.perf_counter() - start) * 1000.0
    ok = error is None and status_code == 200 and height is not None
    return ProbeResult(
        url=url,
        ok=ok,
        latency_ms=latency_ms,
        status_code=status_code,
        error=error,
        height=height,
    )


def run(endpoints: Iterable[str]) -> None:
    results = [probe(url) for url in endpoints]
    results.sort(key=lambda r: (not r.ok, r.latency_ms))
    print("# Base RPC probe (sorted by health, latency)")
    for res in results:
        status = "OK" if res.ok else "FAIL"
        height = res.height if res.height is not None else "-"
        err = res.error or ""
        print(f"{status:4} {res.latency_ms:8.2f} ms  block={height}  url={res.url}")
        if err:
            print(f"    error={err}")


if __name__ == "__main__":
    run(RPC_ENDPOINTS)
