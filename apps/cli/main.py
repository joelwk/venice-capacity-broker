from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure repo root is in sys.path before importing local packages
_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from apps._path import add_repo_root_to_sys_path

add_repo_root_to_sys_path()

from libs.telemetry.logger import get_logger
from libs.venice_sdk.client import VeniceClient
from services.venice_keys.manager import KeyManager
from typing import Any, Dict
import requests
import re
from datetime import datetime


logger = get_logger("cli")


def _env_flag(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def cmd_compact_counters(args: argparse.Namespace) -> None:
    """Compact KV sliding-window counters into SQL 'counter' table.

    - Gated by env KV_SQL_COMPACTION_ENABLED unless --force
    - Best-effort across KV backends (Redis, Replit DB, in-memory)
    - Parses keys like: rl:tenant:{tenantId}:chat:{bucket_start_epoch}
    - Reads optional per-tenant limits at broker:tenant:{tenantId}:limits
    """
    if not args.force and not _env_flag("KV_SQL_COMPACTION_ENABLED", default=False):
        logger.info("KV->SQL compaction disabled (KV_SQL_COMPACTION_ENABLED=false)")
        return
    try:
        from libs.kv import KVStore
        from db.session import get_engine, create_db_and_tables
        from sqlmodel import Session, select
        from db.models import Counter
        import json
    except Exception as e:  # noqa: BLE001
        logger.error(f"Missing dependencies for compaction: {e}")
        return

    # Ensure tables exist (auto-setup for Replit/first run)
    try:
        create_db_and_tables()
    except Exception:
        pass

    # Initialize engine early for fallback SQL reads
    engine = get_engine()

    kv = KVStore()

    # Determine source prefix; default matches limiter keys
    src_prefix = os.getenv("KV_COMPACTION_PREFIX", "rl:tenant:")
    keys = kv.keys(src_prefix)
    if not keys:
        # Heuristic fallback for stores without prefix listing (e.g., some Replit KV variants)
        logger.info("No KV keys via prefix listing; attempting recent-window scan for known tenants...")
        try:
            import time as _time
            from sqlmodel import select as _select
            from db.models import Tenant as _DbTenant

            # Discover tenants from SQL (if available)
            tenant_ids: list[str] = []
            try:
                with Session(engine) as _s:  # type: ignore[call-arg]
                    tenant_ids = [row.id for row in _s.exec(_select(_DbTenant)).all()]
            except Exception:
                tenant_ids = []

            # If SQL is empty (e.g., JSON store), try Broker API /v1/tenants via admin token
            if not tenant_ids:
                base = (os.getenv("BROKER_BASE_URL") or f"http://{os.getenv('BROKER_API_HOST','127.0.0.1')}:{os.getenv('BROKER_API_PORT','8000')}").rstrip("/")
                admin = os.getenv("BROKER_ADMIN_TOKEN")
                if admin:
                    try:
                        r = requests.get(base + "/v1/tenants", headers={"Authorization": f"Bearer {admin}"}, timeout=10)
                        if r.ok:
                            data = r.json()
                            if isinstance(data, list):
                                tenant_ids = [str(t.get("id")) for t in data if isinstance(t, dict) and t.get("id")]
                    except Exception:
                        pass

            # Window config
            window_default = int((os.getenv("RATE_LIMIT_WINDOW_SECONDS") or "60").strip() or 60)
            scan_minutes = int((os.getenv("KV_COMPACTION_SCAN_MINUTES") or "60").strip() or 60)
            now = int(_time.time())
            start = now - scan_minutes * 60
            start = (start // window_default) * window_default

            found = []
            for tid in tenant_ids:
                t = start
                while t <= now:
                    k = f"rl:tenant:{tid}:chat:{t}"
                    v = kv.get(k)
                    if v not in (None, "", "0"):
                        found.append(k)
                    t += window_default
            keys = found
        except Exception as _e:  # noqa: BLE001
            logger.info(f"Heuristic scan skipped: {_e}")

    if not keys:
        logger.info("No KV counter keys found to compact.")
        return
    # engine already initialized above
    pattern = re.compile(r"^rl:tenant:(?P<tenant>[^:]+):chat:(?P<bucket>\d+)$")
    delete_after = _env_flag("KV_COMPACTION_DELETE", default=False)
    window_default = int((os.getenv("RATE_LIMIT_WINDOW_SECONDS") or "60").strip() or 60)
    rows_written = 0
    rows_updated = 0
    with Session(engine) as s:  # type: ignore[call-arg]
        for k in keys:
            m = pattern.match(k)
            if not m:
                continue
            tenant_id = m.group("tenant")
            bucket_s = int(m.group("bucket"))
            # Look up count
            raw = kv.get(k)
            try:
                count = int(raw) if raw is not None else 0
            except Exception:
                count = 0
            if count <= 0:
                continue
            # Compute windowSeconds from per-tenant limits or default
            win_s = window_default
            try:
                limits_raw = kv.get(f"broker:tenant:{tenant_id}:limits")
                if limits_raw:
                    obj = json.loads(limits_raw)
                    win_s = int(obj.get("windowSeconds", win_s))
            except Exception:
                pass
            bucket_dt = datetime.utcfromtimestamp(bucket_s)
            # Upsert by (tenant_id, scope, bucket_start, bucket_seconds, model=None)
            scope = "chat"
            model = None
            existing = s.exec(
                select(Counter).where(
                    Counter.tenant_id == tenant_id,
                    Counter.scope == scope,
                    Counter.bucket_start == bucket_dt,
                    Counter.bucket_seconds == win_s,
                    Counter.model == model,  # type: ignore[comparison-overlap]
                )
            ).first()
            if existing is None:
                rec = Counter(
                    tenant_id=tenant_id,
                    scope=scope,
                    model=model,
                    bucket_start=bucket_dt,
                    bucket_seconds=win_s,
                    count=count,
                )
                s.add(rec)
                rows_written += 1
            else:
                if count > int(existing.count):
                    existing.count = int(count)
                    rows_updated += 1
            try:
                s.commit()
            except Exception as e:  # noqa: BLE001
                s.rollback()
                logger.warning(f"Failed to upsert counter for {tenant_id}@{bucket_s}: {e}")
                continue
            if delete_after:
                try:
                    kv.delete(k)
                except Exception:
                    pass

    logger.info(f"Compaction complete: inserted={rows_written} updated={rows_updated}")


def _parse_dt(val: str) -> datetime:
    # Accept RFC3339/ISO8601 or epoch seconds
    try:
        if val.isdigit():
            return datetime.utcfromtimestamp(int(val))
    except Exception:
        pass
    try:
        # Basic ISO parse without external deps
        # Examples: 2024-08-01T00:00:00Z, 2024-08-01 00:00:00
        v = val.rstrip("Z").replace("T", " ")
        return datetime.fromisoformat(v)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"Invalid datetime '{val}': {e}")


def cmd_counters_show(args: argparse.Namespace) -> None:
    """Show aggregated counter buckets for a tenant from SQL.

    Examples:
      vvv-agents counters:show --tenant t-123 --limit 20 --desc
      vvv-agents counters:show --tenant t-123 --scope chat --since 2024-08-01T00:00:00Z
    """
    try:
        from sqlmodel import Session, select
        from db.session import get_engine, create_db_and_tables
        from db.models import Counter
    except Exception as e:  # noqa: BLE001
        logger.error(f"SQL dependencies not available: {e}")
        return

    # Ensure tables exist before querying
    try:
        create_db_and_tables()
    except Exception:
        pass

    engine = get_engine()
    q = select(Counter).where(Counter.tenant_id == args.tenant)
    if args.scope:
        q = q.where(Counter.scope == args.scope)
    if args.model:
        q = q.where(Counter.model == args.model)
    if args.bucket_seconds is not None:
        q = q.where(Counter.bucket_seconds == int(args.bucket_seconds))
    if args.since:
        q = q.where(Counter.bucket_start >= _parse_dt(args.since))
    if args.until:
        q = q.where(Counter.bucket_start <= _parse_dt(args.until))

    order_desc = bool(getattr(args, "desc", True))
    if order_desc:
        from sqlalchemy import desc

        q = q.order_by(desc(Counter.bucket_start))
    else:
        q = q.order_by(Counter.bucket_start)

    limit = int(args.limit or 50)
    with Session(engine) as s:  # type: ignore[call-arg]
        rows = s.exec(q.limit(limit)).all()

    if not rows:
        logger.info("No counter rows found for selection.")
        return

    if args.json:
        import json

        out = [
            {
                "tenant_id": r.tenant_id,
                "scope": r.scope,
                "model": r.model,
                "bucket_start": r.bucket_start.isoformat() + "Z",
                "bucket_seconds": r.bucket_seconds,
                "count": int(r.count),
            }
            for r in rows
        ]
        print(json.dumps(out, indent=2))
        return

    for r in rows:
        ts = r.bucket_start.strftime("%Y-%m-%d %H:%M:%S") + "Z"
        logger.info(
            f"tenant={r.tenant_id} scope={r.scope} model={r.model or '-'} start={ts} bucket={r.bucket_seconds}s count={int(r.count)}"
        )


def cmd_init(args: argparse.Namespace) -> None:
    logger.info("Environment check and baseline setup (no-op stub)")
    needed = ["BASE_RPC_URL", "VENICE_API_BASE_URL"]
    for k in needed:
        logger.info(f"{k}={'set' if os.getenv(k) else 'missing'}")


def cmd_issue_key(args: argparse.Namespace) -> None:
    # Lazy import wallet helpers to avoid web3 deps for non-wallet commands
    from libs.agentkit_ext.agentkit_wallet import get_address, sign_message

    client = VeniceClient()
    keys = KeyManager(client)
    wallet_addr = args.wallet or get_address()
    try:
        res = keys.issue_root_key_via_challenge(wallet_addr, signer=sign_message)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Challenge flow failed ({e}); attempting direct signature payload")
        # Fallback: sign a deterministic message if challenge endpoint is unavailable
        message = f"Create Venice key for {wallet_addr}"
        signature = sign_message(message)
        res = keys.issue_root_key(wallet_addr, signature)
    logger.info(f"Root key result: {res}")


def cmd_venice_usage(args: argparse.Namespace) -> None:
    client = VeniceClient()
    try:
        usage = client.get_usage()
        logger.info(f"usage: {usage}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to fetch usage: {e}")
        return
    try:
        limits = client.get_rate_limits()
        logger.info(f"limits: {limits}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to fetch rate limits/quota: {e}")


def cmd_venice_models(args: argparse.Namespace) -> None:
    client = VeniceClient()
    try:
        res = client.list_models()
        logger.info(f"models: {res}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to list models: {e}")


def cmd_venice_signals(args: argparse.Namespace) -> None:
    client = VeniceClient()
    out: Dict[str, Any] = {}
    try:
        out["vvv"] = client.get_vvv_signals()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to fetch VVV signals: {e}")
    try:
        out["diem"] = client.get_diem_signals()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to fetch DIEM signals: {e}")
    logger.info(f"signals: {out}")


def _extract_addresses(obj: Any) -> set[str]:
    import re

    addrs: set[str] = set()
    if obj is None:
        return addrs
    if isinstance(obj, str):
        if re.fullmatch(r"0x[a-fA-F0-9]{40}", obj.strip()):
            addrs.add(obj.lower())
        return addrs
    if isinstance(obj, dict):
        for v in obj.values():
            addrs |= _extract_addresses(v)
        return addrs
    if isinstance(obj, (list, tuple)):
        for v in obj:
            addrs |= _extract_addresses(v)
        return addrs
    return addrs


def cmd_venice_validate_addresses(args: argparse.Namespace) -> None:
    client = VeniceClient()
    env_addrs = {
        "VVV_TOKEN_ADDRESS": (os.getenv("VVV_TOKEN_ADDRESS") or "").lower(),
        "VVV_STAKING_ADDRESS": (os.getenv("VVV_STAKING_ADDRESS") or "").lower(),
        "DIEM_TOKEN_ADDRESS": (os.getenv("DIEM_TOKEN_ADDRESS") or "").lower(),
    }
    found: set[str] = set()
    try:
        vvv = client.get_vvv_signals()
        found |= _extract_addresses(vvv)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not fetch /v1/vvv: {e}")
    try:
        diem = client.get_diem_signals()
        found |= _extract_addresses(diem)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not fetch /v1/diem: {e}")

    logger.info(f"discovered_addresses={sorted(found)}")
    for k, v in env_addrs.items():
        if not v:
            logger.info(f"{k} is unset; cannot validate")
            continue
        status = "MATCH" if v in found else "MISMATCH"
        logger.info(f"{k}={v} -> {status}")


def cmd_run_stakemaster(args: argparse.Namespace) -> None:
    # Lazy imports to avoid web3 deps for other commands
    from services.staking.client import StakingService
    from libs.agentkit_ext.actions import VVVActions
    from agents.stake_master.agent import StakeMaster

    stake = StakingService(VVVActions())
    agent = StakeMaster(stake)
    agent.run_once(live=getattr(args, "enable_live", False))


def cmd_run_loop(args: argparse.Namespace) -> None:
    """Minimal loop: StakeMaster heartbeat repeated with sleep and max-cycles.

    Use --enable-live to allow on-chain claims; otherwise dry-run.
    """
    import time

    from services.staking.client import StakingService
    from libs.agentkit_ext.actions import VVVActions
    from agents.stake_master.agent import StakeMaster

    stake = StakingService(VVVActions())
    agent = StakeMaster(stake)
    max_cycles = int(args.max_cycles)
    sleep_s = float(args.sleep)
    live = bool(getattr(args, "enable_live", False))
    for i in range(max_cycles):
        logger.info(f"Loop cycle {i+1}/{max_cycles} (live={live})")
        agent.run_once(live=live)
        if i < max_cycles - 1 and sleep_s > 0:
            time.sleep(sleep_s)


def cmd_run_quorum(args: argparse.Namespace) -> None:
    # Lazy imports to avoid web3 deps for non-quorum commands
    from services.marketdata.provider import MarketDataProvider
    from services.diem.client import DIEMService
    from libs.dex.providers import build_aggregator_from_env
    from agents.arbi_diem.agent import ArbiDiem
    from graph.workflows.revenue_streams import DiemMintSellWorkflow

    market = MarketDataProvider()
    diem = DIEMService(build_aggregator_from_env())
    arbi = ArbiDiem(diem)
    flow = DiemMintSellWorkflow(market=market, arbi=arbi)
    decided = flow.run_once(dry_run=args.dry_run)
    logger.info(f"Quorum/flow decision (dry={args.dry_run}): {decided}")


def cmd_run_orchestrator(args: argparse.Namespace) -> None:
    """Run the orchestrator loop coordinating ArbiDiem decisions with persistence."""
    import time

    from services.marketdata.provider import MarketDataProvider
    from services.diem.client import DIEMService
    from libs.dex.providers import build_aggregator_from_env
    from agents.arbi_diem.agent import ArbiDiem
    from graph.workflows.orchestrator import Orchestrator

    market = MarketDataProvider()
    diem = DIEMService(build_aggregator_from_env())
    arbi = ArbiDiem(diem)
    orch = Orchestrator(market=market, arbi=arbi)

    orch.run_loop(
        interval_s=float(args.interval),
        backoff_s=float(args.backoff),
        max_backoff_s=float(args.max_backoff),
        dry_run=bool(args.dry_run),
        max_cycles=int(args.max_cycles),
    )


def cmd_run_graph(args: argparse.Namespace) -> None:
    """Run the LangGraph pipeline once with optional broker messages."""
    import json

    from graph.langgraph import build_minimal_graph

    run = build_minimal_graph()
    state: Dict[str, Any] = {}
    if args.messages:
        try:
            msgs = json.loads(args.messages)
        except Exception as e:  # noqa: BLE001
            raise SystemExit(f"Invalid --messages JSON: {e}")
        state["broker_request"] = {"messages": msgs, "model": args.model}
    out = run(state)
    logger.info(f"graph_out: {out}")

def cmd_test_challenge_offline(args: argparse.Namespace) -> None:
    """Offline E2E: ephemeral EOA signs a dummy challenge via KeyManager."""
    # Ephemeral wallet for offline signing
    from importlib import import_module

    Account = import_module("eth_account").Account  # type: ignore[attr-defined]
    encode_defunct = import_module("eth_account.messages").encode_defunct  # type: ignore[attr-defined]

    acct = Account.create()
    wallet_addr = acct.address

    def signer(message: str) -> str:
        msg = encode_defunct(text=message)
        sig = Account.sign_message(msg, private_key=acct.key)
        return sig.signature.hex()

    class FakeVeniceClient:
        def __init__(self) -> None:
            self.calls: Dict[str, Any] = {}

        def get_challenge(self, wallet_address: str) -> Dict[str, Any]:
            self.calls["get_challenge"] = wallet_address
            return {
                "id": "offline-123",
                "challenge": f"Sign to create Venice key for {wallet_address}",
                "message": f"Create key: {wallet_address}",
                "nonce": "n-xyz",
            }

        def create_root_inference_key(self, wallet_address: str, signature: str, challenge: str | None = None, challenge_id: str | None = None) -> Dict[str, Any]:
            payload = {
                "wallet": wallet_address,
                "signature": signature,
                "challenge": challenge,
                "challengeId": challenge_id,
            }
            self.calls["create_root_inference_key"] = payload
            return {"status": "ok", "echo": payload}

    client = FakeVeniceClient()
    keys = KeyManager(client)  # type: ignore[arg-type]
    res = keys.issue_root_key_via_challenge(wallet_addr, signer=signer)
    logger.info(f"Offline echo: {res}")


def cmd_print_addresses(args: argparse.Namespace) -> None:
    fields = [
        "VVV_TOKEN_ADDRESS",
        "VVV_STAKING_ADDRESS",
        "DIEM_TOKEN_ADDRESS",
        "UNISWAP_V2_ROUTER_ADDRESS",
        "AERODROME_ROUTER_ADDRESS",
        "AERODROME_STABLE",
        "DEX_PROVIDERS",
        "ROUTER_ADDRESS",
        "TRADE_PATH",
    ]
    for k in fields:
        logger.info(f"{k}={os.getenv(k, '') or '(unset)'}")


# --- Broker admin helpers (HTTP) ---
def _broker_base_url() -> str:
    base = os.getenv("BROKER_BASE_URL")
    if base:
        return base.rstrip("/")
    host = os.getenv("BROKER_API_HOST", "127.0.0.1")
    port = os.getenv("BROKER_API_PORT", "8000")
    return f"http://{host}:{port}"


def _admin_headers() -> dict[str, str]:
    tok = os.getenv("BROKER_ADMIN_TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def cmd_broker_tenants_list(args: argparse.Namespace) -> None:
    url = f"{_broker_base_url()}/v1/tenants"
    r = requests.get(url, headers=_admin_headers(), timeout=10)
    if not r.ok:
        logger.error(f"list tenants failed: {r.status_code} {r.text}")
        return
    tenants = r.json()
    for t in tenants:
        logger.info(f"tenant id={t['id']} label={t['label']} status={t['status']} quota={t['quota']} expires_at={t.get('expires_at')}")


def cmd_broker_limits_get(args: argparse.Namespace) -> None:
    tid = args.tenant
    url = f"{_broker_base_url()}/v1/tenants/{tid}/broker-limits"
    r = requests.get(url, headers=_admin_headers(), timeout=10)
    if not r.ok:
        logger.error(f"get limits failed: {r.status_code} {r.text}")
        return
    logger.info(f"limits[{tid}]: {r.json()}")


def cmd_broker_limits_set(args: argparse.Namespace) -> None:
    tid = args.tenant
    url = f"{_broker_base_url()}/v1/tenants/{tid}/broker-limits"
    payload: Dict[str, Any] = {}
    if args.window is not None:
        payload["windowSeconds"] = int(args.window)
    if args.max is not None:
        payload["maxRequests"] = int(args.max)
    if args.label is not None:
        payload["label"] = args.label
    r = requests.post(url, headers=_admin_headers(), json=payload, timeout=10)
    if not r.ok:
        logger.error(f"set limits failed: {r.status_code} {r.text}")
        return
    logger.info(f"updated limits[{tid}]: {r.json()}")


# --- Idempotency keys admin ---
def cmd_idem_purge(args: argparse.Namespace) -> None:
    """Purge idempotency keys by prefix.

    Example:
      vvv-agents idem:purge --prefix idem:chat:t-123
    """
    try:
        from libs.kv import KVStore
    except Exception as e:  # noqa: BLE001
        logger.error(f"KV unavailable: {e}")
        return

    kv = KVStore()
    prefix = args.prefix
    keys = kv.keys(prefix)
    if not keys:
        logger.info("No keys matched prefix.")
        return
    deleted = 0
    for k in keys:
        try:
            kv.delete(k)
            deleted += 1
            if deleted % 50 == 0:
                logger.info(f"progress: deleted {deleted} keys…")
        except Exception:
            pass
    logger.info(f"purge complete: prefix={prefix} deleted={deleted}")
    # Also print to stdout so non-logging capture (e.g., tests) see the summary
    try:
        print(f"purge complete: prefix={prefix} deleted={deleted}")
    except Exception:
        pass


def cmd_probe_limits(args: argparse.Namespace) -> None:
    """Probe /v1/chat throughput and limiter behavior.

    Uses tenant subkey via --auth-bearer or admin act-as via BROKER_ADMIN_TOKEN + --tenant.
    Prints Prom-style counters and a JSON summary.
    """
    try:
        # Import the async probe runner
        from scripts.limit_probe import _run_probe  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.error(f"limit probe unavailable: {e}. Ensure scripts/limit_probe.py and httpx are installed.")
        return

    import asyncio as _asyncio
    import json as _json

    base_url = (args.base_url or _broker_base_url()).rstrip("/")
    # Resolve auth
    auth_bearer = args.auth_bearer or os.getenv("PROBE_AUTH_BEARER")
    admin_token = os.getenv("BROKER_ADMIN_TOKEN")
    tenant_id = args.tenant_id or os.getenv("PROBE_TENANT_ID")

    ns = argparse.Namespace(
        base_url=base_url,
        rps=float(args.rps),
        duration=int(args.duration),
        concurrency=int(args.concurrency),
        model=(args.model or os.getenv("PROBE_MODEL") or None),
        message=(args.message or os.getenv("PROBE_MESSAGE") or "hello"),
        auth_bearer=auth_bearer,
        tenant_id=tenant_id,
        admin_token=admin_token,
        no_idempotency=bool(args.no_idempotency or _env_flag("PROBE_NO_IDEMPOTENCY", False)),
        timeout=float(args.timeout or os.getenv("PROBE_TIMEOUT") or 10.0),
    )

    # Basic validation
    if not ns.auth_bearer and not (ns.admin_token and ns.tenant_id):
        logger.error("Provide --auth-bearer (tenant subkey) or set BROKER_ADMIN_TOKEN and pass --tenant")
        return

    summary = _asyncio.run(_run_probe(ns))
    # Prom counters
    print(f"probe_requests_total {summary['attempted']}")
    print(f"probe_success_total {summary['ok']}")
    print(f"probe_rate_limited_total {summary['rate_limited']}")
    print(f"probe_other_errors_total {summary['other_errors']}")
    print(
        "probe_latency_ms_avg {}\nprobe_latency_ms_p50 {}\nprobe_latency_ms_p90 {}\nprobe_latency_ms_p99 {}".format(
            summary.get("latency_ms_avg", 0),
            summary.get("latency_ms_p50", 0),
            summary.get("latency_ms_p90", 0),
            summary.get("latency_ms_p99", 0),
        )
    )
    print(_json.dumps(summary, separators=(",", ":")))

def _require_trade_path() -> list[str]:
    path_env = os.getenv("TRADE_PATH")
    if not path_env:
        raise SystemExit("TRADE_PATH must be set: comma-separated token addresses (in,out)")
    return [p.strip() for p in path_env.split(",")]


def cmd_quotes_compare(args: argparse.Namespace) -> None:
    from libs.dex.providers import build_aggregator_from_env

    path = _require_trade_path()
    agg = build_aggregator_from_env()
    quotes = agg.quote_all(args.amount, path)
    if not quotes:
        logger.info("No quotes available. Check DEX_PROVIDERS and router addresses.")
        return
    for q in quotes:
        logger.info(f"provider={q.provider} in={q.amount_in} out={q.amount_out} path={q.path}")
    best = max(quotes, key=lambda q: q.amount_out)
    logger.info(f"best={best.provider} out={best.amount_out}")


def cmd_market_best_price(args: argparse.Namespace) -> None:
    from services.marketdata.provider import MarketDataProvider

    path = _require_trade_path()
    amount = float(args.amount)
    md = MarketDataProvider()
    res = md.best_price(path, amount_in_decimal=amount)
    logger.info(f"best_price: provider={res['provider']} price={res['price']:.8f} path={res['path']}")


def cmd_market_diem(args: argparse.Namespace) -> None:
    from services.marketdata.provider import MarketDataProvider

    md = MarketDataProvider()
    res = md.diem_signals()
    logger.info(f"diem_signals: {res}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vvv-agents", description="VVV Agents CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="Check env and prepare local setup")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("issue-key", help="Issue a Venice root key via signed challenge")
    sp.add_argument("--wallet", required=False, help="Wallet address (defaults to active wallet)")
    sp.set_defaults(func=cmd_issue_key)

    sp = sub.add_parser("venice:usage", help="Show Venice usage and rate limits/quota")
    sp.set_defaults(func=cmd_venice_usage)

    sp = sub.add_parser("venice:models", help="List available Venice models")
    sp.set_defaults(func=cmd_venice_models)

    sp = sub.add_parser("venice:signals", help="Fetch Venice VVV/DIEM tokenomic signals")
    sp.set_defaults(func=cmd_venice_signals)

    sp = sub.add_parser("venice:validate-addresses", help="Validate configured token/contract addresses against Venice signals")
    sp.set_defaults(func=cmd_venice_validate_addresses)

    sp = sub.add_parser("run:stakemaster", help="Run a single StakeMaster heartbeat")
    sp.add_argument("--enable-live", action="store_true", default=False, help="Allow live on-chain actions (claim)")
    sp.set_defaults(func=cmd_run_stakemaster)

    sp = sub.add_parser("run:quorum", help="Run quorum-driven workflow (dry-run by default)")
    sp.add_argument("--dry-run", action="store_true", default=False)
    sp.set_defaults(func=cmd_run_quorum)

    sp = sub.add_parser("run:graph", help="Run LangGraph pipeline once with optional broker chat")
    sp.add_argument("--messages", required=False, help="JSON array of chat messages [{role, content}]")
    sp.add_argument("--model", required=False, default=None, help="Model to use for broker routing")
    sp.set_defaults(func=cmd_run_graph)

    sp = sub.add_parser(
        "run:loop",
        help="Run minimal loop with StakeMaster heartbeat; supports sleep/max-cycles/enable-live",
    )
    sp.add_argument("--sleep", default=15, type=float, help="Seconds to sleep between cycles")
    sp.add_argument("--max-cycles", default=3, type=int, help="Maximum cycles to run")
    sp.add_argument("--enable-live", action="store_true", default=False, help="Allow live actions (claim)")
    sp.set_defaults(func=cmd_run_loop)

    sp = sub.add_parser("test:challenge-offline", help="Offline test: sign a dummy challenge and echo payloads")
    sp.set_defaults(func=cmd_test_challenge_offline)

    sp = sub.add_parser("addresses:print", help="Print current Base addresses from env")
    sp.set_defaults(func=cmd_print_addresses)

    sp = sub.add_parser(
        "quotes:compare",
        help="Compare quotes across configured DEX providers for the TRADE_PATH",
    )
    sp.add_argument("--amount", required=True, type=int, help="Input amount (smallest units) to sell")
    sp.set_defaults(func=cmd_quotes_compare)

    sp = sub.add_parser("market:best-price", help="Compute best normalized price for TRADE_PATH with decimal input amount")
    sp.add_argument("--amount", required=False, default=1.0, help="Decimal amount of input token")
    sp.set_defaults(func=cmd_market_best_price)

    sp = sub.add_parser("market:diem", help="Fetch DIEM signals via Venice API")
    sp.set_defaults(func=cmd_market_diem)

    # Data compaction (KV -> SQL)
    sp = sub.add_parser("data:compact-counters", help="Compact KV sliding-window counters into SQL (env-gated)")
    sp.add_argument("--force", action="store_true", default=False, help="Run even if env gating is disabled")
    sp.set_defaults(func=cmd_compact_counters)

    # Data inspection (SQL counters)
    sp = sub.add_parser("counters:show", help="Show aggregated counters for a tenant from SQL")
    sp.add_argument("--tenant", required=True, help="Tenant id")
    sp.add_argument("--scope", required=False, help="Scope filter (e.g., chat)")
    sp.add_argument("--model", required=False, help="Model filter")
    sp.add_argument("--bucket-seconds", type=int, required=False, help="Bucket size filter (seconds)")
    sp.add_argument("--since", required=False, help="ISO8601 or epoch seconds (inclusive)")
    sp.add_argument("--until", required=False, help="ISO8601 or epoch seconds (inclusive)")
    sp.add_argument("--limit", type=int, default=50, help="Max rows to print")
    sp.add_argument("--asc", dest="desc", action="store_false", help="Sort ascending by bucket_start (default desc)")
    sp.add_argument("--json", action="store_true", default=False, help="Output JSON instead of log lines")
    sp.set_defaults(func=cmd_counters_show)

    # Env status (server or local)
    def cmd_env_status(args: argparse.Namespace) -> None:
        base = os.getenv("BROKER_BASE_URL")
        out: Dict[str, Any] = {}
        if base:
            try:
                r = requests.get(base.rstrip("/") + "/v1/env", timeout=5)
                if r.ok:
                    out["server"] = r.json()
                else:
                    logger.warning(f"/v1/env returned {r.status_code}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Could not reach server /v1/env: {e}")
        # Local snapshot (env-only)
        out["local"] = {
            "kv": {
                "redis_configured": bool(os.getenv("REDIS_URL") or os.getenv("KV_REDIS_URL")),
                "replit_db_configured": bool(os.getenv("KV_URL") or os.getenv("REPLIT_DB_URL")),
                "namespace_set": bool(os.getenv("KV_NAMESPACE")),
                "prefix_set": bool(os.getenv("KV_PREFIX")),
            },
            "sql": {
                "env_configured": bool(os.getenv("SQL_DATABASE_URL") or os.getenv("DATABASE_URL") or os.getenv("POSTGRES_HOST")),
            },
            "limiter": {
                "enabled": str(os.getenv("RATE_LIMITS_ENABLED", "false")).lower() in {"1", "true", "yes"},
                "windowSeconds": int((os.getenv("RATE_LIMIT_WINDOW_SECONDS") or "60").strip() or 60),
                "maxRequests": int((os.getenv("RATE_LIMIT_MAX_REQUESTS") or "60").strip() or 60),
            },
            "idempotency": {
                "ttlSeconds": int((os.getenv("IDEMPOTENCY_TTL_SECONDS") or os.getenv("IDEM_TTL_SECONDS") or "300").strip() or 300),
            },
            "metrics": {
                "backend": (os.getenv("METRICS_BACKEND") or "auto").strip().lower(),
                "path": (os.getenv("METRICS_PATH") or "/metrics").strip() or "/metrics",
            },
            "tracing": {
                "enabled": str(os.getenv("LANGCHAIN_TRACING_V2", "false")).lower() in {"1", "true", "yes"},
            },
            "admin": {
                "token_present": bool(os.getenv("BROKER_ADMIN_TOKEN")),
                "required_at_startup": str(os.getenv("BROKER_REQUIRE_ADMIN_TOKEN", "false")).lower() in {"1", "true", "yes", "on"},
            },
        }
        try:
            import json as _json

            print(_json.dumps(out, indent=2))
        except Exception:
            print(out)

    sp = sub.add_parser("env:status", help="Print environment status (server /v1/env if available plus local snapshot)")
    sp.set_defaults(func=cmd_env_status)

    # Broker admin commands
    sp = sub.add_parser("broker:tenants:list", help="List all tenants (admin)")
    sp.set_defaults(func=cmd_broker_tenants_list)

    sp = sub.add_parser("broker:limits:get", help="Get per-tenant broker limits (admin)")
    sp.add_argument("--tenant", required=True, help="Tenant id")
    sp.set_defaults(func=cmd_broker_limits_get)

    sp = sub.add_parser("broker:limits:set", help="Set per-tenant broker limits (admin)")
    sp.add_argument("--tenant", required=True, help="Tenant id")
    sp.add_argument("--window", type=int, required=False, help="Window seconds")
    sp.add_argument("--max", type=int, required=False, help="Max requests per window")
    sp.add_argument("--label", type=str, required=False, help="Label (e.g., premium, basic)")
    sp.set_defaults(func=cmd_broker_limits_set)

    # Idempotency admin
    sp = sub.add_parser("idem:purge", help="Purge idempotency keys by prefix")
    sp.add_argument("--prefix", required=True, help="Prefix like 'idem:chat:t-123'")
    sp.set_defaults(func=cmd_idem_purge)

    # Limiter probe
    sp = sub.add_parser("probe:limits", help="Probe /v1/chat throughput and 429s vs limits")
    sp.add_argument("--rps", type=float, default=float(os.getenv("PROBE_RPS", "10")))
    sp.add_argument("--duration", type=int, default=int(os.getenv("PROBE_DURATION", "30")))
    sp.add_argument("--concurrency", type=int, default=int(os.getenv("PROBE_CONCURRENCY", "20")))
    sp.add_argument("--model", required=False, default=os.getenv("PROBE_MODEL") or None)
    sp.add_argument("--message", required=False, default=os.getenv("PROBE_MESSAGE", "hello"))
    sp.add_argument("--no-idempotency", action="store_true", default=_env_flag("PROBE_NO_IDEMPOTENCY", False))
    sp.add_argument("--timeout", type=float, default=float(os.getenv("PROBE_TIMEOUT", "10")))
    sp.add_argument("--base-url", required=False, default=None, help="Override broker base URL (defaults to BROKER_BASE_URL)")
    sp.add_argument("--auth-bearer", required=False, default=os.getenv("PROBE_AUTH_BEARER") or None, help="Tenant subkey")
    sp.add_argument("--tenant", dest="tenant_id", required=False, default=os.getenv("PROBE_TENANT_ID") or None, help="Tenant id for admin mode")
    sp.set_defaults(func=cmd_probe_limits)

    # Orchestrator loop
    sp = sub.add_parser("run:orchestrator", help="Run orchestrator loop for ArbiDiem with persistence and backoff")
    sp.add_argument("--dry-run", action="store_true", default=True)
    sp.add_argument("--max-cycles", default="0")
    sp.add_argument("--interval", default="5.0", help="Loop interval seconds")
    sp.add_argument("--backoff", default="1.0", help="Initial backoff seconds on error")
    sp.add_argument("--max-backoff", default="60.0", help="Max backoff seconds")
    sp.set_defaults(func=cmd_run_orchestrator)

    # Broker revoke tenant key (admin)
    def cmd_broker_tenant_revoke(args: argparse.Namespace) -> None:
        tid = args.tenant
        url = f"{_broker_base_url()}/v1/tenants/{tid}/revoke"
        r = requests.post(url, headers=_admin_headers(), timeout=10)
        if not r.ok:
            logger.error(f"revoke failed: {r.status_code} {r.text}")
            return
        logger.info(f"revoked tenant {tid}: {r.json()}")

    sp = sub.add_parser("broker:tenants:revoke", help="Revoke a tenant's Venice subkey (admin)")
    sp.add_argument("--tenant", required=True, help="Tenant id")
    sp.set_defaults(func=cmd_broker_tenant_revoke)

    # Venice keys cleanup (by description prefix)
    def cmd_venice_keys_cleanup(args: argparse.Namespace) -> None:
        from libs.venice_sdk.client import VeniceClient

        prefix = args.prefix or ""
        dry_run = bool(args.dry_run)
        # Prefer parent key for full list/delete permissions
        api_key = os.getenv("VENICE_PARENT_KEY") or os.getenv("VENICE_API_KEY")
        vc = VeniceClient(api_key=api_key)
        try:
            data = vc.list_api_keys()
        except Exception as e:  # noqa: BLE001
            logger.error(f"list api keys failed: {e}")
            return
        # Expect either list or {"data": [...]} shape
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("data") or data.get("items") or []
        else:
            logger.error("unexpected response shape from /api_keys")
            return

        targets = []
        for it in items:
            desc = str(it.get("description") or it.get("label") or "")
            kid = it.get("id") or it.get("keyId") or it.get("apiKeyId") or it.get("api_key_id")
            if not kid:
                continue
            if prefix and not desc.startswith(prefix):
                continue
            targets.append(str(kid))
        if not targets:
            logger.info("no keys matched prefix")
            return
        logger.info(f"matched {len(targets)} keys for prefix '{prefix}'")
        deleted = 0
        for kid in targets:
            if dry_run:
                logger.info(f"dry-run: would delete key {kid}")
                continue
            try:
                vc.delete_api_key(kid)
                deleted += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"delete failed for {kid}: {e}")
        logger.info(f"cleanup complete: deleted={deleted} matched={len(targets)} dry_run={dry_run}")

    sp = sub.add_parser("venice:keys:cleanup", help="List/delete Venice API keys by description prefix")
    sp.add_argument("--prefix", required=False, default="", help="Description prefix to match (e.g., 'T1')")
    sp.add_argument("--dry-run", action="store_true", default=False, help="Only list keys that would be deleted")
    sp.set_defaults(func=cmd_venice_keys_cleanup)

    # Venice OpenAPI probe
    def cmd_venice_probe_openapi(args: argparse.Namespace) -> None:
        base = (args.base_url or "https://api.venice.ai").rstrip("/")
        timeout = float(args.timeout)
        session = requests.Session()
        spec = None
        spec_loc = None
        for path in ("/openapi.json", "/api/openapi.json"):
            try:
                r = session.get(base + path, timeout=timeout)
                if r.ok:
                    spec = r.json()
                    spec_loc = path
                    break
            except Exception:
                continue
        if spec is None:
            logger.error("Failed to fetch OpenAPI from %s (tried /openapi.json and /api/openapi.json)", base)
            return
        servers = spec.get("servers") or []
        server_url = None
        if servers and isinstance(servers, list):
            server_url = servers[0].get("url") if isinstance(servers[0], dict) else None
        # Compute recommended VENICE_API_BASE_URL
        if server_url and isinstance(server_url, str):
            if server_url.startswith("http://") or server_url.startswith("https://"):
                rec_base = server_url.rstrip("/")
            else:
                rec_base = base + ("/" + server_url.lstrip("/"))
        else:
            # Fallback based on where we found spec
            rec_base = base if spec_loc == "/openapi.json" else base + "/api"
        # Probe for key-related paths
        paths = spec.get("paths") or {}
        rec_subkey_path = None
        rec_root_path = None
        rec_challenge_path = None
        # Prefer /api_keys for subkey; else legacy /v1/keys/sub or /v1/keys/subkey
        for candidate in ("/api_keys", "/v1/keys/sub", "/v1/keys/subkey"):
            item = paths.get(candidate)
            if item and ("post" in {k.lower() for k in item.keys()}):
                rec_subkey_path = candidate
                break
        # Web3 root key flow candidates
        for candidate in ("/api_keys/generate_web3_key", "/v1/keys/generate_web3_key"):
            item = paths.get(candidate)
            if item:
                rec_root_path = candidate
                rec_challenge_path = candidate
                break
        print("# Recommended environment exports:")
        print(f"export VENICE_API_BASE_URL={rec_base}")
        if rec_subkey_path:
            print(f"export VENICE_CREATE_SUBKEY_PATH={rec_subkey_path}")
        if rec_root_path:
            print(f"export VENICE_CREATE_ROOT_PATH={rec_root_path}")
        if rec_challenge_path:
            print(f"export VENICE_CHALLENGE_PATH={rec_challenge_path}")
        # Also print JSON summary for programmatic use
        summary: Dict[str, Any] = {
            "base": base,
            "spec_location": spec_loc,
            "recommended_base": rec_base,
            "create_subkey_path": rec_subkey_path,
            "create_root_path": rec_root_path,
            "challenge_path": rec_challenge_path,
        }
        try:
            import json as _json

            print(_json.dumps(summary, separators=(",", ":")))
        except Exception:
            pass

    sp = sub.add_parser("venice:probe-openapi", help="Detect VENICE_API_BASE_URL and key paths from OpenAPI")
    sp.add_argument("--base-url", required=False, default=None, help="Venice host (e.g., https://api.venice.ai)")
    sp.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds")
    sp.set_defaults(func=cmd_venice_probe_openapi)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()


