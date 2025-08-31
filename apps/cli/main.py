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
        from db.session import get_engine
        from sqlmodel import Session, select
        from db.models import Counter
        import json
    except Exception as e:  # noqa: BLE001
        logger.error(f"Missing dependencies for compaction: {e}")
        return

    kv = KVStore()

    # Determine source prefix; default matches limiter keys
    src_prefix = os.getenv("KV_COMPACTION_PREFIX", "rl:tenant:")
    keys = kv.keys(src_prefix)
    if not keys:
        logger.info("No KV counter keys found to compact.")
        return

    engine = get_engine()
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
        from db.session import get_engine
        from db.models import Counter
    except Exception as e:  # noqa: BLE001
        logger.error(f"SQL dependencies not available: {e}")
        return

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

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
