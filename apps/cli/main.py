from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from apps._path import REPO_ROOT
from libs.runtime.preflight import ensure_agentkit_installed, validate_live_wallet_env
from libs.telemetry.logger import get_logger
from libs.venice_sdk.client import VeniceClient
from scripts.wallet_cli import (
    cmd_address as wallet_cmd_address,
    cmd_send as wallet_cmd_send,
    cmd_sign as wallet_cmd_sign,
    cmd_sweep as wallet_cmd_sweep,
    cmd_transfer_cold as wallet_cmd_transfer_cold,
)
from services.venice_keys.manager import KeyManager
from services.wallet.provider import WalletError


def _load_dotenv() -> None:
    """Best-effort loading of repo-level dotenv files for CLI usage."""

    docker_env = REPO_ROOT / ".env.docker"
    local_env = REPO_ROOT / "docker" / ".env.local"
    try:
        from libs.env import load_dotenv_if_present  # type: ignore
    except Exception:
        try:
            from dotenv import load_dotenv
        except Exception:
            return

        load_dotenv(dotenv_path=str(REPO_ROOT / ".env"), override=False)
        if docker_env.exists():
            load_dotenv(dotenv_path=str(docker_env), override=True)
        if local_env.exists():
            load_dotenv(dotenv_path=str(local_env), override=True)
        return

    load_dotenv_if_present(path=str(REPO_ROOT / ".env"), override=False)
    if docker_env.exists():
        load_dotenv_if_present(path=str(docker_env), override=True)
    if local_env.exists():
        load_dotenv_if_present(path=str(local_env), override=True)


_load_dotenv()


logger = get_logger("cli")


def _is_test_or_ci_environment() -> bool:
    """Detect if running in a test or CI environment.
    
    Returns True if any of the following conditions are met:
    - CI environment variable is set (common in CI systems)
    - PYTEST_CURRENT_TEST is set (pytest is running)
    - TESTING environment variable is set to truthy value
    - pytest module is imported (indicating test execution)
    """
    if os.getenv("CI") or os.getenv("PYTEST_CURRENT_TEST"):
        return True
    
    testing_env = os.getenv("TESTING", "").strip().lower()
    if testing_env in {"1", "true", "yes", "on"}:
        return True
    
    # Check if pytest is imported (indicates test execution)
    if "pytest" in sys.modules:
        return True
    
    return False


# Safe defaults for local tooling/CI gates - only set in test/CI environments
# to prevent overriding production security configurations.
if _is_test_or_ci_environment():
    os.environ.setdefault("BROKER_REQUIRE_ADMIN_TOKEN", "true")
    os.environ.setdefault("BROKER_ADMIN_TOKEN", "test-admin")
os.environ.setdefault("VENICE_API_BASE_URL", "https://api.venice.ai/api/v1")

def _env_flag(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(raw)
    except Exception:
        return float(default)


def _wrap_wallet_cmd(func):
    def _runner(args: argparse.Namespace) -> None:
        try:
            func(args)
        except WalletError as exc:
            logger.error(f"wallet: {exc}")
            raise SystemExit(2) from exc

    return _runner


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
    # VVV metrics
    try:
        out["vvv"] = client.get_vvv_metrics()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to fetch VVV metrics: {e}")
    # DIEM via rate-limits balances/quotas
    try:
        limits = client.get_rate_limits()
        obj = limits or {}
        data = obj.get("data") if isinstance(obj, dict) else None
        if isinstance(data, dict):
            balances = data.get("balances") or {}
        else:
            balances = obj.get("balances") or {}
        out["diem"] = {"balances": balances, "diem": balances.get("DIEM") or balances.get("diem"), "raw": limits}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to fetch DIEM balance/quota: {e}")
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
        # Try legacy /vvv aggregate if available to extract addresses
        vvv = client.get_vvv_signals()
        found |= _extract_addresses(vvv)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not fetch /v1/vvv: {e}")

    logger.info(f"discovered_addresses={sorted(found)}")
    for k, v in env_addrs.items():
        if not v:
            logger.info(f"{k} is unset; cannot validate")
            continue
        status = "MATCH" if v in found else "MISMATCH"
        logger.info(f"{k}={v} -> {status}")


def cmd_run_stakemaster(args: argparse.Namespace) -> None:
    try:
        ensure_agentkit_installed(logger)
    except RuntimeError as exc:
        raise SystemExit(2) from exc

    if getattr(args, "enable_live", False):
        missing_env = validate_live_wallet_env(
            ["BASE_RPC_URL", "VVV_TOKEN_ADDRESS", "VVV_STAKING_ADDRESS"],
            logger,
        )
        if missing_env:
            raise SystemExit(2)

    # Lazy imports to avoid web3 deps for other commands
    from services.staking.client import StakingService
    from libs.agentkit_ext.actions import VVVActions
    from agents.stake_master.agent import StakeMaster
    from services.marketdata.provider import MarketDataProvider

    stake = StakingService(VVVActions())
    try:
        market = MarketDataProvider()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Market data unavailable for StakeMaster valuation: {exc}")
        market = None
    agent = StakeMaster(stake, market=market)
    agent.run_once(live=getattr(args, "enable_live", False))


def cmd_run_loop(args: argparse.Namespace) -> None:
    """Run the v1 single-loop orchestrator (StakeMaster → ArbiDiem → CapacityBroker)."""

    try:
        ensure_agentkit_installed(logger)
    except RuntimeError as exc:
        raise SystemExit(2) from exc

    from services.staking.client import StakingService
    from libs.agentkit_ext.actions import VVVActions
    from agents.stake_master.agent import StakeMaster
    from services.marketdata.provider import MarketDataProvider
    from services.diem.client import DIEMService
    from libs.dex.providers import build_aggregator_from_env
    from agents.arbi_diem.agent import ArbiDiem
    from services.venice_keys.manager import KeyManager
    from libs.venice_sdk.client import VeniceClient
    from agents.capacity_broker.agent import CapacityBroker
    from agents.ai_treasurer.agent import AITreasurer
    from agents.quorum import build_default_coordinator
    from services.memory import MemoryStore, ReflectionEngine
    from agents.reflex.guardian import ReflexGuardian
    from graph.workflows.orchestrator import SingleLoopOrchestrator
    from services.portfolio.inventory import PortfolioInventory

    env_progressive = _env_flag("STAKEMASTER_PROGRESSIVE_ENABLE", True)
    arg_progressive = getattr(args, "progressive_live", None)
    progressive = env_progressive if arg_progressive is None else bool(arg_progressive)
    explicit_live = bool(getattr(args, "enable_live", False))
    live_target = explicit_live or progressive
    dry_run = not explicit_live

    if explicit_live or progressive:
        missing_env = validate_live_wallet_env(
            ["BASE_RPC_URL", "VVV_TOKEN_ADDRESS", "VVV_STAKING_ADDRESS"],
            logger,
        )
        if missing_env:
            raise SystemExit(2)

    market = MarketDataProvider()
    try:
        stake_agent = StakeMaster(StakingService(VVVActions()), market=market)
    except (EnvironmentError, RuntimeError) as exc:
        logger.error(f"StakeMaster startup failed: {exc}")
        raise SystemExit(2) from exc
    aggregator = build_aggregator_from_env() if live_target else None
    diem_service = DIEMService(aggregator, market_data=market)
    arbi_agent = ArbiDiem(diem_service, market=market)
    key_manager = KeyManager(VeniceClient())
    capacity_agent = CapacityBroker(key_manager)
    ai_treasurer = AITreasurer()
    quorum = build_default_coordinator() if _env_flag("QUORUM_ENABLE", True) else None

    memory_store = MemoryStore()
    reflection = ReflectionEngine()
    allow_inactive = bool(getattr(args, "allow_inactive_stake", False))
    if not allow_inactive:
        allow_inactive = str(os.getenv("REFLEX_ALLOW_INACTIVE_STAKE", "")).strip().lower() in {"1", "true", "yes", "on"}
    reflex_guard = ReflexGuardian(require_active_stake=not allow_inactive)

    portfolio_inventory = PortfolioInventory(marketdata_provider=market) if live_target else None

    orchestrator = SingleLoopOrchestrator(
        stake_master=stake_agent,
        arbi=arbi_agent,
        capacity_broker=capacity_agent,
        market=market,
        quorum=quorum,
        ai_treasurer=ai_treasurer,
        parent_key=os.getenv("VENICE_API_KEY"),
        memory_store=memory_store,
        reflection=reflection,
        reflex_guard=reflex_guard,
        portfolio_inventory=portfolio_inventory,
    )

    orchestrator.run_loop(
        interval_s=float(args.sleep),
        max_cycles=int(args.max_cycles),
        dry_run=dry_run,
        enable_live=explicit_live,
        progressive_live=progressive,
        mint_rate=_env_float("DIEM_MINT_RATE", 1.0),
    )


def cmd_run_quorum(args: argparse.Namespace) -> None:
    # Lazy imports to avoid web3 deps for non-quorum commands
    from services.marketdata.provider import MarketDataProvider
    from services.diem.client import DIEMService
    from libs.dex.providers import build_aggregator_from_env
    from agents.arbi_diem.agent import ArbiDiem
    from graph.workflows.revenue_streams import DiemMintSellWorkflow

    market = MarketDataProvider()
    diem = DIEMService(build_aggregator_from_env(), market_data=market)
    arbi = ArbiDiem(diem, market=market)
    flow = DiemMintSellWorkflow(market=market, arbi=arbi)
    decided = flow.run_once(dry_run=args.dry_run)
    logger.info(f"Quorum/flow decision (dry={args.dry_run}): {decided}")


def cmd_run_orchestrator(args: argparse.Namespace) -> None:
    """Run the orchestrator loop coordinating ArbiDiem decisions with persistence."""

    from services.marketdata.provider import MarketDataProvider
    from services.diem.client import DIEMService
    from libs.dex.providers import build_aggregator_from_env
    from agents.arbi_diem.agent import ArbiDiem
    from graph.workflows.orchestrator import Orchestrator

    market = MarketDataProvider()
    # Avoid initializing DEX/web3 in dry-run to prevent platform-specific crashes
    diem = DIEMService(build_aggregator_from_env() if not args.dry_run else None, market_data=market)
    arbi = ArbiDiem(diem, market=market)
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

def _require_trade_path():
    from services.marketdata.provider import MarketDataProvider

    path_env = os.getenv("TRADE_PATH")
    if not path_env:
        raise SystemExit("TRADE_PATH must be set for quoting")
    md = MarketDataProvider()
    try:
        return md._parse_route_spec(path_env)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"invalid TRADE_PATH: {exc}") from exc


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
    res = md.diem_balance()
    logger.info(f"diem_balance: {res}")


def cmd_market_validate_trade_paths(args: argparse.Namespace) -> None:
    from services.marketdata.provider import MarketDataProvider

    md = MarketDataProvider()
    try:
        plans = md._collect_trade_paths()  # type: ignore[attr-defined]
    except Exception:
        plans = []
    if not plans:
        try:
            plans = [md._route_from_env("TRADE_PATH")]  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.error(f"No trade paths configured: {exc}")
            raise SystemExit(1) from exc
    seen: set[tuple[str, ...]] = set()
    reports = []
    for plan in plans:
        tokens = tuple(str(t) for t in plan.tokens)
        if not tokens or tokens in seen:
            continue
        seen.add(tokens)
        try:
            discovery = md.discover_trade_path(list(tokens))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"discover_trade_path failed for {'->'.join(tokens)}: {exc}")
            raise SystemExit(1) from exc
        hop_details = []
        all_hops_ok = True
        for hop in discovery.get("hops", []) or []:
            venues = []
            hop_ok = False
            for venue_key, venue_label in (
                ("uniswap_v2", "uniswap_v2"),
                ("aerodrome_vol", "aerodrome_vol"),
                ("aerodrome_stable", "aerodrome_stable"),
            ):
                ent = hop.get(venue_key) or {}
                pair = ent.get("pair")
                reserves = ent.get("reserves")
                has_reserves = False
                if isinstance(reserves, (tuple, list)) and len(reserves) >= 2:
                    try:
                        has_reserves = (int(reserves[0]) > 0) and (int(reserves[1]) > 0)
                    except Exception:
                        has_reserves = False
                if pair and has_reserves:
                    hop_ok = True
                venues.append({
                    "venue": venue_label,
                    "pair": pair,
                    "reserves": reserves,
                    "has_liquidity": bool(pair) and has_reserves,
                })
            hop_details.append({
                "from": hop.get("from"),
                "to": hop.get("to"),
                "ok": hop_ok,
                "venues": venues,
            })
            if not hop_ok:
                all_hops_ok = False
        price_preview = None
        price_error = None
        if not args.skip_quotes:
            try:
                preview = md.best_price(plan, amount_in_decimal=float(args.amount))
                price_preview = float(preview.get("price")) if preview else None
            except Exception as exc:  # noqa: BLE001
                price_error = str(exc)
        reports.append({
            "tokens": list(tokens),
            "hops": hop_details,
            "ok": all_hops_ok,
            "price": price_preview,
            "price_error": price_error,
        })

    if not reports:
        logger.error("No unique trade paths found in configuration")
        raise SystemExit(1)

    all_ok = True
    for idx, info in enumerate(reports, start=1):
        print(f"route[{idx}]: {' -> '.join(info['tokens'])}")
        if info.get("price") is not None:
            print(f"  preview_price={info['price']:.8f} (amount={args.amount})")
        elif info.get("price_error"):
            print(f"  preview_error={info['price_error']}")
        for hop_idx, hop in enumerate(info.get("hops") or [], start=1):
            status = "ok" if hop.get("ok") else "missing"
            print(f"  hop[{hop_idx}] {hop.get('from')} -> {hop.get('to')} :: {status}")
            for venue in hop.get("venues") or []:
                pair = venue.get("pair")
                if pair:
                    reserves = venue.get("reserves")
                    if isinstance(reserves, (tuple, list)) and len(reserves) >= 2:
                        print(f"    {venue['venue']}: pair={pair} reserves={reserves[0]},{reserves[1]}")
                    else:
                        print(f"    {venue['venue']}: pair={pair} reserves=(n/a)")
                else:
                    print(f"    {venue['venue']}: (no pair)")
        all_ok = all_ok and bool(info.get("ok"))
    if not all_ok:
        raise SystemExit(1)


def cmd_market_pools_watch(args: argparse.Namespace) -> None:
    from services.marketdata import pools

    if args.interval is not None:
        os.environ["POOL_WATCH_INTERVAL_SECONDS"] = str(int(args.interval))
    if args.backfill is not None:
        os.environ["POOL_WATCH_BACKFILL_BLOCKS"] = str(int(args.backfill))
    if args.span is not None:
        os.environ["POOL_WATCH_BLOCK_SPAN"] = str(int(args.span))
    if args.once:
        os.environ["POOL_WATCH_ONCE"] = "true"
    try:
        pools.run_pool_watch_loop()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"pool watcher failed: {exc}")
        raise SystemExit(1) from exc


def cmd_market_pools_list(args: argparse.Namespace) -> None:
    from services.marketdata import pools

    try:
        rows = pools.list_pools(factory=args.factory, token=args.token, limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"list pools failed: {exc}")
        raise SystemExit(1) from exc

    if args.json:
        import json as _json

        data = []
        for row in rows:
            data.append(
                {
                    "pool": row.pool_address,
                    "factory": row.factory_type,
                    "factory_address": row.factory_address,
                    "token0": row.token0,
                    "token1": row.token1,
                    "fee": row.fee,
                    "stable": row.stable,
                    "block": row.block_number,
                    "tx": row.tx_hash,
                    "discovered_at": row.discovered_at.isoformat() if row.discovered_at else None,
                }
            )
        print(_json.dumps(data, separators=(",", ":")))
        return

    if not rows:
        logger.info("no pools discovered yet")
        return

    for row in rows:
        extras: List[str] = []
        if row.fee is not None:
            extras.append(f"fee={row.fee}")
        if row.stable is not None:
            extras.append(f"stable={row.stable}")
        if row.tick_spacing is not None:
            extras.append(f"tick_spacing={row.tick_spacing}")
        if row.block_number is not None:
            extras.append(f"block={row.block_number}")
        if row.tx_hash:
            extras.append(f"tx={row.tx_hash}")
        label = " ".join(extras) if extras else ""
        logger.info(
            "%s %s->%s factory=%s %s",
            row.pool_address,
            row.token0,
            row.token1,
            row.factory_type,
            label,
        )


def cmd_market_routes_suggest(args: argparse.Namespace) -> None:
    from services.marketdata import pools
    from services.marketdata.provider import MarketDataProvider

    md = MarketDataProvider()

    def _coerce(value: str) -> str:
        raw = value.strip()
        if raw.lower().startswith("0x"):
            return raw
        addr = md._address_for_symbol(raw.upper())  # type: ignore[attr-defined]
        if not addr:
            raise SystemExit(f"Unknown token or address: {raw}")
        return addr

    base_addr = _coerce(args.base)
    quote_addr = _coerce(args.quote)
    limit = int(args.limit) if args.limit else 8

    try:
        routes = md.route_candidates(base_addr, quote_addr)[:limit]
    except Exception as exc:  # noqa: BLE001
        logger.error(f"route suggestion failed: {exc}")
        raise SystemExit(1) from exc

    if args.json:
        import json as _json

        print(_json.dumps(pools.routes_as_dict(routes), separators=(",", ":")))
        return

    if not routes:
        logger.info("no discovered routes between %s and %s", base_addr, quote_addr)
        return

    for idx, route in enumerate(routes[:limit], start=1):
        tokens = []
        for tok in route.tokens:
            tokens.append(md._norm_symbol_label(tok))
        fee_info = [hop.fee for hop in route.hops if hop.fee is not None]
        if fee_info:
            logger.info("route[%s]: %s (fees=%s)", idx, " -> ".join(tokens), fee_info)
        else:
            logger.info("route[%s]: %s", idx, " -> ".join(tokens))


def cmd_diem_mint(args: argparse.Namespace) -> None:
    """Mint DIEM via DIEMService with optional dry-run and idempotency."""
    from services.diem.client import DIEMService

    svc = DIEMService()
    amount = int(args.amount)
    res = svc.mint(amount, dry_run=bool(args.dry_run), idem_key=args.idem_key, corr_id=args.corr_id)
    logger.info(f"diem:mint amount={amount} dry_run={args.dry_run} res={res}")


def cmd_diem_burn(args: argparse.Namespace) -> None:
    """Burn DIEM via DIEMService with optional dry-run and idempotency."""
    from services.diem.client import DIEMService

    svc = DIEMService()
    amount = int(args.amount)
    res = svc.burn(amount, dry_run=bool(args.dry_run), idem_key=args.idem_key, corr_id=args.corr_id)
    logger.info(f"diem:burn amount={amount} dry_run={args.dry_run} res={res}")


def cmd_ci_gate(args: argparse.Namespace) -> None:
    """CI/Health gate: validates readiness and prod defaults.

    - If BROKER_BASE_URL is set, queries /v1/env and enforces:
      venice.ready=true, signals.offline=false, admin.token_present=true,
      admin.required_at_startup=true.
    - Otherwise, performs local env checks: admin token requirement, Venice base URL,
      offline signals disabled, and basic CORS allowlist when enabled.
    Exits non-zero on violations.
    """
    import sys as _sys
    import requests as _rq

    violations: list[str] = []
    base = os.getenv("BROKER_BASE_URL")
    if base:
        url = base.rstrip("/") + "/v1/env"
        try:
            r = _rq.get(url, timeout=5)
            if not r.ok:
                violations.append(f"server /v1/env returned {r.status_code}")
            else:
                env = r.json()
                ven = env.get("venice") or {}
                adm = env.get("admin") or {}
                sig = env.get("signals") or {}
                if not bool(ven.get("ready")):
                    violations.append("venice.ready=false")
                if bool(sig.get("offline")):
                    violations.append("signals.offline=true")
                if not bool(adm.get("token_present")):
                    violations.append("admin.token_present=false")
                if not bool(adm.get("required_at_startup")):
                    violations.append("admin.required_at_startup=false")
        except Exception as e:  # noqa: BLE001
            violations.append(f"server unreachable: {e}")
    else:
        # Local checks only
        def _flag(name: str, default: bool = False) -> bool:
            v = os.getenv(name)
            if v is None:
                return default
            return str(v).strip().lower() in {"1", "true", "yes", "on"}

        if not _flag("BROKER_REQUIRE_ADMIN_TOKEN", default=False):
            violations.append("BROKER_REQUIRE_ADMIN_TOKEN=false")
        if _flag("BROKER_REQUIRE_ADMIN_TOKEN", default=False) and not os.getenv("BROKER_ADMIN_TOKEN"):
            violations.append("BROKER_ADMIN_TOKEN missing while required")
        if _flag("VENICE_OFFLINE_SIGNALS", default=False):
            violations.append("VENICE_OFFLINE_SIGNALS=true")
        ven_base = os.getenv("VENICE_API_BASE_URL") or ""
        if "/api/v1" not in ven_base:
            violations.append("VENICE_API_BASE_URL must include /api/v1")
        if _flag("CORS_ENABLED", default=False):
            origins = [o.strip() for o in (os.getenv("CORS_ALLOW_ORIGINS") or "").split(",") if o.strip()]
            if not origins or any(o == "*" for o in origins):
                violations.append("CORS_ALLOW_ORIGINS must be set without wildcard when CORS_ENABLED=true")

    if violations:
        print("ci:gate failed:", file=_sys.stderr)
        for v in violations:
            print(f" - {v}", file=_sys.stderr)
        _sys.exit(2)
    print("ci:gate ok")


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

    sp = sub.add_parser("venice:signals", help="Fetch Venice VVV metrics and DIEM balance/quota")
    sp.set_defaults(func=cmd_venice_signals)

    sp = sub.add_parser("venice:wallet:address", help="Print the active hot wallet address")
    sp.set_defaults(func=_wrap_wallet_cmd(wallet_cmd_address))

    sp = sub.add_parser("venice:wallet:sign", help="Sign a plaintext message with the hot wallet")
    sp.add_argument("message", help="Plaintext message to sign")
    sp.set_defaults(func=_wrap_wallet_cmd(wallet_cmd_sign))

    sp = sub.add_parser("venice:wallet:send", help="Send a value transfer or contract call via the hot wallet")
    sp.add_argument("--to", required=True, help="Destination address")
    sp.add_argument("--value", required=True, help="Value in wei (decimal or 0x hex)")
    sp.add_argument("--data", default=None, help="Optional hex data payload")
    sp.set_defaults(func=_wrap_wallet_cmd(wallet_cmd_send))

    sp = sub.add_parser(
        "venice:wallet:transfer-cold",
        help="Bridge funds from the cold wallet into the hot wallet",
    )
    sp.add_argument("amount", help="Amount in wei to bridge into the hot wallet")
    sp.add_argument(
        "--cold-key",
        dest="cold_key",
        default=None,
        help="Cold wallet private key override (uses COLD_WALLET_PRIVATE_KEY when omitted)",
    )
    sp.set_defaults(func=_wrap_wallet_cmd(wallet_cmd_transfer_cold))

    sp = sub.add_parser(
        "venice:wallet:sweep",
        help="Sweep excess hot wallet balance back to the cold wallet",
    )
    sp.add_argument("min_balance", help="Minimum wei balance to retain in the hot wallet")
    sp.add_argument(
        "--cold-address",
        dest="cold_address",
        default=None,
        help="Cold wallet address override (uses COLD_WALLET_ADDRESS when omitted)",
    )
    sp.add_argument(
        "--gas-buffer",
        dest="gas_buffer",
        default=None,
        help="Gas buffer in wei (defaults to WALLET_SWEEP_GAS_BUFFER_WEI or auto)",
    )
    sp.set_defaults(func=_wrap_wallet_cmd(wallet_cmd_sweep))

    # Friendly alias for the OpenAPI probe
    def cmd_venice_probe(args: argparse.Namespace) -> None:
        base = args.base_url or "https://api.venice.ai"
        timeout = float(args.timeout)
        # Reuse internal logic from venice:probe-openapi
        try:
            import requests as _rq
            session = _rq.Session()
            spec = None
            spec_loc = None
            for path in ("/openapi.json", "/api/openapi.json"):
                try:
                    r = session.get(base.rstrip('/') + path, timeout=timeout)
                    if r.ok:
                        spec = r.json()
                        spec_loc = path
                        break
                except Exception:
                    continue
            if spec is None:
                logger.error(f"Failed to fetch OpenAPI from {base}")
                return
            servers = spec.get("servers") or []
            server_url = servers[0].get("url") if servers and isinstance(servers[0], dict) else None
            if server_url and isinstance(server_url, str):
                rec_base = server_url.rstrip('/') if server_url.startswith(('http://','https://')) else base.rstrip('/') + '/' + server_url.lstrip('/')
            else:
                rec_base = base.rstrip('/') if spec_loc == '/openapi.json' else base.rstrip('/') + '/api'
            paths = spec.get('paths') or {}
            def _first(cands):
                return next((c for c in cands if c in paths), None)
            sub_path = _first(["/api_keys","/v1/keys/sub","/v1/keys/subkey"]) or "/api_keys"
            root_path = _first(["/api_keys/generate_web3_key","/v1/keys/generate_web3_key"]) or "/api_keys/generate_web3_key"
            vvv_path = "/vvv" if "/vvv" in paths else ("/signals/vvv" if "/signals/vvv" in paths else "/vvv")
            print("# Recommended environment exports:")
            print(f"export VENICE_API_BASE_URL={rec_base}")
            print(f"export VENICE_CREATE_SUBKEY_PATH={sub_path}")
            print(f"export VENICE_CREATE_ROOT_PATH={root_path}")
            print(f"export VENICE_VVV_PATH={vvv_path}")
            # Explicit VVV metrics endpoints (if your deployment supports them)
            if "/vvv/circulatingsupply" in paths:
                print("export VENICE_VVV_CIRC_PATH=/vvv/circulatingsupply")
            if "/vvv/utilization" in paths:
                print("export VENICE_VVV_UTIL_PATH=/vvv/utilization")
            if "/vvv/staking_yield" in paths:
                print("export VENICE_VVV_YIELD_PATH=/vvv/staking_yield")
        except Exception as e:  # noqa: BLE001
            logger.error(f"probe failed: {e}")

    sp = sub.add_parser("venice:validate-addresses", help="Validate configured token/contract addresses against Venice signals")
    sp.set_defaults(func=cmd_venice_validate_addresses)

    sp = sub.add_parser("venice:probe", help="Probe Venice OpenAPI and suggest exports")
    sp.add_argument("--base-url", required=False, default=None, help="Venice host (e.g., https://api.venice.ai)")
    sp.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds")
    sp.set_defaults(func=cmd_venice_probe)

    # Backward-compat: accept `venice` as a shorthand for the probe
    sp = sub.add_parser("venice", help="Alias for venice:probe")
    sp.add_argument("--base-url", required=False, default=None, help="Venice host (e.g., https://api.venice.ai)")
    sp.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds")
    sp.set_defaults(func=cmd_venice_probe)

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
        help="Run v1 single-loop orchestrator (StakeMaster → ArbiDiem → CapacityBroker); supports sleep/max-cycles/enable-live",
    )
    sp.add_argument("--sleep", default=15, type=float, help="Seconds to sleep between cycles")
    sp.add_argument("--max-cycles", default=3, type=int, help="Maximum cycles to run")
    sp.add_argument("--enable-live", action="store_true", default=False, help="Allow live actions (claim, mint, burn)")
    sp.add_argument("--dry-run", action="store_true", default=False, help="Run without on-chain actions (default)")
    sp.add_argument("--allow-inactive-stake", action="store_true", default=False, help="Skip reflex active-stake requirement (testing)")
    sp.add_argument("--progressive-live", dest="progressive_live", action="store_true", help="Enable progressive live escalation after healthy heartbeats")
    sp.add_argument("--no-progressive-live", dest="progressive_live", action="store_false", help="Disable progressive live escalation explicitly")
    sp.set_defaults(progressive_live=None)
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

    sp = sub.add_parser("market:pools:watch", help="Watch configured factories for new pools and persist catalog")
    sp.add_argument("--interval", type=int, default=None, help="Override POOL_WATCH_INTERVAL_SECONDS")
    sp.add_argument("--backfill", type=int, default=None, help="Override POOL_WATCH_BACKFILL_BLOCKS")
    sp.add_argument("--span", type=int, default=None, help="Override POOL_WATCH_BLOCK_SPAN")
    sp.add_argument("--once", action="store_true", default=False, help="Run a single synchronization pass")
    sp.set_defaults(func=cmd_market_pools_watch)

    sp = sub.add_parser("market:pools:list", help="List pools discovered via factory watcher")
    sp.add_argument("--factory", required=False, help="Filter by factory type or address")
    sp.add_argument("--token", required=False, help="Filter by token address")
    sp.add_argument("--limit", type=int, default=50, help="Max pools to show")
    sp.add_argument("--json", action="store_true", default=False, help="Emit JSON output")
    sp.set_defaults(func=cmd_market_pools_list)

    sp = sub.add_parser("market:routes:suggest", help="Suggest routes between two tokens using pool catalog")
    sp.add_argument("--base", required=True, help="Base token symbol or address")
    sp.add_argument("--quote", required=True, help="Quote token symbol or address")
    sp.add_argument("--limit", type=int, default=8, help="Max routes to display")
    sp.add_argument("--json", action="store_true", default=False, help="Emit JSON output")
    sp.set_defaults(func=cmd_market_routes_suggest)

    sp = sub.add_parser("market:best-price", help="Compute best normalized price for TRADE_PATH with decimal input amount")
    sp.add_argument("--amount", required=False, default=1.0, help="Decimal amount of input token")
    sp.set_defaults(func=cmd_market_best_price)

    sp = sub.add_parser("market:diem", help="Fetch DIEM signals via Venice API")
    sp.set_defaults(func=cmd_market_diem)

    sp = sub.add_parser("market:trade-paths:validate", help="Validate configured trade paths against on-chain pairs")
    sp.add_argument("--amount", required=False, default=1.0, type=float, help="Decimal input amount for quote preview")
    sp.add_argument("--skip-quotes", action="store_true", default=False, help="Skip aggregator quote preview")
    sp.set_defaults(func=cmd_market_validate_trade_paths)

    # DIEM direct actions (base units)
    sp = sub.add_parser("diem:mint", help="Mint DIEM (amount in base units); honors capacity gate if enabled")
    sp.add_argument("amount", type=int, help="Amount in base units (respecting DIEM_DECIMALS)")
    sp.add_argument("--dry-run", action="store_true", default=False, help="Do not send transaction; print intended action")
    sp.add_argument("--idem-key", dest="idem_key", default=None, help="Idempotency key to suppress duplicates")
    sp.add_argument("--corr-id", dest="corr_id", default=None, help="Correlation ID for telemetry")
    sp.set_defaults(func=cmd_diem_mint)

    sp = sub.add_parser("diem:burn", help="Burn DIEM (amount in base units)")
    sp.add_argument("amount", type=int, help="Amount in base units (respecting DIEM_DECIMALS)")
    sp.add_argument("--dry-run", action="store_true", default=False, help="Do not send transaction; print intended action")
    sp.add_argument("--idem-key", dest="idem_key", default=None, help="Idempotency key to suppress duplicates")
    sp.add_argument("--corr-id", dest="corr_id", default=None, help="Correlation ID for telemetry")
    sp.set_defaults(func=cmd_diem_burn)

    # Quotes preview to exercise liquidity-aware metrics without trading
    def cmd_quotes_preview(args: argparse.Namespace) -> None:
        from services.marketdata.provider import MarketDataProvider
        from services.diem.client import DIEMService
        from libs.dex.providers import build_aggregator_from_env
        from agents.arbi_diem.agent import ArbiDiem
        from services.risk.policy import RiskPolicy

        path = _require_trade_path()
        risk = RiskPolicy.from_env()
        market = MarketDataProvider()
        # Determine market price (record if fallback was used)
        used_price_fallback = False
        if args.price is not None:
            px = float(args.price)
        else:
            try:
                bp = market.best_price(path, amount_in_decimal=1.0)
                px = float(bp.get('price') or 0.0)
            except Exception:
                # Fallback: derive DIEM price using mid-price WETH->QUOTE when available
                px = float(market.diem_price_with_fallback() or 0.0)
                used_price_fallback = True
        if px <= 0:
            logger.error('Could not resolve market price. Set --price or ensure DEX providers are configured.')
            return
        # Determine desired units
        if args.units is not None:
            units = int(args.units)
        else:
            # Use max allowed units based on USD caps as a sensible preview default
            units = int(risk.max_allowed_units(px))
        if units <= 0:
            logger.error('Desired/allowed units is zero. Adjust env caps or pass --units.')
            return
        svc = DIEMService(build_aggregator_from_env(), market_data=market)
        arbi = ArbiDiem(diem=svc, risk=risk, market=market)
        # Reserve-cap sizing (best-effort)
        try:
            cap_bps = int((os.getenv("RISK_MAX_POOL_TAKE_BPS") or "100").strip() or 100)
        except Exception:
            cap_bps = 100
        cap_units = market.reserve_cap_units(path, take_bps=cap_bps)
        if isinstance(cap_units, int) and cap_units > 0:
            logger.info(f"reserve-cap: take_bps={cap_bps} units_cap={cap_units}")
            units = min(units, cap_units)
        adjusted, last_bps = arbi._adjust_for_liquidity(units, px)  # noqa: SLF001
        # If we could not preview via router/aggregator, estimate slippage using AMM fallback
        approx_used = False
        if last_bps is None and adjusted > 0:
            try:
                exec_px_approx = market.approx_exec_price(adjusted, path)
                if exec_px_approx and exec_px_approx > 0:
                    slip = risk.check_slippage(exec_px_approx, px)
                    last_bps = float(slip.get('slippage_bps', 0.0)) if isinstance(slip, dict) else None
                    approx_used = True
            except Exception:
                pass
        logger.info(
            "preview: price=%.6f desired_units=%d adjusted_units=%d slippage_bps=%s approx=%s price_fallback=%s note='Aerodrome exact-out disabled'",
            px,
            units,
            adjusted,
            last_bps,
            approx_used,
            used_price_fallback,
        )

    sp = sub.add_parser(
        "quotes:preview",
        help="Preview quotes to exercise liquidity-aware metrics (no trades)",
    )
    sp.add_argument("--units", type=int, required=False, help="Desired DIEM units (base units). Defaults to risk-based max.")
    sp.add_argument("--price", type=float, required=False, help="Override market price (USD per DIEM)")
    sp.set_defaults(func=cmd_quotes_preview)

    # Market scan: progressively smaller input until a quote is found
    def cmd_market_best_price_scan(args: argparse.Namespace) -> None:
        from services.marketdata.provider import MarketDataProvider
        path = _require_trade_path()
        md = MarketDataProvider()
        amt = float(args.start)
        floor = float(args.min)
        factor = float(args.factor)
        if factor <= 1.0:
            factor = 10.0
        while amt >= floor:
            try:
                res = md.best_price(path, amount_in_decimal=amt)
                logger.info(f"scan_best_price: amount={amt} provider={res['provider']} price={res['price']:.8f} path={res['path']}")
                return
            except Exception:
                amt = amt / factor
        logger.warning(f"No quotes available down to min amount {floor}. Consider using --price fallback or verifying pool reserves.")

    sp = sub.add_parser("market:best-price:scan", help="Scan smaller inputs until a quote is found (guards thin pools)")
    sp.add_argument("--start", default=1.0, type=float, help="Starting decimal input amount (default 1.0)")
    sp.add_argument("--min", default=1e-12, type=float, help="Minimum decimal input amount to try (default 1e-12)")
    sp.add_argument("--factor", default=10.0, type=float, help="Division factor per step (default 10)")
    sp.set_defaults(func=cmd_market_best_price_scan)

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

    # CI/health gate
    sp = sub.add_parser("ci:gate", help="Fail build if readiness/security checks fail (server /v1/env or local env)")
    sp.set_defaults(func=cmd_ci_gate)

    # --- Startup DEX probe (Etherscan v2) ---
    def cmd_startup_probe(args: argparse.Namespace) -> None:
        """Validate environment, Venice API config, DEX pairs, and system readiness.
        
        Checks:
        - Venice API base URL includes /api/v1
        - Portfolio inventory service readiness
        - DEX pairs along TRADE_PATH (if ETHERSCAN_API_KEY available)
        - Market data price health
        """
        issues = []
        warnings = []
        
        # Validate Venice API base URL
        venice_base = os.getenv("VENICE_API_BASE_URL", "")
        if not venice_base:
            msg = "VENICE_API_BASE_URL is not set"
            if args.warn_only:
                warnings.append(msg)
            else:
                issues.append(msg)
        else:
            cleaned = venice_base.rstrip("/")
            if "/api/v1" not in cleaned:
                msg = f"VENICE_API_BASE_URL must include '/api/v1'; got: {cleaned}"
                hint = f"Use: VENICE_API_BASE_URL={cleaned}/api/v1"
                if args.warn_only:
                    warnings.append(f"{msg} ({hint})")
                else:
                    issues.append(f"{msg} ({hint})")
            else:
                logger.info(f"Venice API base URL validated: {cleaned}")
        
        # Test portfolio inventory if live mode requested
        if args.check_live:
            try:
                from services.portfolio.inventory import PortfolioInventory
                from services.marketdata.provider import MarketDataProvider
                
                market = MarketDataProvider()
                inventory = PortfolioInventory(marketdata_provider=market)
                snapshot = inventory.snapshot(include_eth=False)
                
                if snapshot.errors:
                    warnings.extend(f"Portfolio inventory: {err}" for err in snapshot.errors)
                else:
                    logger.info(f"Portfolio inventory ready: {snapshot.inventory_usd:.2f} USD total")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Portfolio inventory check failed: {exc}")
        
        # Validate DEX pairs if Etherscan key available
        es_key = os.getenv("ETHERSCAN_API_KEY")
        if not es_key:
            logger.debug("ETHERSCAN_API_KEY not set; skipping DEX startup probe.")
        else:
            fee_tiers: Optional[List[Optional[int]]] = None
            route_tokens: List[str] = []

        tp = os.getenv("TRADE_PATH")
        if tp:
            try:
                from services.marketdata.provider import MarketDataProvider

                plan = MarketDataProvider._parse_route_spec(tp)
                route_tokens = [str(tok) for tok in plan.tokens]
                fee_tiers = [hop.fee for hop in plan.hops]
            except Exception as parse_exc:  # noqa: BLE001
                logger.debug("startup probe: falling back to comma-split TRADE_PATH (%s)", parse_exc)
                route_tokens = [p.strip() for p in tp.split(",") if p.strip()]
        else:
            try:
                from services.marketdata.dynamic_paths import discover_trade_paths

                auto_routes = discover_trade_paths(logger)
                if auto_routes:
                    first = auto_routes[0]
                    route_tokens = [str(tok) for tok in first.get("tokens") or []]
                    fees = first.get("fees")
                    if isinstance(fees, list):
                        fee_tiers = [int(f) if f is not None else None for f in fees]
                else:
                    logger.warning(
                        "TRADE_PATH is not set and dynamic discovery returned no routes; skipping DEX startup probe."
                    )
                    return
            except Exception as discovery_exc:  # noqa: BLE001
                logger.warning(
                    "TRADE_PATH is not set and dynamic discovery failed; skipping DEX startup probe. (%s)",
                    discovery_exc,
                )
                return

        if len(route_tokens) < 2:
            logger.warning("Parsed TRADE_PATH contains fewer than two tokens; skipping DEX startup probe.")
            return
        try:
            from services.marketdata.etherscan_verify import (
                warm_cache_for_path,
                format_report,  # type: ignore
                get_liquidity_cache_summary,
            )

            res = warm_cache_for_path(route_tokens, fee_tiers)
            report = format_report(res)
            print(report)
            # Print a compact cache summary with common labels when possible
            try:
                sym = {
                    "DIEM": (os.getenv("DIEM_TOKEN_ADDRESS") or "").lower(),
                    "VVV": (os.getenv("VVV_TOKEN_ADDRESS") or "").lower(),
                    "USDC": (os.getenv("QUOTE_TOKEN_ADDRESS") or "").lower(),
                }
                cache = get_liquidity_cache_summary()
                # Annotate token keyed entries with symbols for readability
                annotated = {}
                for k, v in (cache.get("by_tokens") or {}).items():
                    try:
                        a, b = k.split("->", 1)
                        la = next((n for n, addr in sym.items() if addr and addr == a.lower()), a)
                        lb = next((n for n, addr in sym.items() if addr and addr == b.lower()), b)
                        annotated[f"{la}->{lb}"] = v
                    except Exception:
                        annotated[k] = v
                print("Cache by_tokens:")
                for k, v in annotated.items():
                    print(f" - {k}: pair={v.get('pair')} has_reserves={v.get('has_reserves')}")
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            logger.warning(f"startup probe failed: {e}")

        try:
            from services.marketdata.provider import MarketDataProvider

            market = MarketDataProvider()
            health = market.price_health("DIEM", max_age=300.0)
            diff = health.get("diff")
            clamped = bool(health.get("clamped"))
            threshold = health.get("threshold")
            try:
                warn_threshold = float(os.getenv("MARKETDATA_SANITY_THRESHOLD") or 0.15)
            except Exception:
                warn_threshold = 0.15
            if isinstance(diff, (int, float)) and clamped:
                cmp_threshold = float(threshold) if isinstance(threshold, (int, float)) else warn_threshold
                if diff >= cmp_threshold:
                    provider = health.get("provider")
                    path = health.get("path")
                    path_detail = None
                    if isinstance(path, (list, tuple)):
                        path_tokens = [str(p).strip() for p in path if p]
                        if path_tokens:
                            path_detail = "->".join(path_tokens)
                    elif isinstance(path, str) and path.strip():
                        path_detail = path.strip()
                    logger.warning(
                        "startup probe: DIEM price drift exceeds clamp threshold",
                        extra={
                            "diff": float(diff),
                            "threshold": float(cmp_threshold),
                            "provider": provider,
                            "path": path_detail,
                        },
                    )
        except Exception:
            logger.debug("startup probe sanity check skipped", exc_info=True)
        
        # Print summary
        if issues:
            print("\n=== Issues Found ===")
            for issue in issues:
                print(f"❌ {issue}")
        if warnings:
            print("\n=== Warnings ===")
            for warning in warnings:
                print(f"⚠️  {warning}")
        
        if issues and not args.warn_only:
            raise SystemExit(1)
        elif issues or warnings:
            print("\n⚠️  Startup probe completed with warnings")
        else:
            print("\n✅ Startup probe passed")

    sp = sub.add_parser("startup:probe", help="Validate environment, Venice API config, and system readiness")
    sp.add_argument("--check-live", action="store_true", default=False, help="Also validate live operation requirements")
    sp.add_argument("--warn-only", action="store_true", default=False, help="Treat issues as warnings instead of errors")
    sp.set_defaults(func=cmd_startup_probe)

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
    sp.add_argument("--dry-run", action="store_true", default=False, help="Run without on-chain actions")
    sp.add_argument("--live", dest="dry_run", action="store_false", help="Alias to disable dry-run guard")
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
