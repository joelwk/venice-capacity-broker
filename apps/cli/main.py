from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

from apps._path import REPO_ROOT
from core.config import ConfigError, get_config
from libs.runtime.preflight import ensure_agentkit_installed, validate_live_wallet_env
from libs.telemetry.logger import get_logger
from libs.venice_sdk.client import VeniceClient
from scripts.wallet_cli import (
    cmd_address as wallet_cmd_address,
)
from scripts.wallet_cli import (
    cmd_send as wallet_cmd_send,
)
from scripts.wallet_cli import (
    cmd_sign as wallet_cmd_sign,
)
from scripts.wallet_cli import (
    cmd_sweep as wallet_cmd_sweep,
)
from scripts.wallet_cli import (
    cmd_transfer_cold as wallet_cmd_transfer_cold,
)
from services.venice_keys.manager import KeyManager
from services.wallet.provider import WalletError


def _load_dotenv() -> None:
    """Best-effort loading of repo-level dotenv + defaults for CLI usage."""

    try:
        from libs.env import bootstrap_env  # type: ignore

        bootstrap_env(repo_root=REPO_ROOT)
    except Exception:
        return


_load_dotenv()

logger = get_logger("cli")

# Validate RPC configuration early to fail fast on misconfiguration
try:
    from libs.runtime.rpc_validation import (
        log_rpc_configuration,
        validate_rpc_configuration,
    )

    # Log RPC configuration for observability
    log_rpc_configuration()

    # In production (non-dry-run), validate that we're not using public RPCs
    # Allow public RPCs in dry-run mode for testing
    is_dry_run_default = os.getenv("ENABLE_LIVE", "").lower() not in (
        "1",
        "true",
        "yes",
        "on",
    )
    if not is_dry_run_default:
        try:
            validate_rpc_configuration(
                fail_on_public=True,
                require_paid=False,  # Don't require paid if not explicitly set
                allow_dry_run=False,
            )
        except ValueError as exc:
            logger.error("RPC configuration validation failed: %s", exc)
            logger.error(
                "Set BASE_RPC_URLS=https://base-mainnet.g.alchemy.com/v2/YOUR_KEY "
                "in docker/.env.local or environment variables."
            )
            # Don't fail hard in CLI - let commands decide if RPC is needed
            # But log prominently so operators notice
except Exception as exc:
    # Don't fail startup if validation module has issues
    logger.debug("RPC validation skipped: %s", exc)

try:
    _APP_CONFIG = get_config()
except ConfigError as exc:
    # In lightweight CI steps we still want commands like ci:gate to run even if
    # optional env (e.g., TRADE_PATH) is absent. Defer hard failure to commands
    # that actually require the full config.
    logger.debug("configuration unavailable at import time: %s", exc)
    _APP_CONFIG = None


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


def cmd_wallet_health(args: argparse.Namespace) -> None:
    """Check wallet balances and trading readiness."""

    try:
        from web3 import Web3

        from libs.agentkit_ext.agentkit_wallet import get_address
        from libs.agentkit_ext.web3_utils import get_contract, get_web3
    except Exception as exc:  # pragma: no cover - import guard
        logger.error("wallet:health setup failed: %s", exc)
        raise SystemExit(2) from exc

    wallet = get_address()
    w3 = get_web3()

    def _erc20_balance(
        token_address: str | None, decimals_fallback: int
    ) -> tuple[float, int, int]:
        if not token_address:
            return 0.0, 0, decimals_fallback
        try:
            contract = get_contract(
                w3, Web3.to_checksum_address(token_address), "erc20.json"
            )
            raw = int(contract.functions.balanceOf(wallet).call())
            try:
                decimals = int(contract.functions.decimals().call())
            except Exception:
                decimals = decimals_fallback
            human = raw / float(10**decimals)
            return human, raw, decimals
        except Exception:
            return 0.0, 0, decimals_fallback

    eth_wei = w3.eth.get_balance(wallet)
    eth = eth_wei / 1e18

    usdc_token = os.getenv("USDC_TOKEN_ADDRESS")
    diem_token = os.getenv("DIEM_TOKEN_ADDRESS")
    vvv_token = os.getenv("VVV_TOKEN_ADDRESS")

    usdc_h, usdc_raw, usdc_dec = _erc20_balance(usdc_token, 6)
    diem_h, diem_raw, diem_dec = _erc20_balance(diem_token, 18)
    vvv_h, vvv_raw, vvv_dec = _erc20_balance(vvv_token, 18)

    min_trade_usd = float(os.getenv("ARBI_DIEM_MIN_TRADE_USD") or 1.0)
    warn_usdc_usd = float(os.getenv("PORTFOLIO_USDC_LOW_BALANCE_USD") or 5.0)
    gas_min_eth = float(os.getenv("GAS_REFUEL_MIN_ETH") or 0.0005)

    warnings: list[str] = []
    if usdc_h < min_trade_usd:
        warnings.append(
            f"USDC balance ${usdc_h:.2f} is below trade minimum ${min_trade_usd:.2f}; deposit USDC to enable buy/burn."
        )
    elif usdc_h < warn_usdc_usd:
        warnings.append(
            f"USDC balance ${usdc_h:.2f} is below warning threshold ${warn_usdc_usd:.2f}; top up soon."
        )
    if eth < gas_min_eth:
        warnings.append(
            f"ETH balance {eth:.6f} is below gas buffer {gas_min_eth:.6f}; refuel for transactions."
        )

    payload = {
        "wallet": wallet,
        "balances": {
            "ETH": {"eth": eth, "wei": eth_wei},
            "USDC": {"units": usdc_raw, "decimals": usdc_dec, "human": usdc_h},
            "VVV": {"units": vvv_raw, "decimals": vvv_dec, "human": vvv_h},
            "DIEM": {"units": diem_raw, "decimals": diem_dec, "human": diem_h},
        },
        "thresholds": {
            "min_trade_usd": min_trade_usd,
            "usdc_warn_usd": warn_usdc_usd,
            "gas_min_eth": gas_min_eth,
        },
        "warnings": warnings,
    }

    try:
        print(json.dumps(payload, indent=2))
    except Exception:
        print(payload)
    if warnings:
        raise SystemExit(3)


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
        import json

        from sqlmodel import Session, select

        from db.models import Counter
        from db.session import create_db_and_tables, get_engine
        from libs.kv import KVStore
    except Exception as e:
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
        logger.info(
            "No KV keys via prefix listing; attempting recent-window scan for known tenants..."
        )
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
                base = (
                    os.getenv("BROKER_BASE_URL")
                    or f"http://{os.getenv('BROKER_API_HOST', '127.0.0.1')}:{os.getenv('BROKER_API_PORT', '8000')}"
                ).rstrip("/")
                admin = os.getenv("BROKER_ADMIN_TOKEN")
                if admin:
                    try:
                        r = requests.get(
                            base + "/v1/tenants",
                            headers={"Authorization": f"Bearer {admin}"},
                            timeout=10,
                        )
                        if r.ok:
                            data = r.json()
                            if isinstance(data, list):
                                tenant_ids = [
                                    str(t.get("id"))
                                    for t in data
                                    if isinstance(t, dict) and t.get("id")
                                ]
                    except Exception:
                        pass

            # Window config
            window_default = int(
                (os.getenv("RATE_LIMIT_WINDOW_SECONDS") or "60").strip() or 60
            )
            scan_minutes = int(
                (os.getenv("KV_COMPACTION_SCAN_MINUTES") or "60").strip() or 60
            )
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
        except Exception as _e:
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
            elif count > int(existing.count):
                existing.count = int(count)
                rows_updated += 1
            try:
                s.commit()
            except Exception as e:
                s.rollback()
                logger.warning(
                    f"Failed to upsert counter for {tenant_id}@{bucket_s}: {e}"
                )
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
    except Exception as e:
        raise SystemExit(f"Invalid datetime '{val}': {e}")


def cmd_counters_show(args: argparse.Namespace) -> None:
    """Show aggregated counter buckets for a tenant from SQL.

    Examples:
      vvv-agents counters:show --tenant t-123 --limit 20 --desc
      vvv-agents counters:show --tenant t-123 --scope chat --since 2024-08-01T00:00:00Z
    """
    try:
        from sqlmodel import Session, select

        from db.models import Counter
        from db.session import create_db_and_tables, get_engine
    except Exception as e:
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
    except Exception as e:
        logger.warning(
            f"Challenge flow failed ({e}); attempting direct signature payload"
        )
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
    except Exception as e:
        logger.error(f"Failed to fetch usage: {e}")
        return
    try:
        limits = client.get_rate_limits()
        logger.info(f"limits: {limits}")
    except Exception as e:
        logger.warning(f"Failed to fetch rate limits/quota: {e}")


def cmd_venice_models(args: argparse.Namespace) -> None:
    client = VeniceClient()
    try:
        res = client.list_models()
        logger.info(f"models: {res}")
    except Exception as e:
        logger.error(f"Failed to list models: {e}")


def cmd_venice_signals(args: argparse.Namespace) -> None:
    client = VeniceClient()
    out: dict[str, Any] = {}
    # VVV metrics
    try:
        out["vvv"] = client.get_vvv_metrics()
    except Exception as e:
        logger.warning(f"Failed to fetch VVV metrics: {e}")
    # Raw VVV staking_yield payload (helps validate FV inputs derivation)
    try:
        out["vvv_staking_yield_raw"] = client.get_vvv_staking_yield()
    except Exception as e:
        logger.warning(f"Failed to fetch /vvv/staking_yield: {e}")
    # DIEM via rate-limits balances/quotas
    try:
        limits = client.get_rate_limits()
        obj = limits or {}
        data = obj.get("data") if isinstance(obj, dict) else None
        if isinstance(data, dict):
            balances = data.get("balances") or {}
        else:
            balances = obj.get("balances") or {}
        out["diem"] = {
            "balances": balances,
            "diem": balances.get("DIEM") or balances.get("diem"),
            "raw": limits,
        }
    except Exception as e:
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
    except Exception as e:
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
    from agents.stake_master.agent import StakeMaster
    from libs.agentkit_ext.actions import VVVActions
    from services.marketdata.provider import MarketDataProvider
    from services.staking.client import StakingService

    stake = StakingService(VVVActions())
    try:
        market = MarketDataProvider()
    except Exception as exc:
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

    from agents.ai_treasurer.agent import AITreasurer
    from agents.arbi_diem.agent import ArbiDiem
    from agents.arbi_diem.decider import InventorySnapshot
    from agents.capacity_broker.agent import CapacityBroker
    from agents.quorum import build_default_coordinator
    from agents.reflex.guardian import ReflexGuardian
    from agents.stake_master.agent import StakeMaster
    from graph.workflows.orchestrator import SingleLoopOrchestrator
    from libs.agentkit_ext.actions import VVVActions
    from libs.dex.providers import build_aggregator_from_env
    from libs.venice_sdk.client import VeniceClient
    from services.diem.client import DIEMService
    from services.marketdata.provider import MarketDataProvider
    from services.memory import MemoryStore, ReflectionEngine
    from services.portfolio.inventory import PortfolioInventory
    from services.staking.client import StakingService
    from services.venice_keys.manager import KeyManager

    arg_progressive = getattr(args, "progressive_live", None)
    env_progressive = _env_flag("STAKEMASTER_PROGRESSIVE_ENABLE", False)
    # Progressive is enabled if:
    # 1. CLI arg explicitly sets it (--progressive-live or --no-progressive-live), OR
    # 2. No CLI arg given, fall back to env var STAKEMASTER_PROGRESSIVE_ENABLE
    if arg_progressive is not None:
        progressive = bool(arg_progressive)
    else:
        progressive = env_progressive
    explicit_live = bool(getattr(args, "enable_live", False))
    forced_dry_run = bool(getattr(args, "dry_run", False))
    if explicit_live and forced_dry_run:
        raise SystemExit("--enable-live and --dry-run are mutually exclusive")
    dry_run = forced_dry_run or not explicit_live
    live_target = bool(explicit_live or progressive)

    if explicit_live or progressive:
        missing_env = validate_live_wallet_env(
            ["BASE_RPC_URL", "VVV_TOKEN_ADDRESS", "VVV_STAKING_ADDRESS"],
            logger,
        )
        if missing_env:
            raise SystemExit(2)

    market = MarketDataProvider()
    if live_target:
        try:
            stake_agent = StakeMaster(StakingService(VVVActions()), market=market)
        except (OSError, RuntimeError) as exc:
            logger.error(f"StakeMaster startup failed: {exc}")
            raise SystemExit(2) from exc
    else:
        # Dry-run smoke should not require web3/AgentKit wallet wiring.
        class _DryRunStakingService:
            def status(self) -> dict[str, Any]:
                try:
                    min_active = int(os.getenv("VVV_ACTIVE_MIN_STAKE_UNITS", "0") or 0)
                except Exception:
                    min_active = 0
                return {
                    "status": "unknown",
                    "staked": 0,
                    "rewards": 0,
                    "active_staker": False,
                    "min_active_stake": min_active,
                    "cooldown": {"seconds_remaining": None},
                    "snapshot_source": "dry_run",
                }

        stake_agent = StakeMaster(_DryRunStakingService(), market=market)  # type: ignore[arg-type]
    # Always initialize aggregator for quote simulation, even in dry-run mode.
    # Quotes are needed to validate trade paths and preview execution.
    aggregator = build_aggregator_from_env()
    diem_service = DIEMService(aggregator, market_data=market)
    arbi_agent = ArbiDiem(diem_service, market=market)
    startup_inventory = (
        InventorySnapshot.capture(include_eth=False) if live_target else None
    )
    if startup_inventory and startup_inventory.has_data:
        try:
            diem_units = startup_inventory.balance("DIEM")[0]
            usdc_units = startup_inventory.balance("USDC")[0]
            logger.info(
                "Captured wallet inventory snapshot (DIEM=%s, USDC=%s)",
                diem_units,
                usdc_units,
            )
        except Exception:
            logger.debug("Unable to log startup inventory snapshot")
    key_manager = KeyManager(VeniceClient())
    capacity_agent = CapacityBroker(key_manager)
    ai_treasurer = AITreasurer()
    quorum = build_default_coordinator() if _env_flag("QUORUM_ENABLE", True) else None

    memory_store = MemoryStore()
    reflection = ReflectionEngine()
    allow_inactive = bool(getattr(args, "allow_inactive_stake", False))
    if not allow_inactive:
        allow_inactive = str(
            os.getenv("REFLEX_ALLOW_INACTIVE_STAKE", "")
        ).strip().lower() in {"1", "true", "yes", "on"}
    reflex_guard = ReflexGuardian(require_active_stake=not allow_inactive)

    portfolio_inventory = (
        PortfolioInventory(marketdata_provider=market) if live_target else None
    )

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
    from agents.arbi_diem.agent import ArbiDiem
    from graph.workflows.revenue_streams import DiemMintSellWorkflow
    from libs.dex.providers import build_aggregator_from_env
    from services.diem.client import DIEMService
    from services.marketdata.provider import MarketDataProvider

    market = MarketDataProvider()
    diem = DIEMService(build_aggregator_from_env(), market_data=market)
    arbi = ArbiDiem(diem, market=market)
    flow = DiemMintSellWorkflow(market=market, arbi=arbi)
    decided = flow.run_once(dry_run=args.dry_run)
    logger.info(f"Quorum/flow decision (dry={args.dry_run}): {decided}")


def cmd_run_orchestrator(args: argparse.Namespace) -> None:
    """Run the orchestrator loop coordinating ArbiDiem decisions with persistence."""

    from agents.arbi_diem.agent import ArbiDiem
    from graph.workflows.orchestrator import Orchestrator
    from libs.dex.providers import build_aggregator_from_env
    from services.diem.client import DIEMService
    from services.marketdata.provider import MarketDataProvider

    market = MarketDataProvider()
    # Avoid initializing DEX/web3 in dry-run to prevent platform-specific crashes
    diem = DIEMService(
        build_aggregator_from_env() if not args.dry_run else None, market_data=market
    )
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
    state: dict[str, Any] = {}
    if args.messages:
        try:
            msgs = json.loads(args.messages)
        except Exception as e:
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
            self.calls: dict[str, Any] = {}

        def get_challenge(self, wallet_address: str) -> dict[str, Any]:
            self.calls["get_challenge"] = wallet_address
            return {
                "id": "offline-123",
                "challenge": f"Sign to create Venice key for {wallet_address}",
                "message": f"Create key: {wallet_address}",
                "nonce": "n-xyz",
            }

        def create_root_inference_key(
            self,
            wallet_address: str,
            signature: str,
            challenge: str | None = None,
            challenge_id: str | None = None,
        ) -> dict[str, Any]:
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
        logger.info(
            f"tenant id={t['id']} label={t['label']} status={t['status']} quota={t['quota']} expires_at={t.get('expires_at')}"
        )


def cmd_broker_tenants_create(args: argparse.Namespace) -> None:
    tenant_id = str(args.tenant).strip()
    if not tenant_id:
        raise SystemExit("--tenant is required")

    label = (getattr(args, "label", None) or "").strip()
    tier = (getattr(args, "tier", None) or "").strip()
    if not label:
        label = tenant_id

    payload: dict[str, Any] = {"tenant_id": tenant_id, "label": label}
    if getattr(args, "quota", None) is not None:
        payload["quota"] = int(args.quota)
    expires_at = (getattr(args, "expires_at", None) or "").strip()
    if expires_at:
        payload["expires_at"] = expires_at

    rotate = bool(getattr(args, "rotate", False))
    revoke_old = bool(getattr(args, "revoke_old", False))
    params: dict[str, str] = {}
    if rotate:
        params["rotate"] = "true"
        if revoke_old:
            params["revoke_old"] = "true"

    url = f"{_broker_base_url()}/v1/tenants"
    r = requests.post(
        url, headers=_admin_headers(), params=params, json=payload, timeout=20
    )
    if not r.ok:
        logger.error(f"tenant create failed: {r.status_code} {r.text}")
        raise SystemExit(2)

    created = r.json()
    action = "rotated" if rotate else "created"
    logger.info(
        f"tenant {action}: id={created.get('id')} label={created.get('label')} status={created.get('status')} quota={created.get('quota')} expires_at={created.get('expires_at')}"
    )

    # Optional: set per-tenant broker limits label/window/max (classification only).
    limits_payload: dict[str, Any] = {}
    if tier:
        limits_payload["label"] = tier
    if getattr(args, "window", None) is not None:
        limits_payload["windowSeconds"] = int(args.window)
    if getattr(args, "max", None) is not None:
        limits_payload["maxRequests"] = int(args.max)

    if limits_payload:
        limits_url = f"{_broker_base_url()}/v1/tenants/{tenant_id}/broker-limits"
        rr = requests.post(
            limits_url, headers=_admin_headers(), json=limits_payload, timeout=20
        )
        if not rr.ok:
            logger.error(f"tenant broker-limits set failed: {rr.status_code} {rr.text}")
        else:
            logger.info(f"tenant broker-limits updated: tenant={tenant_id} {rr.json()}")


def cmd_broker_venice_subkey(args: argparse.Namespace) -> None:
    label = str(args.label).strip()
    if not label:
        raise SystemExit("--label is required")

    expires_at = (getattr(args, "expires_at", None) or "").strip()
    if not expires_at:
        try:
            from apps.broker_api.config import compute_expires_at

            expires_at = compute_expires_at(None) or ""
        except Exception:
            expires_at = ""
    if not expires_at:
        raise SystemExit(
            "--expires-at is required (or set BROKER_DEFAULT_EXPIRY_DAYS>0)"
        )

    limit = int(args.diem)
    payload: dict[str, Any] = {
        "label": label,
        "consumptionLimit": {"diem": limit},
        "expiresAt": expires_at,
    }
    parent = (getattr(args, "parent_key", None) or "").strip()
    if parent:
        payload["parentKey"] = parent

    url = f"{_broker_base_url()}/v1/venice/subkey"
    r = requests.post(url, headers=_admin_headers(), json=payload, timeout=20)
    if not r.ok:
        logger.error(f"subkey create failed: {r.status_code} {r.text}")
        raise SystemExit(2)

    data = r.json()
    safe = dict(data) if isinstance(data, dict) else {"data": data}
    for k in ("apiKey", "api_key", "key", "token"):
        if safe.get(k):
            safe[k] = "[redacted]"
    logger.info(f"subkey created (redacted): {safe}")
    print(json.dumps(data, indent=2))


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
    payload: dict[str, Any] = {}
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


def cmd_broker_tenants_probe_subkeys(args: argparse.Namespace) -> None:
    """Probe all SQL-backed tenants by calling Venice with their subkeys."""
    try:
        from apps.broker_api.tenant_store_sql import SQLTenantStore
    except Exception as exc:
        logger.error(f"SQLTenantStore unavailable: {exc}")
        raise SystemExit(1) from exc

    from libs.venice_sdk.client import VeniceClient

    store = SQLTenantStore()
    client = VeniceClient()

    tenants = store.all()
    if not tenants:
        logger.info("no tenants found")
        return

    failures: list[tuple[str, str]] = []
    for tenant_id, t in tenants.items():
        subkey = getattr(t, "subkey", "") or ""
        if not subkey:
            logger.warning(f"[probe] {tenant_id}: missing subkey")
            failures.append((tenant_id, "missing_subkey"))
            continue

        logger.info(f"[probe] {tenant_id}: testing subkey against Venice...")
        sub_client = VeniceClient(
            base_url=client.config.base_url,
            api_key=subkey,
        )
        try:
            sub_client.list_models()
            logger.info(f"[probe] {tenant_id}: OK")
        except Exception as exc:
            msg = str(exc)
            logger.error(f"[probe] {tenant_id}: FAILED ({msg})")
            failures.append((tenant_id, msg))

    if failures:
        logger.error("\n[summary] tenant subkey probe failures:")
        for tenant_id, msg in failures:
            logger.error(f"  - {tenant_id}: {msg}")
        raise SystemExit(1)

    logger.info("\n[summary] all tenant subkeys passed Venice auth probe")


def cmd_broker_tenants_subkey(args: argparse.Namespace) -> None:
    """Print a tenant subkey from the local tenant store (SQL or JSON).

    This is intended for operator support and local validation.
    """
    tenant_id = str(args.tenant).strip()
    if not tenant_id:
        raise SystemExit("--tenant is required")

    backend = (os.getenv("BROKER_STORE_BACKEND") or "sql").strip().lower()
    store: Any
    if backend == "sql":
        try:
            from apps.broker_api.tenant_store_sql import SQLTenantStore

            store = SQLTenantStore()
        except Exception as exc:
            logger.error(f"SQL tenant store unavailable: {exc}")
            raise SystemExit(1) from exc
    else:
        try:
            from apps.broker_api.tenant_store import TenantStore

            store = TenantStore()
        except Exception as exc:
            logger.error(f"JSON tenant store unavailable: {exc}")
            raise SystemExit(1) from exc

    t = store.get(tenant_id)
    if not t:
        logger.error("tenant not found")
        raise SystemExit(2)

    out: dict[str, Any] = {
        "tenant": getattr(t, "id", tenant_id),
        "label": getattr(t, "label", None),
        "status": getattr(t, "status", None),
        "quota": getattr(t, "quota", None),
        "expires_at": getattr(t, "expires_at", None),
        "key_id": getattr(t, "key_id", None),
        "subkey": getattr(t, "subkey", None),
    }
    safe = dict(out)
    if safe.get("subkey"):
        safe["subkey"] = "[redacted]"
    logger.info(f"tenant subkey (redacted): {safe}")
    print(json.dumps(out, indent=2))


def cmd_broker_me_usage(args: argparse.Namespace) -> None:
    """Fetch /v1/me/usage using a tenant subkey as bearer auth."""
    base_url = (args.base_url or _broker_base_url()).rstrip("/")
    token = (args.auth_bearer or "").strip()
    if not token:
        raise SystemExit("--auth-bearer is required")
    url = f"{base_url}/v1/me/usage"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    if not r.ok:
        logger.error(f"/v1/me/usage failed: {r.status_code} {r.text}")
        raise SystemExit(2)
    print(json.dumps(r.json(), indent=2))


def cmd_broker_activity_counts(args: argparse.Namespace) -> None:
    """Print SQL-backed broker activity counters (tenants/keys)."""
    try:
        from sqlmodel import Session, select

        from db.models import Key as DbKey
        from db.models import Tenant as DbTenant
        from db.session import get_engine
    except Exception as exc:
        logger.error(f"SQL backend unavailable: {exc}")
        raise SystemExit(1) from exc

    engine = get_engine()
    total_keys = 0
    active_tenants = 0
    revoked_tenants = 0
    last_key_issue_ts: int | None = None

    with Session(engine) as s:  # type: ignore[call-arg]
        try:
            keys = s.exec(select(DbKey.created_at)).all()
            total_keys = len(keys)
            if keys:
                last = max(keys)
                try:
                    last_key_issue_ts = int(last.timestamp())
                except Exception:
                    last_key_issue_ts = None
        except Exception:
            total_keys = 0
            last_key_issue_ts = None

        try:
            active = s.exec(
                select(DbTenant.id).where(DbTenant.status == "active")
            ).all()
            active_tenants = len(active)
        except Exception:
            active_tenants = 0

        try:
            revoked = s.exec(
                select(DbTenant.id).where(DbTenant.status != "active")
            ).all()
            revoked_tenants = len(revoked)
        except Exception:
            revoked_tenants = 0

    print(
        json.dumps(
            {
                "active_tenants": int(active_tenants),
                "revoked_tenants": int(revoked_tenants),
                "total_keys": int(total_keys),
                "last_key_issue_ts": last_key_issue_ts,
            },
            indent=2,
        )
    )


# --- Idempotency keys admin ---
def cmd_idem_purge(args: argparse.Namespace) -> None:
    """Purge idempotency keys by prefix.

    Example:
      vvv-agents idem:purge --prefix idem:chat:t-123
    """
    try:
        from libs.kv import KVStore
    except Exception as e:
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
    except Exception as e:
        logger.error(
            f"limit probe unavailable: {e}. Ensure scripts/limit_probe.py and httpx are installed."
        )
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
        no_idempotency=bool(
            args.no_idempotency or _env_flag("PROBE_NO_IDEMPOTENCY", False)
        ),
        timeout=float(args.timeout or os.getenv("PROBE_TIMEOUT") or 10.0),
    )

    # Basic validation
    if not ns.auth_bearer and not (ns.admin_token and ns.tenant_id):
        logger.error(
            "Provide --auth-bearer (tenant subkey) or set BROKER_ADMIN_TOKEN and pass --tenant"
        )
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

    md = MarketDataProvider()
    try:
        return md.primary_trade_path()
    except Exception as exc:
        raise SystemExit(f"TRADE_PATH unavailable: {exc}") from exc


def cmd_quotes_compare(args: argparse.Namespace) -> None:
    from libs.dex.providers import build_aggregator_from_env

    path = _require_trade_path()
    agg = build_aggregator_from_env()
    quotes = agg.quote_all(args.amount, path)
    if not quotes:
        logger.info("No quotes available. Check DEX_PROVIDERS and router addresses.")
        return
    for q in quotes:
        logger.info(
            f"provider={q.provider} in={q.amount_in} out={q.amount_out} path={q.path}"
        )
    best = max(quotes, key=lambda q: q.amount_out)
    logger.info(f"best={best.provider} out={best.amount_out}")


def cmd_market_best_price(args: argparse.Namespace) -> None:
    from services.marketdata.provider import MarketDataProvider

    path = _require_trade_path()
    amount = float(args.amount)
    md = MarketDataProvider()
    res = md.best_price(path, amount_in_decimal=amount)
    if not res or "price" not in res or "provider" not in res:
        logger.warning(
            "best_price: no quotes available for path=%s amount=%s",
            list(path.tokens),
            amount,
        )
        return
    logger.info(
        f"best_price: provider={res['provider']} price={res['price']:.8f} path={res['path']}"
    )


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    if isinstance(value, dict) and "price" in value:
        return _coerce_float(value.get("price"))
    price_attr = getattr(value, "price", None)
    if price_attr is not None:
        return _coerce_float(price_attr)
    return None


def cmd_debug_diem_pricing(args: argparse.Namespace) -> None:
    """Compare all DIEM pricing methods side-by-side."""
    import json
    from datetime import datetime

    try:
        from libs.dex.routes import make_route
        from services.marketdata.pathing.env import load_env_config
        from services.marketdata.pathing.fallbacks import bridge_vvv_price
        from services.marketdata.provider import MarketDataProvider
    except Exception as exc:
        logger.error(f"Failed to import marketdata modules: {exc}")
        raise SystemExit(1) from exc

    md = MarketDataProvider()
    config = load_env_config()
    diem = config.diem_token
    quote = config.quote_token
    weth = os.getenv("WETH_ADDRESS")

    if not diem or not quote:
        logger.error(
            "DIEM_TOKEN_ADDRESS or QUOTE_TOKEN_ADDRESS missing; cannot run pricing debug."
        )
        raise SystemExit(1)

    def _call(name: str, fn) -> dict[str, Any]:
        try:
            result = fn()
            payload: dict[str, Any] = {"status": "success"}
            price = _coerce_float(result)
            if price is not None:
                payload["price"] = price
            if args.verbose:
                payload["raw"] = repr(result)
            return payload
        except Exception as exc:
            return {"status": "error", "error": str(exc), "price": None}

    bridges = {}
    bridge_price = None
    try:
        bridge_price = _coerce_float(bridge_vvv_price(config))
        bridges["bridge_vvv"] = bridge_price
    except Exception as exc:
        bridges["bridge_vvv_error"] = str(exc)

    methods = {
        "path_engine_direct": lambda: md._quote_via_path_engine(diem, quote, 1.0),
        "bridge_vvv": lambda: bridge_price,
        "get_price_final": lambda: md.get_price("DIEM"),
    }

    if weth and diem and quote:

        def _segments():
            plan = make_route([diem, weth, quote])
            return md._price_via_segments(plan)

        methods["segments_multi_hop"] = _segments

    if diem and quote:
        methods["exact_best_price"] = lambda: md.best_price(
            make_route([diem, quote]), amount_in_decimal=1.0
        )

    results: dict[str, Any] = {
        "timestamp": datetime.utcnow().isoformat(),
        "methods": {},
        "bridge": bridges,
    }

    for name, fn in methods.items():
        results["methods"][name] = _call(name, fn)

    bridge_px = bridge_price
    if bridge_px:
        for name, data in results["methods"].items():
            price = data.get("price")
            if price:
                try:
                    drift = abs(float(price) - bridge_px) / bridge_px
                    data["drift_from_bridge_pct"] = drift * 100
                except Exception:
                    continue

    print(json.dumps(results, indent=2, default=str))


def cmd_market_diem(args: argparse.Namespace) -> None:
    from services.marketdata.provider import MarketDataProvider

    md = MarketDataProvider()
    res = md.diem_balance()
    logger.info(f"diem_balance: {res}")


def cmd_market_dexscreener_pairs(args: argparse.Namespace) -> None:
    """Query DexScreener API for token pairs on Base chain."""
    from services.marketdata.dynamic_paths import _fetch_pairs

    token_address = args.token_address
    pairs = _fetch_pairs(token_address)

    if not pairs:
        logger.warning(f"No Base pairs found for token {token_address}")
        return

    # Format output similar to jq query
    output = []
    for pair in pairs:
        base_token = pair.get("baseToken", {})
        quote_token = pair.get("quoteToken", {})
        liquidity = pair.get("liquidity", {})

        pair_info = {
            "pairAddress": pair.get("pairAddress"),
            "dexId": pair.get("dexId"),
            "liquidity": liquidity.get("usd") if isinstance(liquidity, dict) else None,
            "baseToken": base_token.get("address")
            if isinstance(base_token, dict)
            else None,
            "quoteToken": quote_token.get("address")
            if isinstance(quote_token, dict)
            else None,
        }
        output.append(pair_info)

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        # Pretty print table format
        print(f"\nFound {len(output)} Base pairs for token {token_address}:\n")
        print(
            f"{'Pair Address':<45} {'DEX':<15} {'Liquidity (USD)':<20} {'Base Token':<45} {'Quote Token':<45}"
        )
        print("-" * 175)
        for pair_info in output:
            liq_str = (
                f"${pair_info['liquidity']:,.2f}" if pair_info["liquidity"] else "N/A"
            )
            print(
                f"{pair_info['pairAddress'] or 'N/A':<45} "
                f"{pair_info['dexId'] or 'N/A':<15} "
                f"{liq_str:<20} "
                f"{pair_info['baseToken'] or 'N/A':<45} "
                f"{pair_info['quoteToken'] or 'N/A':<45}"
            )


def cmd_market_validate_trade_paths(args: argparse.Namespace) -> None:
    from services.marketdata.dynamic_paths import discover_trade_paths
    from services.marketdata.provider import MarketDataProvider

    md = MarketDataProvider()
    try:
        plans = md._collect_trade_paths()  # type: ignore[attr-defined]
    except Exception:
        plans = []
    if not plans:
        try:
            raw_path = os.getenv("TRADE_PATH")
            if raw_path:
                plans = [md._parse_route_spec(raw_path)]  # type: ignore[attr-defined]
            else:
                raise ValueError("TRADE_PATH environment variable not set")
        except Exception as exc:
            logger.error(f"No trade paths configured: {exc}")
            raise SystemExit(1) from exc
    # Discover all dynamic paths once and cache
    try:
        discovered_specs = discover_trade_paths(logger_instance=logger)
    except Exception as exc:
        logger.warning(f"Dynamic path discovery failed: {exc}")
        discovered_specs = []
    seen: set[tuple[str, ...]] = set()
    reports = []
    for plan in plans:
        tokens = tuple(str(t).lower() for t in plan.tokens)
        if not tokens or tokens in seen:
            continue
        seen.add(tokens)
        # Find matching spec from discovered paths
        matching_spec = None
        for spec in discovered_specs:
            spec_tokens = tuple(str(addr).lower() for addr in spec.get("tokens") or [])
            if spec_tokens == tokens:
                matching_spec = spec
                break

        hop_details = []
        all_hops_ok = True

        if matching_spec:
            # Use dynamic discovery metadata if available
            metadata = matching_spec.get("metadata") or {}
            for hop in metadata.get("hops", []) or []:
                token_in = hop.get("token_in", "")
                token_out = hop.get("token_out", "")
                dex = hop.get("dex", "unknown")
                pool = hop.get("pool", "")
                liquidity_usd = float(hop.get("liquidityUsd", 0.0) or 0.0)
                min_liquidity_required_usd = float(
                    hop.get("minLiquidityRequiredUsd", 0.0) or 0.0
                )
                hop_ok = (
                    liquidity_usd >= min_liquidity_required_usd
                    and bool(pool)
                    and min_liquidity_required_usd > 0
                )
                hop_details.append(
                    {
                        "from": token_in,
                        "to": token_out,
                        "ok": hop_ok,
                        "venues": [
                            {
                                "venue": dex,
                                "pair": pool,
                                "reserves": None,
                                "has_liquidity": hop_ok,
                                "liquidity_usd": liquidity_usd,
                                "min_liquidity_required_usd": min_liquidity_required_usd,
                            }
                        ],
                    }
                )
                if not hop_ok:
                    all_hops_ok = False
        else:
            # Fallback: validate using RoutePlan hops when dynamic discovery doesn't have a match
            # This handles routes from env/canonical/bridge that may not be in dynamic discovery
            logger.warning(
                f"No matching dynamic path spec found for {'->'.join(tokens)}. "
                f"Validating route structure from RoutePlan (dynamic discovery may not include all configured routes). "
                f"Available dynamic specs: {[tuple(str(addr).lower() for addr in s.get('tokens') or []) for s in discovered_specs]}"
            )
            # Extract hop information from RoutePlan
            plan_hops = getattr(plan, "hops", [])
            for hop in plan_hops:
                token_in = str(hop.token_in) if hasattr(hop, "token_in") else ""
                token_out = str(hop.token_out) if hasattr(hop, "token_out") else ""
                fee = hop.fee if hasattr(hop, "fee") else None
                dex = "uniswap_v3" if fee is not None else "uniswap_v2"
                # Without dynamic discovery metadata, we can't validate liquidity
                # Mark as "ok" but note that liquidity validation is unavailable
                hop_details.append(
                    {
                        "from": token_in,
                        "to": token_out,
                        "ok": True,  # Assume ok if route is configured (liquidity check unavailable)
                        "venues": [
                            {
                                "venue": dex,
                                "pair": None,
                                "reserves": None,
                                "has_liquidity": None,
                                "liquidity_usd": None,
                                "min_liquidity_required_usd": None,
                            }
                        ],
                    }
                )
        price_preview = None
        price_error = None
        if not args.skip_quotes:
            try:
                preview = md.best_price(plan, amount_in_decimal=float(args.amount))
                price_preview = float(preview.get("price")) if preview else None
            except Exception as exc:
                price_error = str(exc)
        reports.append(
            {
                "tokens": [str(t) for t in plan.tokens],
                "hops": hop_details,
                "ok": all_hops_ok,
                "price": price_preview,
                "price_error": price_error,
            }
        )

    if not reports:
        logger.error("No unique trade paths found in configuration")
        raise SystemExit(1)

    # Check DIEM bridge leg / route hop consistency
    bridge_validation_failed = False
    try:
        from services.marketdata.pathing.env import load_env_config
        from services.marketdata.pathing.fallbacks import (
            get_bridge_trade_path_with_metadata,
        )

        env_config = load_env_config()
        bridge_metadata = get_bridge_trade_path_with_metadata(env_config)
        if bridge_metadata:
            bridge_legs = bridge_metadata.get("legs", [])
            bridge_path = bridge_metadata.get("path", [])
            if bridge_legs and len(bridge_legs) >= 2:
                diem_addr = os.getenv("DIEM_TOKEN_ADDRESS", "").lower()
                diem_routes = [
                    r
                    for r in reports
                    if r["tokens"] and r["tokens"][0].lower() == diem_addr
                ]
                matching_routes = [
                    r
                    for r in diem_routes
                    if len(r["tokens"]) == len(bridge_path)
                    and all(
                        t.lower() == b.lower() for t, b in zip(r["tokens"], bridge_path)
                    )
                ]
                incompatible_routes = [
                    r
                    for r in diem_routes
                    if len(r["tokens"]) == 2 and len(bridge_legs) >= 2
                ]
                if incompatible_routes and not matching_routes:
                    logger.error(
                        "DIEM bridge mismatch: bridge has %d legs but no matching "
                        "%d-token route found. Found %d incompatible 2-token route(s). "
                        "Ensure a route matching bridge endpoints exists.",
                        len(bridge_legs),
                        len(bridge_path),
                        len(incompatible_routes),
                    )
                    bridge_validation_failed = True
                elif incompatible_routes:
                    logger.warning(
                        "DIEM bridge has %d legs but found %d incompatible 2-token "
                        "route(s) alongside %d matching route(s). Consider removing "
                        "2-token DIEM routes to avoid composite routing failures.",
                        len(bridge_legs),
                        len(incompatible_routes),
                        len(matching_routes),
                    )
    except Exception as exc:
        logger.debug("Bridge consistency check skipped: %s", exc)

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
                dex = venue.get("venue", "unknown")
                pool = venue.get("pair")
                liquidity_usd = venue.get("liquidity_usd")
                min_liquidity = venue.get("min_liquidity_required_usd")
                if pool:
                    if liquidity_usd is not None and min_liquidity is not None:
                        print(
                            f"    {dex}: pool={pool} liquidity=${liquidity_usd:.2f} "
                            f"(min_required=${min_liquidity:.2f})"
                        )
                    else:
                        print(f"    {dex}: pool={pool}")
                else:
                    print(f"    {dex}: (no pool)")
        all_ok = all_ok and bool(info.get("ok"))
    if not all_ok or bridge_validation_failed:
        raise SystemExit(1)


def cmd_market_diem_route_probe(args: argparse.Namespace) -> None:
    """Show the first viable DIEM route using live pool quotes."""
    from libs.dex.providers import build_aggregator_from_env
    from libs.dex.routes import RoutePlan
    from services.diem.client import DIEMService
    from services.marketdata.provider import MarketDataProvider

    md = MarketDataProvider()
    agg = build_aggregator_from_env()
    svc = DIEMService(aggregator=agg, market_data=md)

    routes = svc.trade_routes(force_dynamic=args.dynamic)
    if not routes:
        print("No DIEM routes available.")
        raise SystemExit(1)

    def _fmt_route(route: RoutePlan) -> str:
        tokens = []
        for tok in route.tokens:
            ts = str(tok)
            tokens.append(ts if len(ts) <= 10 else ts[:6] + "…" + ts[-4:])
        return " -> ".join(tokens)

    try:
        diem_addr = md._address_for_symbol("DIEM")  # type: ignore[attr-defined]
    except Exception:
        diem_addr = os.getenv("DIEM_TOKEN_ADDRESS")
    dec = md.get_decimals(diem_addr or "") if diem_addr else 18
    amount_in = args.amount
    if amount_in is None:
        amount_in = max(1, int((10 ** max(dec, 0)) / 1000))

    for idx, route in enumerate(routes, 1):
        quotes = agg.quote_all(int(amount_in), route)
        if quotes:
            best = max(quotes, key=lambda q: q.amount_out)
            print(f"✅ Route {idx}: {_fmt_route(route)}")
            print(f"   Provider: {best.provider}")
            print(f"   Amount in (wei): {best.amount_in}")
            print(f"   Amount out (wei): {best.amount_out}")
            meta = getattr(route, "_metadata", None)
            if isinstance(meta, dict):
                print(f"   Metadata: {json.dumps(meta, default=str)}")
            return
        print(f"❌ Route {idx} produced no quotes: {_fmt_route(route)}")

    print("No viable DIEM routes returned quotes.")
    raise SystemExit(2)


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
    except Exception as exc:
        logger.error(f"pool watcher failed: {exc}")
        raise SystemExit(1) from exc


def cmd_market_pools_list(args: argparse.Namespace) -> None:
    from services.marketdata import pools

    try:
        rows = pools.list_pools(
            factory=args.factory, token=args.token, limit=args.limit
        )
    except Exception as exc:
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
                    "discovered_at": (
                        row.discovered_at.isoformat() if row.discovered_at else None
                    ),
                }
            )
        print(_json.dumps(data, separators=(",", ":")))
        return

    if not rows:
        logger.info("no pools discovered yet")
        return

    for row in rows:
        extras: list[str] = []
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


def cmd_market_pools_seed_diem(args: argparse.Namespace) -> None:
    """Seed DIEM/VVV and VVV/USDC pools from env variables into pool registry."""
    from services.marketdata.pools import seed_diem_pools_from_env

    try:
        seeded, updated = seed_diem_pools_from_env()
        logger.info(
            "pool.diem | Seeding complete: %d pools seeded, %d pools already existed",
            seeded,
            updated,
        )
        if seeded == 0 and updated == 0:
            logger.warning(
                "pool.diem | No pools seeded. Check DIEM_TOKEN_ADDRESS, VVV_TOKEN_ADDRESS, "
                "DIEM_VVV_PAIR_ADDRESS, and VVV_USDC_POOL_ADDRESS env variables."
            )
    except Exception as exc:
        logger.error(f"pool.diem | Seeding failed: {exc}")
        raise SystemExit(1) from exc


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
    except Exception as exc:
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
    res = svc.mint(
        amount, dry_run=bool(args.dry_run), idem_key=args.idem_key, corr_id=args.corr_id
    )
    logger.info(f"diem:mint amount={amount} dry_run={args.dry_run} res={res}")


def cmd_diem_burn(args: argparse.Namespace) -> None:
    """Burn DIEM via DIEMService with optional dry-run and idempotency."""
    from services.diem.client import DIEMService

    svc = DIEMService()
    amount = int(args.amount)
    res = svc.burn(
        amount, dry_run=bool(args.dry_run), idem_key=args.idem_key, corr_id=args.corr_id
    )
    logger.info(f"diem:burn amount={amount} dry_run={args.dry_run} res={res}")


def cmd_diem_custody(args: argparse.Namespace) -> None:
    """Show DIEM custody and sVVV lock status for the active wallet."""
    from services.diem.client import DIEMService

    svc = DIEMService()
    svvv = svc.svvv_lock_status()
    custody = svc.diem_custody_status()
    out = {"svvv": svvv, "diem": custody}
    try:
        locked = svvv.get("locked")
        total = svvv.get("total")
        if locked is not None and total not in (None, 0):
            out["svvv"]["locked_ratio"] = float(int(locked)) / float(int(total))
    except Exception:
        pass

    # Track A1: Enhanced validation logging for mixed DIEM custody
    try:
        wallet_diem = custody.get("wallet_diem_units") or 0
        staked_diem = custody.get("staked_diem_units") or 0
        total_diem = int(wallet_diem) + int(staked_diem)
        out["diem"]["total_diem_units"] = total_diem
        out["diem"]["burnable_from_wallet"] = wallet_diem
        out["diem"]["burnable_from_staking"] = staked_diem
        if custody.get("diem_staking_address"):
            out["diem"]["staking_configured"] = True
            out["diem"]["unlock_path_feasible"] = staked_diem > 0 or wallet_diem > 0
        else:
            out["diem"]["staking_configured"] = False
            out["diem"]["unlock_path_feasible"] = wallet_diem > 0
    except Exception as exc:
        logger.debug(f"Enhanced custody validation failed: {exc}")

    import json

    print(json.dumps(out, indent=2, sort_keys=True))


def cmd_diem_mint_rate(args: argparse.Namespace) -> None:
    """Display the current DIEM mint rate, optionally sourced from on-chain data."""
    from services.diem.client import DIEMService

    svc = DIEMService()
    ttl = int(args.ttl)
    if args.live:
        result = svc.fetch_mint_rate_onchain(ttl_s=ttl)
    else:
        result = svc.calc_mint_rate(ttl_s=ttl)
    logger.info("diem:mint-rate result=%s", result)
    try:
        import json  # local import to avoid global dependency

        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception:
        print(result)


def cmd_diem_route_health(args: argparse.Namespace) -> None:
    """Display route health status: mute counters, canonical route health, and active circuit breakers."""
    import time

    from libs.dex.providers import build_aggregator_from_env
    from libs.dex.routes import make_route
    from services.diem.client import DIEMService
    from services.marketdata.provider import MarketDataProvider

    market = MarketDataProvider()
    agg = build_aggregator_from_env()
    svc = DIEMService(aggregator=agg, market_data=market)

    # Get canonical routes
    diem = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip()
    weth = (
        os.getenv("WETH_ADDRESS") or "0x4200000000000000000000000000000000000006"
    ).strip()
    quote = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip()

    health_info = {
        "timestamp": time.time(),
        "muted_routes": {},
        "canonical_routes": {},
        "circuit_breakers": {},
    }

    # Check muted routes
    if hasattr(svc, "_route_revert_counts"):
        for route_key, (count, first_ts) in svc._route_revert_counts.items():
            age_seconds = time.time() - first_ts
            threshold = int(os.getenv("DIEM_ROUTE_REVERT_BAN_THRESHOLD", "3") or 3)
            ttl_seconds = float(
                os.getenv("DIEM_ROUTE_REVERT_BAN_TTL_SECONDS", "3600") or 3600
            )
            is_muted = count >= threshold and age_seconds < ttl_seconds
            health_info["muted_routes"][route_key] = {
                "revert_count": count,
                "threshold": threshold,
                "age_seconds": age_seconds,
                "ttl_seconds": ttl_seconds,
                "is_muted": is_muted,
                "route_type": "standard",
            }

    # Check canonical route mutes
    if hasattr(svc, "_canonical_route_revert_counts"):
        for route_key, (count, first_ts) in svc._canonical_route_revert_counts.items():
            age_seconds = time.time() - first_ts
            threshold = int(
                os.getenv("DIEM_CANONICAL_ROUTE_REVERT_BAN_THRESHOLD", "5") or 5
            )
            ttl_seconds = float(
                os.getenv("DIEM_CANONICAL_ROUTE_REVERT_BAN_TTL_SECONDS", "1800") or 1800
            )
            is_muted = count >= threshold and age_seconds < ttl_seconds
            health_info["canonical_routes"][route_key] = {
                "revert_count": count,
                "threshold": threshold,
                "age_seconds": age_seconds,
                "ttl_seconds": ttl_seconds,
                "is_muted": is_muted,
                "route_type": "canonical",
            }

    # Check circuit breakers
    if agg and hasattr(agg, "_circ_is_open"):
        for provider_name in ["uniswap_v3", "uniswap_v2", "aerodrome", "bridge_vvv"]:
            is_open = agg._circ_is_open(provider_name)
            health_info["circuit_breakers"][provider_name] = {
                "is_open": is_open,
            }

    # Check canonical route health
    if diem and weth and quote:
        try:
            canonical_route = make_route([diem, weth, quote])
            is_muted = (
                svc._is_route_muted(canonical_route)
                if hasattr(svc, "_is_route_muted")
                else False
            )
            circuit_open = (
                svc._is_route_circuit_open(canonical_route)
                if hasattr(svc, "_is_route_circuit_open")
                else False
            )
            health_info["canonical_route_health"] = {
                "route": [diem, weth, quote],
                "is_muted": is_muted,
                "circuit_open": circuit_open,
                "healthy": not (is_muted or circuit_open),
            }
        except Exception as exc:
            health_info["canonical_route_health"] = {
                "error": str(exc),
            }

    try:
        import json  # local import to avoid global dependency

        print(json.dumps(health_info, indent=2, sort_keys=True))
    except Exception:
        print(health_info)


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
        except Exception as e:
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
        if _flag("BROKER_REQUIRE_ADMIN_TOKEN", default=False) and not os.getenv(
            "BROKER_ADMIN_TOKEN"
        ):
            violations.append("BROKER_ADMIN_TOKEN missing while required")
        if _flag("VENICE_OFFLINE_SIGNALS", default=False):
            violations.append("VENICE_OFFLINE_SIGNALS=true")
        ven_base = os.getenv("VENICE_API_BASE_URL") or ""
        if "/api/v1" not in ven_base:
            violations.append("VENICE_API_BASE_URL must include /api/v1")
        if _flag("CORS_ENABLED", default=False):
            origins = [
                o.strip()
                for o in (os.getenv("CORS_ALLOW_ORIGINS") or "").split(",")
                if o.strip()
            ]
            if not origins or any(o == "*" for o in origins):
                violations.append(
                    "CORS_ALLOW_ORIGINS must be set without wildcard when CORS_ENABLED=true"
                )

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

    sp = sub.add_parser(
        "issue-key", help="Issue a Venice root key via signed challenge"
    )
    sp.add_argument(
        "--wallet", required=False, help="Wallet address (defaults to active wallet)"
    )
    sp.set_defaults(func=cmd_issue_key)

    sp = sub.add_parser("venice:usage", help="Show Venice usage and rate limits/quota")
    sp.set_defaults(func=cmd_venice_usage)

    sp = sub.add_parser("venice:models", help="List available Venice models")
    sp.set_defaults(func=cmd_venice_models)

    sp = sub.add_parser(
        "venice:signals", help="Fetch Venice VVV metrics and DIEM balance/quota"
    )
    sp.set_defaults(func=cmd_venice_signals)

    sp = sub.add_parser(
        "venice:wallet:address", help="Print the active hot wallet address"
    )
    sp.set_defaults(func=_wrap_wallet_cmd(wallet_cmd_address))

    sp = sub.add_parser(
        "venice:wallet:sign", help="Sign a plaintext message with the hot wallet"
    )
    sp.add_argument("message", help="Plaintext message to sign")
    sp.set_defaults(func=_wrap_wallet_cmd(wallet_cmd_sign))

    sp = sub.add_parser(
        "venice:wallet:send",
        help="Send a value transfer or contract call via the hot wallet",
    )
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
    sp.add_argument(
        "min_balance", help="Minimum wei balance to retain in the hot wallet"
    )
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

    sp = sub.add_parser(
        "wallet:health", help="Show wallet balances and trading readiness"
    )
    sp.set_defaults(func=cmd_wallet_health)

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
                    r = session.get(base.rstrip("/") + path, timeout=timeout)
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
            server_url = (
                servers[0].get("url")
                if servers and isinstance(servers[0], dict)
                else None
            )
            if server_url and isinstance(server_url, str):
                rec_base = (
                    server_url.rstrip("/")
                    if server_url.startswith(("http://", "https://"))
                    else base.rstrip("/") + "/" + server_url.lstrip("/")
                )
            else:
                rec_base = (
                    base.rstrip("/")
                    if spec_loc == "/openapi.json"
                    else base.rstrip("/") + "/api"
                )
            paths = spec.get("paths") or {}

            def _first(cands):
                return next((c for c in cands if c in paths), None)

            sub_path = (
                _first(["/api_keys", "/v1/keys/sub", "/v1/keys/subkey"]) or "/api_keys"
            )
            root_path = (
                _first(["/api_keys/generate_web3_key", "/v1/keys/generate_web3_key"])
                or "/api_keys/generate_web3_key"
            )
            vvv_path = (
                "/vvv"
                if "/vvv" in paths
                else ("/signals/vvv" if "/signals/vvv" in paths else "/vvv")
            )
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
        except Exception as e:
            logger.error(f"probe failed: {e}")

    sp = sub.add_parser(
        "venice:validate-addresses",
        help="Validate configured token/contract addresses against Venice signals",
    )
    sp.set_defaults(func=cmd_venice_validate_addresses)

    sp = sub.add_parser("venice:probe", help="Probe Venice OpenAPI and suggest exports")
    sp.add_argument(
        "--base-url",
        required=False,
        default=None,
        help="Venice host (e.g., https://api.venice.ai)",
    )
    sp.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds")
    sp.set_defaults(func=cmd_venice_probe)

    # Backward-compat: accept `venice` as a shorthand for the probe
    sp = sub.add_parser("venice", help="Alias for venice:probe")
    sp.add_argument(
        "--base-url",
        required=False,
        default=None,
        help="Venice host (e.g., https://api.venice.ai)",
    )
    sp.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds")
    sp.set_defaults(func=cmd_venice_probe)

    sp = sub.add_parser("run:stakemaster", help="Run a single StakeMaster heartbeat")
    sp.add_argument(
        "--enable-live",
        action="store_true",
        default=False,
        help="Allow live on-chain actions (claim)",
    )
    sp.set_defaults(func=cmd_run_stakemaster)

    sp = sub.add_parser(
        "run:quorum", help="Run quorum-driven workflow (dry-run by default)"
    )
    sp.add_argument("--dry-run", action="store_true", default=False)
    sp.set_defaults(func=cmd_run_quorum)

    sp = sub.add_parser(
        "run:graph", help="Run LangGraph pipeline once with optional broker chat"
    )
    sp.add_argument(
        "--messages",
        required=False,
        help="JSON array of chat messages [{role, content}]",
    )
    sp.add_argument(
        "--model", required=False, default=None, help="Model to use for broker routing"
    )
    sp.set_defaults(func=cmd_run_graph)

    sp = sub.add_parser(
        "run:loop",
        help="Run v1 single-loop orchestrator (StakeMaster → ArbiDiem → CapacityBroker); supports sleep/max-cycles/enable-live",
    )
    sp.add_argument(
        "--sleep", default=15, type=float, help="Seconds to sleep between cycles"
    )
    sp.add_argument(
        "--max-cycles",
        default=3,
        type=int,
        help="Maximum cycles to run (0 = run indefinitely)",
    )
    sp.add_argument(
        "--enable-live",
        action="store_true",
        default=False,
        help="Allow live actions (claim, mint, burn)",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run without on-chain actions (default)",
    )
    sp.add_argument(
        "--allow-inactive-stake",
        action="store_true",
        default=False,
        help="Skip reflex active-stake requirement (testing)",
    )
    sp.add_argument(
        "--progressive-live",
        dest="progressive_live",
        action="store_true",
        help="Enable progressive live escalation after healthy heartbeats",
    )
    sp.add_argument(
        "--no-progressive-live",
        dest="progressive_live",
        action="store_false",
        help="Disable progressive live escalation explicitly",
    )
    sp.set_defaults(progressive_live=None)
    sp.set_defaults(func=cmd_run_loop)

    sp = sub.add_parser(
        "test:challenge-offline",
        help="Offline test: sign a dummy challenge and echo payloads",
    )
    sp.set_defaults(func=cmd_test_challenge_offline)

    sp = sub.add_parser("addresses:print", help="Print current Base addresses from env")
    sp.set_defaults(func=cmd_print_addresses)

    sp = sub.add_parser(
        "quotes:compare",
        help="Compare quotes across configured DEX providers for the TRADE_PATH",
    )
    sp.add_argument(
        "--amount",
        required=True,
        type=int,
        help="Input amount (smallest units) to sell",
    )
    sp.set_defaults(func=cmd_quotes_compare)

    sp = sub.add_parser(
        "market:pools:watch",
        help="Watch configured factories for new pools and persist catalog",
    )
    sp.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Override POOL_WATCH_INTERVAL_SECONDS",
    )
    sp.add_argument(
        "--backfill", type=int, default=None, help="Override POOL_WATCH_BACKFILL_BLOCKS"
    )
    sp.add_argument(
        "--span", type=int, default=None, help="Override POOL_WATCH_BLOCK_SPAN"
    )
    sp.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="Run a single synchronization pass",
    )
    sp.set_defaults(func=cmd_market_pools_watch)

    sp = sub.add_parser(
        "market:pools:list", help="List pools discovered via factory watcher"
    )
    sp.add_argument(
        "--factory", required=False, help="Filter by factory type or address"
    )
    sp.add_argument("--token", required=False, help="Filter by token address")
    sp.add_argument("--limit", type=int, default=50, help="Max pools to show")
    sp.add_argument(
        "--json", action="store_true", default=False, help="Emit JSON output"
    )
    sp.set_defaults(func=cmd_market_pools_list)

    sp = sub.add_parser(
        "market:pools:seed-diem",
        help="Seed DIEM/VVV and VVV/USDC pools from env variables into pool registry",
    )
    sp.set_defaults(func=cmd_market_pools_seed_diem)

    sp = sub.add_parser(
        "market:routes:suggest",
        help="Suggest routes between two tokens using pool catalog",
    )
    sp.add_argument("--base", required=True, help="Base token symbol or address")
    sp.add_argument("--quote", required=True, help="Quote token symbol or address")
    sp.add_argument("--limit", type=int, default=8, help="Max routes to display")
    sp.add_argument(
        "--json", action="store_true", default=False, help="Emit JSON output"
    )
    sp.set_defaults(func=cmd_market_routes_suggest)

    sp = sub.add_parser(
        "market:best-price",
        help="Compute best normalized price for TRADE_PATH with decimal input amount",
    )
    sp.add_argument(
        "--amount", required=False, default=1.0, help="Decimal amount of input token"
    )
    sp.set_defaults(func=cmd_market_best_price)

    sp = sub.add_parser(
        "debug:diem-pricing", help="Compare DIEM pricing methods side-by-side"
    )
    sp.add_argument(
        "--verbose",
        action="store_true",
        help="Include raw method output in diagnostics",
    )
    sp.set_defaults(func=cmd_debug_diem_pricing)

    sp = sub.add_parser("market:diem", help="Fetch DIEM signals via Venice API")
    sp.set_defaults(func=cmd_market_diem)

    sp = sub.add_parser(
        "market:dexscreener:pairs",
        help="Query DexScreener API for token pairs on Base chain",
    )
    sp.add_argument(
        "token_address",
        help="Token address to query (e.g., 0xf4d97f2da56e8c3098f3a8d538db630a2606a024)",
    )
    sp.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output as JSON (default: table format)",
    )
    sp.set_defaults(func=cmd_market_dexscreener_pairs)

    sp = sub.add_parser(
        "market:trade-paths:validate",
        help="Validate configured trade paths against on-chain pairs",
    )
    sp.add_argument(
        "--amount",
        required=False,
        default=1.0,
        type=float,
        help="Decimal input amount for quote preview",
    )
    sp.add_argument(
        "--skip-quotes",
        action="store_true",
        default=False,
        help="Skip aggregator quote preview",
    )
    sp.set_defaults(func=cmd_market_validate_trade_paths)

    sp = sub.add_parser(
        "market:diem-route:probe",
        help="Show first viable DIEM route with a live quote",
    )
    sp.add_argument(
        "--amount",
        type=int,
        default=None,
        help="Probe amount in DIEM base units (defaults to ~0.001 DIEM)",
    )
    sp.add_argument(
        "--dynamic",
        action="store_true",
        default=False,
        help="Force dynamic route discovery even when cached paths exist",
    )
    sp.set_defaults(func=cmd_market_diem_route_probe)

    # DIEM direct actions (base units)
    sp = sub.add_parser(
        "diem:mint-rate", help="Show DIEM mint rate (on-chain or cached)"
    )
    sp.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Fetch directly from DIEM contract when available",
    )
    sp.add_argument(
        "--ttl", type=int, default=60, help="Cache TTL (seconds) for repeated calls"
    )
    sp.set_defaults(func=cmd_diem_mint_rate)

    sp = sub.add_parser(
        "diem:custody",
        help="Show DIEM custody (wallet vs staking helper) and sVVV lock status",
    )
    sp.set_defaults(func=cmd_diem_custody)

    sp = sub.add_parser(
        "diem:mint",
        help="Mint DIEM (amount in base units); honors capacity gate if enabled",
    )
    sp.add_argument(
        "amount", type=int, help="Amount in base units (respecting DIEM_DECIMALS)"
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Do not send transaction; print intended action",
    )
    sp.add_argument(
        "--idem-key",
        dest="idem_key",
        default=None,
        help="Idempotency key to suppress duplicates",
    )
    sp.add_argument(
        "--corr-id", dest="corr_id", default=None, help="Correlation ID for telemetry"
    )
    sp.set_defaults(func=cmd_diem_mint)

    sp = sub.add_parser("diem:burn", help="Burn DIEM (amount in base units)")
    sp.add_argument(
        "amount", type=int, help="Amount in base units (respecting DIEM_DECIMALS)"
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Do not send transaction; print intended action",
    )
    sp.add_argument(
        "--idem-key",
        dest="idem_key",
        default=None,
        help="Idempotency key to suppress duplicates",
    )
    sp.add_argument(
        "--corr-id", dest="corr_id", default=None, help="Correlation ID for telemetry"
    )
    sp.set_defaults(func=cmd_diem_burn)

    def cmd_diem_buy_preview(args: argparse.Namespace) -> None:
        """Preview DIEM buy quotes using USDC amount (exact-in mode)."""
        import os

        from libs.dex.providers import build_aggregator_from_env
        from services.diem.client import DIEMService
        from services.marketdata.provider import MarketDataProvider

        usdc_decimal = float(args.usdc)
        quote_decimals = int(os.getenv("QUOTE_TOKEN_DECIMALS", "6") or 6)
        usdc_base_units = int(usdc_decimal * (10**quote_decimals))

        market = MarketDataProvider()
        agg = build_aggregator_from_env()
        svc = DIEMService(aggregator=agg, market_data=market)

        # Get routes (bridge routes prioritized)
        routes = svc.trade_routes()
        if not routes:
            logger.error("No trade routes available")
            return

        # Filter to bridge routes for exact-in (matching trade() logic)
        bridge_routes = (
            svc._filter_bridge_buy_routes(routes)
            if hasattr(svc, "_filter_bridge_buy_routes")
            else routes
        )
        routes_to_use = bridge_routes if bridge_routes else routes

        logger.info(
            f"diem:buy-preview: usdc={usdc_decimal} ({usdc_base_units} base units), "
            f"routes={len(routes_to_use)}/{len(routes)}"
        )

        # Log route details for debugging
        for i, route in enumerate(routes_to_use[:3]):
            route_tokens = list(route.tokens) if hasattr(route, "tokens") else []
            logger.debug(f"Route {i + 1}: {route_tokens}")

        # Try exact-in quotes on bridge routes with quote shrinker
        quotes = []
        min_trade_usd = float(
            os.getenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "2.0") or 2.0
        )
        max_adjust_steps = int(
            os.getenv("ARBI_DIEM_LIQUIDITY_MAX_ADJUST_STEPS", "10") or 10
        )

        for route in routes_to_use[:3]:  # Try first 3 routes
            try:
                rev_route = (
                    route.reversed()
                )  # Reverse for buy direction (USDC->...->DIEM)
                route_tokens = (
                    list(rev_route.tokens) if hasattr(rev_route, "tokens") else []
                )

                # Try initial quote using best_quote() (exact-in by default)
                quote = None
                if hasattr(agg, "best_quote"):
                    try:
                        quote = agg.best_quote(usdc_base_units, rev_route)
                    except Exception as exc:
                        logger.debug(
                            f"best_quote failed for route {route_tokens}: {exc}"
                        )

                # If quote fails or is None, try shrinking the size
                if quote is None:
                    # Apply quote shrinker: halve size up to max_adjust_steps times
                    current_amount = usdc_base_units
                    min_amount = int(min_trade_usd * (10**quote_decimals))

                    for step in range(max_adjust_steps):
                        current_amount = int(current_amount / 2)
                        if current_amount < min_amount:
                            break

                        if hasattr(agg, "best_quote"):
                            try:
                                quote = agg.best_quote(current_amount, rev_route)
                                if quote:
                                    # Log shrinker success
                                    try:
                                        from libs.dex.diagnostics import (
                                            log_event as _dex_diag_log_event,
                                        )

                                        _dex_diag_log_event(
                                            {
                                                "event": "diem_buy_preview_shrinker",
                                                "route_tokens": route_tokens,
                                                "original_amount": usdc_base_units,
                                                "shrunk_amount": current_amount,
                                                "adjust_step": step + 1,
                                                "success": True,
                                            }
                                        )
                                    except Exception:
                                        pass
                                    break
                            except Exception as exc:
                                logger.debug(
                                    f"best_quote failed at step {step + 1} for route {route_tokens}: {exc}"
                                )
                                continue

                if quote:
                    quotes.append(quote)
                elif hasattr(agg, "quote_all"):
                    # Fallback to quote_all if best_quote fails
                    try:
                        route_quotes = agg.quote_all(usdc_base_units, rev_route)
                        if route_quotes:
                            quotes.extend(route_quotes)
                    except Exception as exc:
                        logger.debug(
                            f"quote_all failed for route {route_tokens}: {exc}"
                        )
            except Exception as exc:
                logger.warning(
                    f"Quote failed for route {list(route.tokens)}: {exc}",
                    exc_info=True,
                    extra={
                        "route_tokens": list(route.tokens)
                        if hasattr(route, "tokens")
                        else [],
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )

        if quotes:
            best = max(
                quotes,
                key=lambda q: q.amount_out
                if hasattr(q, "amount_out")
                else q.get("amount_out", 0),
            )
            diem_decimals = int(os.getenv("DIEM_DECIMALS", "18") or 18)
            diem_out = (
                best.amount_out
                if hasattr(best, "amount_out")
                else best.get("amount_out", 0)
            )
            diem_tokens = diem_out / (10**diem_decimals)
            provider = (
                best.provider
                if hasattr(best, "provider")
                else best.get("provider", "unknown")
            )
            route_tokens = (
                list(best.route.tokens)
                if hasattr(best, "route") and hasattr(best.route, "tokens")
                else best.get("route", {}).get("tokens", [])
            )

            logger.info(
                f"diem:buy-preview: best quote: {diem_tokens:.6f} DIEM via {provider}, "
                f"route={route_tokens}, effective_price=${usdc_decimal / diem_tokens:.2f}/DIEM"
            )
            try:
                print(
                    json.dumps(
                        {
                            "usdc_in": usdc_decimal,
                            "usdc_base_units": usdc_base_units,
                            "diem_out": diem_tokens,
                            "diem_base_units": diem_out,
                            "provider": provider,
                            "route": route_tokens,
                            "effective_price_usd_per_diem": usdc_decimal / diem_tokens
                            if diem_tokens > 0
                            else None,
                        },
                        indent=2,
                    )
                )
            except Exception:
                pass
        else:
            logger.warning(
                f"diem:buy-preview: no quotes available after trying {len(routes_to_use)} route(s)",
                extra={
                    "routes_tried": len(routes_to_use),
                    "usdc_amount": usdc_decimal,
                    "usdc_base_units": usdc_base_units,
                },
            )
            print(f"No quotes available for {usdc_decimal} USDC")

    sp = sub.add_parser(
        "diem:buy-preview",
        help="Preview DIEM buy quotes using USDC amount (exact-in mode, bridge routes only)",
    )
    sp.add_argument(
        "--usdc",
        type=float,
        required=True,
        help="USDC amount (decimal, e.g., 10.0 for 10 USDC)",
    )
    sp.set_defaults(func=cmd_diem_buy_preview)

    def cmd_diem_buy(args: argparse.Namespace) -> None:
        """Execute DIEM buy trade using USDC amount (exact-in mode)."""
        import os

        from libs.dex.providers import build_aggregator_from_env
        from services.diem.client import DIEMService
        from services.marketdata.provider import MarketDataProvider

        usdc_decimal = float(args.usdc)
        quote_decimals = int(os.getenv("QUOTE_TOKEN_DECIMALS", "6") or 6)
        usdc_base_units = int(usdc_decimal * (10**quote_decimals))

        market = MarketDataProvider()
        agg = build_aggregator_from_env()
        svc = DIEMService(aggregator=agg, market_data=market)

        # Get routes and estimate DIEM amount from USDC
        routes = svc.trade_routes()
        if not routes:
            logger.error("No trade routes available")
            return

        # Estimate DIEM amount from market price for initial quote
        try:
            diem_price = (
                market.get_price("DIEM") if hasattr(market, "get_price") else None
            )
            if diem_price and diem_price > 0:
                diem_decimals = int(os.getenv("DIEM_DECIMALS", "18") or 18)
                estimated_diem_tokens = usdc_decimal / diem_price
                estimated_diem_base = int(estimated_diem_tokens * (10**diem_decimals))
            else:
                # Fallback: assume $140/DIEM
                diem_decimals = int(os.getenv("DIEM_DECIMALS", "18") or 18)
                estimated_diem_tokens = usdc_decimal / 140.0
                estimated_diem_base = int(estimated_diem_tokens * (10**diem_decimals))
        except Exception:
            diem_decimals = int(os.getenv("DIEM_DECIMALS", "18") or 18)
            estimated_diem_tokens = usdc_decimal / 140.0
            estimated_diem_base = int(estimated_diem_tokens * (10**diem_decimals))

        logger.info(
            f"diem:buy: usdc={usdc_decimal} ({usdc_base_units} base units), "
            f"estimated_diem={estimated_diem_tokens:.6f} ({estimated_diem_base} base units), "
            f"dry_run={args.dry_run}"
        )

        if args.dry_run:
            # For dry-run, just preview the trade
            quote_result = svc.quote("buy", estimated_diem_base)
            quotes = quote_result.get("quotes", [])
            if quotes:
                best = max(
                    quotes,
                    key=lambda q: q.amount_out
                    if hasattr(q, "amount_out")
                    else q.get("amount_out", 0),
                )
                logger.info(
                    f"diem:buy (dry-run): would execute buy via {best.provider if hasattr(best, 'provider') else best.get('provider', 'unknown')}"
                )
                print(
                    json.dumps(
                        {
                            "status": "dry_run",
                            "action": "buy",
                            "usdc_in": usdc_decimal,
                            "estimated_diem": estimated_diem_tokens,
                        },
                        indent=2,
                    )
                )
            else:
                logger.warning("diem:buy (dry-run): no quotes available")
                print(
                    json.dumps(
                        {
                            "status": "dry_run",
                            "action": "buy",
                            "usdc_in": usdc_decimal,
                            "error": "no_quotes",
                        },
                        indent=2,
                    )
                )
        else:
            # For live execution, use trade() with estimated DIEM amount
            # Note: trade() will calculate actual amount_in_usdc internally for exact-in mode
            slippage_bps = int(args.slippage_bps) if args.slippage_bps else None
            res = svc.trade(
                side="buy",
                amount=estimated_diem_base,
                slippage_bps=slippage_bps,
                corr_id=args.corr_id,
            )
            logger.info(f"diem:buy: res={res}")
            try:
                print(json.dumps(res, indent=2, default=str))
            except Exception:
                print(res)

    sp = sub.add_parser(
        "diem:buy",
        help="Execute DIEM buy trade using USDC amount (exact-in mode, bridge routes only)",
    )
    sp.add_argument(
        "--usdc",
        type=float,
        required=True,
        help="USDC amount (decimal, e.g., 10.0 for 10 USDC)",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Do not send transaction; print intended action",
    )
    sp.add_argument(
        "--slippage-bps",
        type=int,
        default=None,
        help="Slippage tolerance in basis points (default: from SLIPPAGE_BPS env)",
    )
    sp.add_argument(
        "--corr-id",
        dest="corr_id",
        default=None,
        help="Correlation ID for telemetry",
    )
    sp.set_defaults(func=cmd_diem_buy)

    sp = sub.add_parser(
        "diem:route-health",
        help="Show route health status: mute counters, canonical route health, and active circuit breakers",
    )
    sp.set_defaults(func=cmd_diem_route_health)

    # Show DIEM execution routes (composite + env/dynamic) with pool metadata
    def cmd_diem_show_routes(args: argparse.Namespace) -> None:
        from libs.dex.providers import build_aggregator_from_env
        from services.diem.client import DIEMService
        from services.marketdata.provider import MarketDataProvider

        market = MarketDataProvider()
        agg = build_aggregator_from_env()
        svc = DIEMService(aggregator=agg, market_data=market)

        if hasattr(svc, "_trade_routes"):
            routes = svc._trade_routes()  # type: ignore[attr-defined]
        else:
            routes = svc.trade_routes()

        if not routes:
            print("No DIEM routes available")
            return

        print("DIEM execution routes:")
        for idx, route in enumerate(routes):
            tokens = [t for t in getattr(route, "tokens", [])]
            fees = [hop.fee for hop in route.hops]
            print(f"[{idx}] tokens={tokens} fees={fees}")
            meta = getattr(route, "_metadata", None)
            if isinstance(meta, dict) and meta:
                print(f"     metadata: {meta}")
            legs = getattr(route, "_bridge_legs", None)
            if legs:
                print("     bridge_legs:")
                for leg_idx, leg in enumerate(legs):
                    print(
                        f"       - leg{leg_idx}: provider={leg.get('provider')} "
                        f"pool={leg.get('pool_address')} fee={leg.get('fee')} "
                        f"{leg.get('token_in')} -> {leg.get('token_out')}"
                    )

    sp = sub.add_parser(
        "diem:routes:show",
        help="Print current DIEM execution routes with pool metadata",
    )
    sp.set_defaults(func=cmd_diem_show_routes)

    # Quotes preview to exercise liquidity-aware metrics without trading
    def cmd_quotes_preview(args: argparse.Namespace) -> None:
        from agents.arbi_diem.agent import ArbiDiem
        from libs.dex.providers import build_aggregator_from_env
        from services.diem.client import DIEMService
        from services.marketdata.provider import MarketDataProvider
        from services.risk.policy import RiskPolicy

        path = _require_trade_path()
        risk = RiskPolicy.from_env()
        market = MarketDataProvider()
        # Determine market price (record if fallback was used)
        used_price_fallback = False
        if args.price is not None:
            px = float(args.price)
        else:
            bp = {}
            try:
                bp = market.best_price(path, amount_in_decimal=1.0)
                px = float(bp.get("price") or 0.0)
            except Exception:
                pass

            if px <= 0:
                # Fallback: derive DIEM price using mid-price WETH->QUOTE when available
                # or use direct bridge/external reference
                px = float(market.diem_price_with_fallback() or 0.0)
                used_price_fallback = True
        if px <= 0:
            logger.error(
                "Could not resolve market price. Set --price or ensure DEX providers are configured."
            )
            return
        # Determine desired units
        if args.units is not None:
            units = int(args.units)
        else:
            # Use max allowed units based on USD caps as a sensible preview default
            units = int(risk.max_allowed_units(px))
        if units <= 0:
            logger.error(
                "Desired/allowed units is zero. Adjust env caps or pass --units."
            )
            return
        svc = DIEMService(build_aggregator_from_env(), market_data=market)
        arbi = ArbiDiem(diem=svc, risk=risk, market=market)
        # Reserve-cap sizing (best-effort)
        try:
            cap_bps = int((os.getenv("RISK_MAX_POOL_TAKE_BPS") or "100").strip() or 100)
        except Exception:
            cap_bps = 100

        # Handle RoutePlan vs Sequence[str] for legacy helpers
        path_tokens = getattr(path, "tokens", path)

        cap_units = market.reserve_cap_units(path_tokens, take_bps=cap_bps)
        if isinstance(cap_units, int) and cap_units > 0:
            logger.info(f"reserve-cap: take_bps={cap_bps} units_cap={cap_units}")
            units = min(units, cap_units)
        adjusted, last_bps = arbi._adjust_for_liquidity(units, px)
        # If we could not preview via router/aggregator, estimate slippage using AMM fallback
        approx_used = False
        if last_bps is None and adjusted > 0:
            try:
                exec_px_approx = market.approx_exec_price(adjusted, path_tokens)
                if exec_px_approx and exec_px_approx > 0:
                    slip = risk.check_slippage(exec_px_approx, px)
                    last_bps = (
                        float(slip.get("slippage_bps", 0.0))
                        if isinstance(slip, dict)
                        else None
                    )
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
    sp.add_argument(
        "--units",
        type=int,
        required=False,
        help="Desired DIEM units (base units). Defaults to risk-based max.",
    )
    sp.add_argument(
        "--price",
        type=float,
        required=False,
        help="Override market price (USD per DIEM)",
    )
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
                logger.info(
                    f"scan_best_price: amount={amt} provider={res['provider']} price={res['price']:.8f} path={res['path']}"
                )
                return
            except Exception:
                amt = amt / factor
        logger.warning(
            f"No quotes available down to min amount {floor}. Consider using --price fallback or verifying pool reserves."
        )

    sp = sub.add_parser(
        "market:best-price:scan",
        help="Scan smaller inputs until a quote is found (guards thin pools)",
    )
    sp.add_argument(
        "--start",
        default=1.0,
        type=float,
        help="Starting decimal input amount (default 1.0)",
    )
    sp.add_argument(
        "--min",
        default=1e-12,
        type=float,
        help="Minimum decimal input amount to try (default 1e-12)",
    )
    sp.add_argument(
        "--factor",
        default=10.0,
        type=float,
        help="Division factor per step (default 10)",
    )
    sp.set_defaults(func=cmd_market_best_price_scan)

    # Data compaction (KV -> SQL)
    sp = sub.add_parser(
        "data:compact-counters",
        help="Compact KV sliding-window counters into SQL (env-gated)",
    )
    sp.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Run even if env gating is disabled",
    )
    sp.set_defaults(func=cmd_compact_counters)

    # Data inspection (SQL counters)
    sp = sub.add_parser(
        "counters:show", help="Show aggregated counters for a tenant from SQL"
    )
    sp.add_argument("--tenant", required=True, help="Tenant id")
    sp.add_argument("--scope", required=False, help="Scope filter (e.g., chat)")
    sp.add_argument("--model", required=False, help="Model filter")
    sp.add_argument(
        "--bucket-seconds",
        type=int,
        required=False,
        help="Bucket size filter (seconds)",
    )
    sp.add_argument(
        "--since", required=False, help="ISO8601 or epoch seconds (inclusive)"
    )
    sp.add_argument(
        "--until", required=False, help="ISO8601 or epoch seconds (inclusive)"
    )
    sp.add_argument("--limit", type=int, default=50, help="Max rows to print")
    sp.add_argument(
        "--asc",
        dest="desc",
        action="store_false",
        help="Sort ascending by bucket_start (default desc)",
    )
    sp.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output JSON instead of log lines",
    )
    sp.set_defaults(func=cmd_counters_show)

    # Env status (server or local)
    def cmd_env_status(args: argparse.Namespace) -> None:
        base = os.getenv("BROKER_BASE_URL")
        out: dict[str, Any] = {}
        if base:
            try:
                r = requests.get(base.rstrip("/") + "/v1/env", timeout=5)
                if r.ok:
                    out["server"] = r.json()
                else:
                    logger.warning(f"/v1/env returned {r.status_code}")
            except Exception as e:
                logger.warning(f"Could not reach server /v1/env: {e}")
        # Local snapshot (env-only)
        out["local"] = {
            "kv": {
                "redis_configured": bool(
                    os.getenv("REDIS_URL") or os.getenv("KV_REDIS_URL")
                ),
                "replit_db_configured": bool(
                    os.getenv("KV_URL") or os.getenv("REPLIT_DB_URL")
                ),
                "namespace_set": bool(os.getenv("KV_NAMESPACE")),
                "prefix_set": bool(os.getenv("KV_PREFIX")),
            },
            "sql": {
                "env_configured": bool(
                    os.getenv("SQL_DATABASE_URL")
                    or os.getenv("DATABASE_URL")
                    or os.getenv("POSTGRES_HOST")
                ),
            },
            "limiter": {
                "enabled": str(os.getenv("RATE_LIMITS_ENABLED", "false")).lower()
                in {"1", "true", "yes"},
                "windowSeconds": int(
                    (os.getenv("RATE_LIMIT_WINDOW_SECONDS") or "60").strip() or 60
                ),
                "maxRequests": int(
                    (os.getenv("RATE_LIMIT_MAX_REQUESTS") or "60").strip() or 60
                ),
            },
            "idempotency": {
                "ttlSeconds": int(
                    (
                        os.getenv("IDEMPOTENCY_TTL_SECONDS")
                        or os.getenv("IDEM_TTL_SECONDS")
                        or "300"
                    ).strip()
                    or 300
                ),
            },
            "metrics": {
                "backend": (os.getenv("METRICS_BACKEND") or "auto").strip().lower(),
                "path": (os.getenv("METRICS_PATH") or "/metrics").strip() or "/metrics",
            },
            "tracing": {
                "enabled": str(os.getenv("LANGCHAIN_TRACING_V2", "false")).lower()
                in {"1", "true", "yes"},
            },
            "admin": {
                "token_present": bool(os.getenv("BROKER_ADMIN_TOKEN")),
                "required_at_startup": str(
                    os.getenv("BROKER_REQUIRE_ADMIN_TOKEN", "false")
                ).lower()
                in {"1", "true", "yes", "on"},
            },
        }
        try:
            import json as _json

            print(_json.dumps(out, indent=2))
        except Exception:
            print(out)

    sp = sub.add_parser(
        "env:status",
        help="Print environment status (server /v1/env if available plus local snapshot)",
    )
    sp.set_defaults(func=cmd_env_status)

    # CI/health gate
    sp = sub.add_parser(
        "ci:gate",
        help="Fail build if readiness/security checks fail (server /v1/env or local env)",
    )
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
        debug_routes_flag = str(
            os.getenv("DIEM_DEBUG_ROUTES") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}

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

        # Validate core token/env cohesion
        token_envs = {
            "DIEM_TOKEN_ADDRESS": os.getenv("DIEM_TOKEN_ADDRESS", "").strip(),
            "VVV_TOKEN_ADDRESS": os.getenv("VVV_TOKEN_ADDRESS", "").strip(),
            "QUOTE_TOKEN_ADDRESS": os.getenv("QUOTE_TOKEN_ADDRESS", "").strip(),
        }
        missing_tokens = [k for k, v in token_envs.items() if not v]
        if missing_tokens:
            msg = f"Missing token addresses: {', '.join(missing_tokens)}"
            if args.warn_only:
                warnings.append(msg)
            else:
                issues.append(msg)

        def _looks_hex_address(value: str) -> bool:
            raw = value.lower()
            return (
                raw.startswith("0x")
                and len(raw) == 42
                and all(c in "0123456789abcdef" for c in raw[2:])
            )

        malformed = [
            k for k, v in token_envs.items() if v and not _looks_hex_address(v)
        ]
        if malformed:
            msg = f"Malformed token addresses: {', '.join(malformed)}"
            if args.warn_only:
                warnings.append(msg)
            else:
                issues.append(msg)

        base_rpc = os.getenv("BASE_RPC_URL", "").strip()
        base_chain = os.getenv("BASE_CHAIN_ID", "").strip()
        if base_rpc and not base_chain:
            warnings.append("BASE_RPC_URL is set but BASE_CHAIN_ID is missing")
        if base_chain and not base_rpc:
            warnings.append("BASE_CHAIN_ID is set but BASE_RPC_URL is missing")

        # Validate execution configuration
        if args.check_live:
            try:
                from services.diem.execution import (
                    ExecutionConfigError,
                    validate_execution_env,
                )

                try:
                    exec_config = validate_execution_env()
                    logger.info(
                        "Execution configuration validated: %s required vars present",
                        len(exec_config.get("config", {})),
                    )
                    if exec_config.get("warnings"):
                        for warning in exec_config["warnings"]:
                            warnings.append(
                                f"Execution config: {warning} not set (recommended)"
                            )
                except ExecutionConfigError as exc:
                    msg = f"Execution configuration invalid: {exc}"
                    if args.warn_only:
                        warnings.append(msg)
                    else:
                        issues.append(msg)
            except Exception as exc:
                warnings.append(f"Execution config validation failed: {exc}")

        # Check pool registry status
        try:
            from db.models import DexPool
            from db.session import get_session
            from services.marketdata.pools import seed_diem_pools_from_env

            logger.info("Checking pool registry status...")
            seeded, updated = seed_diem_pools_from_env()
            if seeded > 0:
                logger.info(f"✅ Seeded {seeded} pools in registry")

            diem_vvv_pair = (os.getenv("DIEM_VVV_PAIR_ADDRESS") or "").strip().lower()
            vvv_usdc_pool = (os.getenv("VVV_USDC_POOL_ADDRESS") or "").strip().lower()

            # Normalize addresses for lookup
            def _normalize_hex(value: str) -> str:
                raw = value.lower().strip()
                if raw.startswith("0x"):
                    raw = raw[2:]
                padded = raw.rjust(40, "0")
                return "0x" + padded[-40:]

            if diem_vvv_pair:
                pool_addr = _normalize_hex(diem_vvv_pair)
                with next(get_session()) as session:
                    pool = session.get(DexPool, pool_addr)
                    if pool:
                        logger.info(f"✅ DIEM/VVV pair registered: {diem_vvv_pair}")
                    else:
                        warnings.append(
                            f"DIEM/VVV pair not found in registry: {diem_vvv_pair}"
                        )

            if vvv_usdc_pool:
                pool_addr = _normalize_hex(vvv_usdc_pool)
                with next(get_session()) as session:
                    pool = session.get(DexPool, pool_addr)
                    if pool:
                        logger.info(f"✅ VVV/USDC pool registered: {vvv_usdc_pool}")
                    else:
                        warnings.append(
                            f"VVV/USDC pool not found in registry: {vvv_usdc_pool}"
                        )
        except Exception as exc:
            warnings.append(f"Pool registry check failed: {exc}")

        # On-chain factory registration status for bridge pools
        try:
            from libs.agentkit_ext.web3_utils import get_web3
            from scripts.register_bridge_pools import (
                check_aerodrome_registration,
                check_uniswap_v3_registration,
                load_addresses,
            )

            w3 = get_web3()
            addresses = load_addresses()
            statuses = [
                (
                    "Aerodrome DIEM/VVV pair",
                    check_aerodrome_registration(w3, addresses),
                ),
                (
                    "Uniswap V3 VVV/USDC pool",
                    check_uniswap_v3_registration(w3, addresses),
                ),
            ]

            print("\nFactory registration:")
            for label, status in statuses:
                print(f"- {label}: {'registered' if status.registered else 'missing'}")
                print(f"  factory:  {status.factory}")
                print(f"  expected: {status.expected}")
                print(
                    "  factory get*: "
                    f"{status.reported if status.reported else '<none>'}"
                )
                for note in status.notes:
                    print(f"    note: {note}")
                if not status.registered:
                    guidance = (
                        f"{label} not registered with its factory. "
                        "Register via `uv run python scripts/register_bridge_pools.py --enable-live` "
                        "after confirming env addresses, or keep composite routing fallback enabled until registration completes."
                    )
                    if args.warn_only:
                        warnings.append(guidance)
                    else:
                        issues.append(guidance)
        except Exception as exc:
            warnings.append(f"Factory registration check failed: {exc}")

        # Test portfolio inventory if live mode requested
        if args.check_live:
            try:
                from services.marketdata.provider import MarketDataProvider
                from services.portfolio.inventory import PortfolioInventory

                market = MarketDataProvider()
                inventory = PortfolioInventory(marketdata_provider=market)
                snapshot = inventory.snapshot(include_eth=False)

                if snapshot.errors:
                    warnings.extend(
                        f"Portfolio inventory: {err}" for err in snapshot.errors
                    )
                else:
                    logger.info(
                        f"Portfolio inventory ready: {snapshot.inventory_usd:.2f} USD total"
                    )
            except Exception as exc:
                warnings.append(f"Portfolio inventory check failed: {exc}")

        # Validate DEX pairs if Etherscan key available
        es_key = os.getenv("ETHERSCAN_API_KEY")
        if not es_key:
            logger.debug("ETHERSCAN_API_KEY not set; skipping DEX startup probe.")
        else:
            fee_tiers: list[int | None] | None = None
            route_tokens: list[str] = []

        fee_tiers: list[int | None] = []
        tp = os.getenv("TRADE_PATH")
        if tp:
            try:
                from services.marketdata.provider import MarketDataProvider

                md = MarketDataProvider()
                plan = md._parse_route_spec(tp)
                route_tokens = [str(tok) for tok in plan.tokens]
                fee_tiers = [hop.fee for hop in plan.hops]
            except Exception as parse_exc:
                logger.debug(
                    "startup probe: falling back to comma-split TRADE_PATH (%s)",
                    parse_exc,
                )
                route_tokens = [p.strip() for p in tp.split(",") if p.strip()]
                # Try to extract fee tiers from comma-split path (e.g., "token1,token2@3000")
                fee_tiers = []
                for segment in route_tokens:
                    if "@" in segment:
                        _, fee_part = segment.rsplit("@", 1)
                        try:
                            fee_tiers.append(int(fee_part.strip()))
                        except Exception:
                            fee_tiers.append(None)
                    else:
                        fee_tiers.append(None)
                # Map token fees to hop fees: fee on token[i+1] applies to hop[i]
                if len(fee_tiers) > 1:
                    hop_fees = []
                    for i in range(len(route_tokens) - 1):
                        hop_fees.append(
                            fee_tiers[i + 1] if i + 1 < len(fee_tiers) else None
                        )
                    fee_tiers = hop_fees
                else:
                    fee_tiers = []
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
            except Exception as discovery_exc:
                logger.warning(
                    "TRADE_PATH is not set and dynamic discovery failed; skipping DEX startup probe. (%s)",
                    discovery_exc,
                )
                return

        if len(route_tokens) < 2:
            logger.warning(
                "Parsed TRADE_PATH contains fewer than two tokens; skipping DEX startup probe."
            )
            return
        try:
            from services.marketdata.etherscan_verify import (
                format_report,  # type: ignore
                get_liquidity_cache_summary,
                warm_cache_for_path,
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
                        la = next(
                            (
                                n
                                for n, addr in sym.items()
                                if addr and addr == a.lower()
                            ),
                            a,
                        )
                        lb = next(
                            (
                                n
                                for n, addr in sym.items()
                                if addr and addr == b.lower()
                            ),
                            b,
                        )
                        annotated[f"{la}->{lb}"] = v
                    except Exception:
                        annotated[k] = v
                print("Cache by_tokens:")
                for k, v in annotated.items():
                    print(
                        f" - {k}: pair={v.get('pair')} has_reserves={v.get('has_reserves')}"
                    )
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"startup probe failed: {e}")

        try:
            from services.marketdata.provider import MarketDataProvider

            market = MarketDataProvider()
            health = market.price_health("DIEM", max_age=300.0)
            diff = health.get("diff")
            clamped = bool(health.get("clamped"))
            threshold = health.get("threshold")
            source = health.get("source", "unknown")
            fallback_reason = health.get("fallback_reason")
            try:
                warn_threshold = float(os.getenv("MARKETDATA_SANITY_THRESHOLD") or 0.15)
            except Exception:
                warn_threshold = 0.15

            # Check if DIEM pricing is using authoritative sources
            source_ok = source.startswith("aggregator") or source == "bridge_vvv"
            if not source_ok and fallback_reason:
                logger.info(
                    "startup probe: DIEM price using fallback source",
                    extra={
                        "source": source,
                        "fallback_reason": fallback_reason,
                        "trusted": health.get("trusted_external", False),
                    },
                )
            elif source_ok:
                logger.info(
                    "startup probe: DIEM price from authoritative source",
                    extra={"source": source},
                )

            if isinstance(diff, (int, float)) and clamped:
                cmp_threshold = (
                    float(threshold)
                    if isinstance(threshold, (int, float))
                    else warn_threshold
                )
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
                            "source": source,
                            "fallback_reason": fallback_reason,
                        },
                    )

            # Explicit DIEM route health check
            try:
                diem_price = market.get_price("DIEM")
                if diem_price and diem_price > 0:
                    logger.info(
                        "startup probe: DIEM route health check passed",
                        extra={
                            "price": float(diem_price),
                            "source": source,
                            "health_valid": health.get("valid", True),
                        },
                    )
                else:
                    warnings.append("DIEM price unavailable or invalid")
            except Exception as route_exc:
                warnings.append(f"DIEM route health check failed: {route_exc}")
        except Exception:
            logger.debug("startup probe sanity check skipped", exc_info=True)

        # DIEM route quote probe at $2 notional
        try:
            from services.marketdata.provider import MarketDataProvider

            diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip()
            quote_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip()

            if diem_addr and quote_addr:
                md = MarketDataProvider()
                aggregator = md._get_aggregator()

                normalized_quote = quote_addr.lower()
                candidates: list[list[str]] = []

                if route_tokens:
                    try:
                        rt_clean = [
                            str(tok).strip() for tok in route_tokens if str(tok).strip()
                        ]
                        if rt_clean:
                            if rt_clean[0].lower() == normalized_quote:
                                candidates.append(rt_clean)
                            if rt_clean[-1].lower() == normalized_quote:
                                candidates.append(list(reversed(rt_clean)))
                    except Exception:
                        pass

                # Fallback to a direct quote->DIEM probe when no candidate route starts with the quote token
                if not candidates:
                    candidates.append([quote_addr, diem_addr])

                seen_routes = set()
                deduped: list[list[str]] = []
                for cand in candidates:
                    key = tuple(tok.lower() for tok in cand)
                    if len(cand) < 2 or key in seen_routes:
                        continue
                    seen_routes.add(key)
                    deduped.append(cand)
                candidates = deduped

                quote_ok = False
                diag_reports: list[dict[str, Any]] = []

                for cand in candidates:
                    token_in = cand[0]
                    try:
                        dec = md.get_decimals(token_in)
                        amount_in = int(Decimal("2") * (Decimal(10) ** dec))
                    except Exception as dec_exc:
                        warnings.append(
                            f"DIEM quote probe skipped for route {cand}: decimals unavailable ({dec_exc})"
                        )
                        continue

                    try:
                        aggregator.best_quote(amount_in, cand)
                        ctx = getattr(aggregator, "_last_quote_context", {}) or {}
                        exec_count = ctx.get("executable_quotes")
                        diag_reports.append(
                            {
                                "route": cand,
                                "amount_in": amount_in,
                                "quotes_attempted": ctx.get("quotes_attempted"),
                                "executable_quote_count": exec_count,
                                "provider_errors": ctx.get("provider_errors"),
                                "status_counts": ctx.get("status_counts"),
                            }
                        )

                        if exec_count and int(exec_count) > 0:
                            quote_ok = True
                            logger.info(
                                "startup probe: DIEM $2 quote succeeded",
                                extra={
                                    "route": cand,
                                    "executable_quote_count": exec_count,
                                    "quotes_attempted": ctx.get("quotes_attempted"),
                                },
                            )
                            break
                    except Exception as quote_exc:
                        diag_reports.append(
                            {
                                "route": cand,
                                "amount_in": amount_in,
                                "error": str(quote_exc),
                            }
                        )
                        continue

                if debug_routes_flag:
                    for report in diag_reports:
                        try:
                            logger.info(
                                "startup probe: DIEM quote diagnostics",
                                extra=report,
                            )
                        except Exception:
                            pass

                if not quote_ok:
                    msg = "No executable DIEM quotes at $2 notional"
                    if args.warn_only:
                        warnings.append(msg)
                    else:
                        issues.append(msg)
            else:
                logger.debug("Skipping DIEM $2 quote probe: missing token addresses")
        except Exception as quote_probe_exc:
            warnings.append(f"DIEM quote probe failed: {quote_probe_exc}")

        # Database readiness checks
        try:
            from sqlalchemy import text

            from db.session import get_engine

            db_url = os.getenv("SQL_DATABASE_URL") or os.getenv("DATABASE_URL")
            if db_url:
                try:
                    eng = get_engine()
                    with eng.connect() as conn:
                        conn.execute(text("SELECT 1"))
                    logger.info("startup probe: Database connection OK")

                    # Check migrations
                    try:
                        import alembic.command
                        import alembic.config
                        from alembic.runtime.migration import MigrationContext
                        from alembic.script import ScriptDirectory

                        alembic_cfg = alembic.config.Config()
                        alembic_cfg.set_main_option("script_location", "db/migrations")
                        script = ScriptDirectory.from_config(alembic_cfg)
                        head_rev = script.get_current_head()

                        with eng.connect() as conn:
                            context = MigrationContext.configure(conn)
                            db_rev = context.get_current_revision()

                        if db_rev == head_rev:
                            logger.info(
                                "startup probe: Migrations at head",
                                extra={"revision": db_rev},
                            )
                        else:
                            warnings.append(
                                f"Migrations out of date: db={db_rev} head={head_rev}"
                            )
                    except Exception as mig_exc:
                        warnings.append(f"Migration check failed: {mig_exc}")
                except Exception as db_exc:
                    issues.append(f"Database connection failed: {db_exc}")
            else:
                logger.debug(
                    "startup probe: No database URL configured (skipping DB checks)"
                )
        except ImportError:
            logger.debug("startup probe: DB modules unavailable (skipping DB checks)")
        except Exception as db_check_exc:
            warnings.append(f"Database readiness check failed: {db_check_exc}")

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
        if issues or warnings:
            print("\n⚠️  Startup probe completed with warnings")
        else:
            print("\n✅ Startup probe passed")

    sp = sub.add_parser(
        "startup:probe",
        help="Validate environment, Venice API config, and system readiness",
    )
    sp.add_argument(
        "--check-live",
        action="store_true",
        default=False,
        help="Also validate live operation requirements",
    )
    sp.add_argument(
        "--warn-only",
        action="store_true",
        default=False,
        help="Treat issues as warnings instead of errors",
    )
    sp.set_defaults(func=cmd_startup_probe)

    def cmd_gas_status(args: argparse.Namespace) -> None:
        """Check gas balance and refuel status."""
        from services.wallet.gas_refuel import GasRefuelService

        service = GasRefuelService()
        status = service.get_status()

        eth_balance = status.get("eth_balance_wei") or 0
        eth_balance_eth = status.get("eth_balance_eth") or 0.0
        min_eth = status.get("min_eth_wei", 0)
        target_eth = status.get("target_eth_wei", 0)
        needs_refuel = status.get("needs_refuel", False)

        print("=== Gas Refuel Status ===")
        print(f"Enabled:        {status.get('enabled', False)}")
        print(f"Dry run:        {status.get('dry_run', False)}")
        print(f"ETH balance:    {eth_balance_eth:.6f} ETH ({eth_balance} wei)")
        print(f"Min threshold:  {min_eth / 1e18:.6f} ETH")
        print(f"Target:         {target_eth / 1e18:.6f} ETH")
        print(f"Needs refuel:   {'YES ⚠️' if needs_refuel else 'No ✅'}")
        print(f"Asset priority: {', '.join(status.get('asset_priority', []))}")
        print(f"WETH address:   {status.get('weth_address', 'N/A')}")

        print("\n=== Available Assets ===")
        asset_balances = status.get("asset_balances", {})
        for symbol, balance in asset_balances.items():
            if symbol in {"USDC", "USDBC"}:
                formatted = f"${balance / 1e6:.2f}"
            else:
                formatted = f"{balance / 1e18:.6f}"
            print(f"  {symbol}: {formatted}")

        if needs_refuel:
            if args.refuel:
                print("\nAttempting gas refuel...")
                result = service.check_and_refuel()
                print(f"Result: {result.action} - {result.reason}")
                if result.error:
                    print(f"Error: {result.error}")
                if result.tx_hash:
                    print(f"Swap TX: {result.tx_hash}")
                if result.unwrap_tx_hash:
                    print(f"Unwrap TX: {result.unwrap_tx_hash}")
                if result.eth_balance_after_wei:
                    print(f"New balance: {result.eth_balance_after_wei / 1e18:.6f} ETH")
            else:
                print("\n⚠️  Gas refuel needed. Run with --refuel to attempt refuel.")

    sp = sub.add_parser("gas:status", help="Check gas balance and refuel status")
    sp.add_argument(
        "--refuel",
        action="store_true",
        default=False,
        help="Attempt to refuel if gas is low",
    )
    sp.set_defaults(func=cmd_gas_status)

    def cmd_market_diem_bridge_check(args: argparse.Namespace) -> None:
        """Check DIEM pricing via bridge and path engine, recording price health."""
        from services.marketdata.provider import MarketDataProvider

        md = MarketDataProvider()

        print("Checking DIEM pricing...")
        price = md.diem_price_with_fallback()
        health = md.price_health("DIEM")

        if price is not None:
            print(f"✅ DIEM price: ${price:.4f}")
        else:
            print("❌ DIEM price: unavailable")

        print("\nPrice health:")
        print(f"  Source: {health.get('source', 'unknown')}")
        print(f"  Valid: {health.get('valid', False)}")
        print(f"  Provider: {health.get('provider', 'unknown')}")

        if health.get("clamped"):
            print(
                f"  ⚠️  Price was clamped (reason: {health.get('clamp_reason', 'unknown')})"
            )

        if health.get("fallback_reason"):
            print(f"  Fallback reason: {health.get('fallback_reason')}")

        if price is None or not health.get("valid"):
            print("\n❌ DIEM pricing check failed")
            sys.exit(1)
        else:
            print("\n✅ DIEM pricing check passed")

    def cmd_market_bridge_factory_check(args: argparse.Namespace) -> None:
        """Check that bridge pools are discoverable via Aerodrome and Uniswap factories before enabling router execution."""
        from libs.agentkit_ext.web3_utils import get_web3
        from scripts.register_bridge_pools import (
            BridgeAddresses,
            check_aerodrome_registration,
            check_uniswap_v3_registration,
            load_addresses,
        )

        w3 = get_web3()
        addresses: BridgeAddresses = load_addresses()
        statuses = [
            ("Aerodrome DIEM/VVV pair", check_aerodrome_registration(w3, addresses)),
            ("Uniswap V3 VVV/USDC pool", check_uniswap_v3_registration(w3, addresses)),
        ]

        failed = False
        for label, status in statuses:
            print(f"\n=== {label} ===")
            print(f"Factory:  {status.factory}")
            print(f"Expected: {status.expected}")
            if status.reported:
                print(f"Factory get*: {status.reported}")
            else:
                print("Factory get*: <none>")
            print(f"Registered: {'yes' if status.registered else 'no'}")
            for note in status.notes:
                print(f"  - {note}")
            if not status.registered:
                failed = True

        if failed:
            print(
                "\n❌ At least one factory does not return the configured bridge pool."
                " Enable composite routing fallback or register the pool before enabling router execution."
            )
            if not args.warn_only:
                sys.exit(1)
        else:
            print("\n✅ Bridge pools are discoverable via factory lookups.")

    sp = sub.add_parser(
        "market:diem-bridge-check",
        help="Check DIEM pricing via bridge and path engine, verify price health",
    )
    sp.set_defaults(func=cmd_market_diem_bridge_check)

    sp = sub.add_parser(
        "market:bridge-factory-check",
        help=(
            "Check whether bridge pools are registered with Aerodrome and Uniswap factories "
            "(required before live DIEM router execution)"
        ),
    )
    sp.add_argument(
        "--warn-only",
        action="store_true",
        default=False,
        help="Print status but exit 0 even if registration is missing.",
    )
    sp.set_defaults(func=cmd_market_bridge_factory_check)

    # Broker admin commands
    sp = sub.add_parser("broker:tenants:list", help="List all tenants (admin)")
    sp.set_defaults(func=cmd_broker_tenants_list)

    sp = sub.add_parser(
        "broker:tenants:create",
        help="Create (or rotate) a tenant and issue a Venice subkey (admin)",
    )
    sp.add_argument("--tenant", required=True, help="Tenant id")
    sp.add_argument(
        "--label", required=False, default=None, help="Tenant label (required by API)"
    )
    sp.add_argument(
        "--quota", required=False, type=int, default=None, help="Daily DIEM quota"
    )
    sp.add_argument(
        "--expires-at",
        required=False,
        default=None,
        help="ISO8601 expiry (e.g., 2026-01-17T00:00:00Z)",
    )
    sp.add_argument(
        "--rotate",
        action="store_true",
        default=False,
        help="Rotate key for existing tenant",
    )
    sp.add_argument(
        "--revoke-old",
        action="store_true",
        default=False,
        help="When rotating, revoke old key_id if available",
    )
    sp.add_argument(
        "--tier",
        required=False,
        default=None,
        help="Optional broker limits classification label (e.g., premium, basic)",
    )
    sp.add_argument(
        "--window", type=int, required=False, help="Broker limits window seconds"
    )
    sp.add_argument(
        "--max", type=int, required=False, help="Broker limits max requests per window"
    )
    sp.set_defaults(func=cmd_broker_tenants_create)

    sp = sub.add_parser(
        "broker:tenants:probe-subkeys",
        help="Probe all SQL-backed tenants by calling Venice with their subkeys",
    )
    sp.set_defaults(func=cmd_broker_tenants_probe_subkeys)

    sp = sub.add_parser(
        "broker:tenants:subkey",
        help="Print a tenant subkey from the local store (operator only)",
    )
    sp.add_argument("--tenant", required=True, help="Tenant id")
    sp.set_defaults(func=cmd_broker_tenants_subkey)

    sp = sub.add_parser(
        "broker:limits:get", help="Get per-tenant broker limits (admin)"
    )
    sp.add_argument("--tenant", required=True, help="Tenant id")
    sp.set_defaults(func=cmd_broker_limits_get)

    sp = sub.add_parser(
        "broker:limits:set", help="Set per-tenant broker limits (admin)"
    )
    sp.add_argument("--tenant", required=True, help="Tenant id")
    sp.add_argument("--window", type=int, required=False, help="Window seconds")
    sp.add_argument("--max", type=int, required=False, help="Max requests per window")
    sp.add_argument(
        "--label", type=str, required=False, help="Label (e.g., premium, basic)"
    )
    sp.set_defaults(func=cmd_broker_limits_set)

    sp = sub.add_parser(
        "broker:me:usage",
        help="Fetch /v1/me/usage using tenant bearer auth",
    )
    sp.add_argument(
        "--auth-bearer",
        required=True,
        help="Tenant subkey (Authorization bearer)",
    )
    sp.add_argument(
        "--base-url",
        required=False,
        default=None,
        help="Override broker base URL (defaults to BROKER_BASE_URL / BROKER_API_HOST+PORT)",
    )
    sp.set_defaults(func=cmd_broker_me_usage)

    sp = sub.add_parser(
        "broker:activity:counts",
        help="Show SQL-backed broker counters (tenants/keys)",
    )
    sp.set_defaults(func=cmd_broker_activity_counts)

    # Idempotency admin
    sp = sub.add_parser("idem:purge", help="Purge idempotency keys by prefix")
    sp.add_argument("--prefix", required=True, help="Prefix like 'idem:chat:t-123'")
    sp.set_defaults(func=cmd_idem_purge)

    # Limiter probe
    sp = sub.add_parser(
        "probe:limits", help="Probe /v1/chat throughput and 429s vs limits"
    )
    sp.add_argument("--rps", type=float, default=float(os.getenv("PROBE_RPS", "10")))
    sp.add_argument(
        "--duration", type=int, default=int(os.getenv("PROBE_DURATION", "30"))
    )
    sp.add_argument(
        "--concurrency", type=int, default=int(os.getenv("PROBE_CONCURRENCY", "20"))
    )
    sp.add_argument("--model", required=False, default=os.getenv("PROBE_MODEL") or None)
    sp.add_argument(
        "--message", required=False, default=os.getenv("PROBE_MESSAGE", "hello")
    )
    sp.add_argument(
        "--no-idempotency",
        action="store_true",
        default=_env_flag("PROBE_NO_IDEMPOTENCY", False),
    )
    sp.add_argument(
        "--timeout", type=float, default=float(os.getenv("PROBE_TIMEOUT", "10"))
    )
    sp.add_argument(
        "--base-url",
        required=False,
        default=None,
        help="Override broker base URL (defaults to BROKER_BASE_URL)",
    )
    sp.add_argument(
        "--auth-bearer",
        required=False,
        default=os.getenv("PROBE_AUTH_BEARER") or None,
        help="Tenant subkey",
    )
    sp.add_argument(
        "--tenant",
        dest="tenant_id",
        required=False,
        default=os.getenv("PROBE_TENANT_ID") or None,
        help="Tenant id for admin mode",
    )
    sp.set_defaults(func=cmd_probe_limits)

    # Orchestrator loop
    sp = sub.add_parser(
        "run:orchestrator",
        help="Run orchestrator loop for ArbiDiem with persistence and backoff",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run without on-chain actions",
    )
    sp.add_argument(
        "--live",
        dest="dry_run",
        action="store_false",
        help="Alias to disable dry-run guard",
    )
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

    sp = sub.add_parser(
        "broker:tenants:revoke", help="Revoke a tenant's Venice subkey (admin)"
    )
    sp.add_argument("--tenant", required=True, help="Tenant id")
    sp.set_defaults(func=cmd_broker_tenant_revoke)

    sp = sub.add_parser(
        "broker:venice:subkey",
        help="Create a Venice scoped subkey via Broker API (admin)",
    )
    sp.add_argument("--label", required=True, help="Subkey description/label")
    sp.add_argument(
        "--diem",
        type=int,
        required=True,
        help="Daily DIEM consumption limit",
    )
    sp.add_argument(
        "--expires-at",
        required=False,
        default=None,
        help="ISO8601 expiry (defaults to BROKER_DEFAULT_EXPIRY_DAYS when set)",
    )
    sp.add_argument(
        "--parent-key",
        required=False,
        default=None,
        help="Optional parent key override; otherwise server env is used",
    )
    sp.set_defaults(func=cmd_broker_venice_subkey)

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
        except Exception as e:
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
            kid = (
                it.get("id")
                or it.get("keyId")
                or it.get("apiKeyId")
                or it.get("api_key_id")
            )
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
            except Exception as e:
                logger.warning(f"delete failed for {kid}: {e}")
        logger.info(
            f"cleanup complete: deleted={deleted} matched={len(targets)} dry_run={dry_run}"
        )

    sp = sub.add_parser(
        "venice:keys:cleanup", help="List/delete Venice API keys by description prefix"
    )
    sp.add_argument(
        "--prefix",
        required=False,
        default="",
        help="Description prefix to match (e.g., 'T1')",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Only list keys that would be deleted",
    )
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
            logger.error(
                "Failed to fetch OpenAPI from %s (tried /openapi.json and /api/openapi.json)",
                base,
            )
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
        summary: dict[str, Any] = {
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

    sp = sub.add_parser(
        "venice:probe-openapi",
        help="Detect VENICE_API_BASE_URL and key paths from OpenAPI",
    )
    sp.add_argument(
        "--base-url",
        required=False,
        default=None,
        help="Venice host (e.g., https://api.venice.ai)",
    )
    sp.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds")
    sp.set_defaults(func=cmd_venice_probe_openapi)

    # Data backfill: JSONL -> SQL AgentMemory
    sp = sub.add_parser(
        "data:backfill-memory", help="Backfill JSONL agent memory into SQL AgentMemory"
    )
    sp.add_argument(
        "--path",
        required=False,
        default=None,
        help="Path to JSONL file (defaults to AGENT_MEMORY_PATH)",
    )
    sp.set_defaults(func=cmd_data_backfill_memory)

    # Inspect recent orchestrator cycles
    sp = sub.add_parser(
        "orchestrator:cycles", help="Inspect recent single-loop orchestrator cycles"
    )
    sp.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of recent cycles to show (default: 10)",
    )
    sp.add_argument(
        "--path",
        required=False,
        default=None,
        help="Path to JSONL file (defaults to AGENT_MEMORY_PATH)",
    )
    sp.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output raw JSON instead of formatted summary",
    )
    sp.set_defaults(func=cmd_orchestrator_cycles)

    sp = sub.add_parser(
        "quorum:inspect", help="Inspect latest quorum decision from memory store"
    )
    sp.add_argument("--path", default=None, help="Path to agent_memory.jsonl")
    sp.add_argument("--json", action="store_true", help="Output raw JSON")
    sp.set_defaults(func=cmd_quorum_inspect)

    sp = sub.add_parser(
        "ops:verify:reflex-quorum",
        help="Verify recent cycles reflect configured reflex limits and quorum status",
    )
    sp.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Number of recent cycles to inspect (default: 3)",
    )
    sp.add_argument(
        "--path",
        default=None,
        help="Path to JSONL file (defaults to AGENT_MEMORY_PATH)",
    )
    sp.add_argument(
        "--alert-threshold",
        type=int,
        default=3,
        help="Raise alert if reflex halts and quorum skips persist for this many consecutive cycles",
    )
    sp.set_defaults(func=cmd_ops_verify_reflex_quorum)

    return p


def cmd_orchestrator_cycles(args: argparse.Namespace) -> None:
    """Inspect recent single-loop orchestrator cycles with progressive state."""
    import json

    from services.memory import MemoryStore

    path_value = args.path or os.getenv("AGENT_MEMORY_PATH") or "db/agent_memory.jsonl"
    store = MemoryStore(path=path_value)
    cycles = store.recent(limit=int(args.limit))

    if not cycles:
        logger.info("No cycles found in memory store")
        return

    if args.json:
        print(json.dumps(cycles, indent=2))
        return

    # Format summary
    for idx, entry in enumerate(reversed(cycles), 1):
        cycle = entry.get("cycle") if isinstance(entry, dict) else entry
        if not isinstance(cycle, dict):
            continue
        ts = cycle.get("ts") or entry.get("ts")
        progressive = cycle.get("progressive", {})
        prog_state = progressive.get("state", {})
        stake = cycle.get("stake", {})
        arbi = cycle.get("arbi", {})

        print(f"\n--- Cycle {idx} (ts={ts}) ---")
        print(
            f"Progressive: requested={progressive.get('requested')}, live={progressive.get('live')}"
        )
        if prog_state:
            print(
                f"  State: counter={prog_state.get('counter', 0)}, threshold={prog_state.get('threshold', 5)}, enabled={prog_state.get('enabled')}"
            )
            if prog_state.get("live"):
                print(f"  LIVE MODE ENABLED at {prog_state.get('enabled_at')}")
        stake_status = stake.get("status")
        stake_live = stake.get("live", False)
        heartbeat = stake.get("heartbeat", {})
        print(f"StakeMaster: status={stake_status}, live={stake_live}")
        print(
            f"  Heartbeat: sent={heartbeat.get('sent')}, error={heartbeat.get('error')}"
        )
        claim = stake.get("claim", {})
        print(
            f"  Claim: attempted={claim.get('attempted')}, executed={claim.get('executed')}, reason={claim.get('reason')}"
        )
        arbi_action = arbi.get("action", "hold")
        arbi_dry = arbi.get("dry_run", True)
        print(f"ArbiDiem: action={arbi_action}, dry_run={arbi_dry}")

        # Display quorum status
        quorum = arbi.get("quorum", {})
        quorum_status = quorum.get("status", "unknown")
        print(f"Quorum: status={quorum_status}")
        if quorum_status in {"approved", "blocked"}:
            ratio = quorum.get("ratio")
            threshold = quorum.get("threshold")
            confidence = quorum.get("confidence")
            if ratio is not None:
                print(f"  Vote ratio: {ratio:.2f} (threshold: {threshold:.2f})")
            if confidence is not None:
                print(f"  Confidence: {confidence:.2f}")
            breakdown = quorum.get("breakdown", [])
            if breakdown:
                print("  Model votes:")
                for vote in breakdown:
                    name = vote.get("name", "unknown")
                    approve = vote.get("approve", False)
                    weight = vote.get("weight", 0.0)
                    conf = vote.get("confidence", 0.0)
                    reason = vote.get("reason", "")
                    vote_str = "APPROVE" if approve else "BLOCK"
                    print(
                        f"    {name}: {vote_str} (weight={weight:.2f}, conf={conf:.2f}, reason={reason})"
                    )
        elif quorum_status in {"skipped", "not_invoked", "disabled"}:
            reason = quorum.get("reason", "unknown")
            print(f"  Reason: {reason}")
        elif quorum_status == "error":
            error = quorum.get("error", "unknown error")
            print(f"  Error: {error}")

        reflex = cycle.get("reflex", {})
        if reflex.get("halt"):
            print(f"Reflex: HALTED - {reflex.get('reasons', [])}")
        else:
            limits = reflex.get("limits", {})
            if limits:
                print(
                    f"Reflex: limits max_vol_bps={limits.get('max_vol_bps')}, "
                    f"max_utilization={limits.get('max_utilization')}, "
                    f"max_drawdown={limits.get('max_drawdown')}"
                )


def cmd_quorum_inspect(args: argparse.Namespace) -> None:
    """Inspect latest quorum decision from memory store."""
    import json

    from services.memory import MemoryStore

    path_value = args.path or os.getenv("AGENT_MEMORY_PATH") or "db/agent_memory.jsonl"
    store = MemoryStore(path=path_value)
    cycles = store.recent(limit=1)

    if not cycles:
        logger.info("No cycles found in memory store")
        return

    entry = cycles[0]
    cycle = entry.get("cycle") if isinstance(entry, dict) else entry
    if not isinstance(cycle, dict):
        logger.info("Invalid cycle format")
        return

    arbi = cycle.get("arbi", {})
    quorum = arbi.get("quorum", {})

    if args.json:
        print(json.dumps(quorum, indent=2))
        return

    quorum_status = quorum.get("status", "unknown")
    print(f"Quorum Status: {quorum_status}")

    if quorum_status in {"approved", "blocked"}:
        ratio = quorum.get("ratio")
        threshold = quorum.get("threshold")
        confidence = quorum.get("confidence")
        approved_weight = quorum.get("approvedWeight")
        total_weight = quorum.get("totalWeight")
        if ratio is not None:
            print(f"  Vote ratio: {ratio:.2f} (threshold: {threshold:.2f})")
        if approved_weight is not None and total_weight is not None:
            print(f"  Weight: {approved_weight:.2f} / {total_weight:.2f}")
        if confidence is not None:
            print(f"  Confidence: {confidence:.2f}")
        breakdown = quorum.get("breakdown", [])
        if breakdown:
            print("\n  Model votes:")
            for vote in breakdown:
                name = vote.get("name", "unknown")
                approve = vote.get("approve", False)
                weight = vote.get("weight", 0.0)
                conf = vote.get("confidence", 0.0)
                reason = vote.get("reason", "")
                vote_str = "APPROVE" if approve else "BLOCK"
                print(f"    {name}: {vote_str} (weight={weight:.2f}, conf={conf:.2f})")
                if reason:
                    print(f"      Reason: {reason}")
    elif quorum_status in {"skipped", "not_invoked", "disabled"}:
        reason = quorum.get("reason", "unknown")
        print(f"  Reason: {reason}")
    elif quorum_status == "error":
        error = quorum.get("error", "unknown error")
        print(f"  Error: {error}")
    else:
        print("  No quorum information available")


def cmd_ops_verify_reflex_quorum(args: argparse.Namespace) -> None:
    """Check that recent cycles respect configured reflex limits and quorum logging."""
    import json

    from services.memory import MemoryStore

    def _recent_cycles_from_sql(limit: int) -> list[dict[str, Any]] | None:
        """Best-effort fetch of recent cycles from SQL AgentMemory."""
        try:
            from sqlmodel import Session, select  # type: ignore

            from db.models import AgentMemory
            from db.session import get_engine
        except Exception:
            return None

        try:
            eng = get_engine()
        except Exception:
            return None

        try:
            with Session(eng) as session:  # type: ignore[call-arg]
                rows = session.exec(
                    select(AgentMemory)
                    .order_by(AgentMemory.created_at.desc())  # type: ignore[attr-defined]
                    .limit(limit)
                ).all()
        except Exception:
            return None

        if not rows:
            return []

        cycles: list[dict[str, Any]] = []
        for row in reversed(rows):
            payload = getattr(row, "payload", None)
            if not isinstance(payload, dict):
                continue
            ts_val = payload.get("ts")
            if ts_val is None:
                try:
                    created = getattr(row, "created_at", None)
                    ts_val = created.timestamp() if created else None
                except Exception:
                    ts_val = None
            cycles.append({"ts": ts_val, "cycle": payload})
        return cycles

    path_value = args.path or os.getenv("AGENT_MEMORY_PATH") or "db/agent_memory.jsonl"
    recent: list[dict[str, Any]] | None = _recent_cycles_from_sql(int(args.limit))
    source = "sql"
    if recent is None:
        store = MemoryStore(path=path_value)
        recent = store.recent(limit=int(args.limit))
        source = "json"

    if not recent:
        logger.error("No cycles found in memory store (source=%s)", source)
        raise SystemExit(1)

    limit_env_map = {
        "max_vol_bps": "REFLEX_MAX_VOL_BPS",
        "max_utilization": "REFLEX_MAX_UTILIZATION",
        "max_drawdown": "REFLEX_MAX_PRICE_DRAWDOWN",
    }
    expected_limits: dict[str, float] = {}
    for field, env_name in limit_env_map.items():
        raw = os.getenv(env_name)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            value = float(raw)
            if field == "max_drawdown" and value > 1.0:
                value = value / 100.0
            expected_limits[field] = value
        except ValueError:
            logger.warning("Unable to parse %s=%s; skipping comparison", env_name, raw)

    issues: list[str] = []
    combined_streak = 0
    max_combined_streak = 0

    def _cycle_payload(entry: object) -> dict | None:
        if isinstance(entry, dict):
            cycle = entry.get("cycle")
            if isinstance(cycle, dict):
                return cycle
            return entry if "arbi" in entry else None
        return entry if isinstance(entry, dict) else None

    for idx, entry in enumerate(recent, start=1):
        cycle = _cycle_payload(entry)
        if not isinstance(cycle, dict):
            issues.append(f"Cycle {idx}: invalid record structure")
            continue

        ts = cycle.get("ts") or (entry.get("ts") if isinstance(entry, dict) else None)
        arbi = cycle.get("arbi", {})
        reflex_block = cycle.get("reflex") or arbi.get("reflex")
        limits = {}
        if isinstance(reflex_block, dict):
            limits = reflex_block.get("limits", {}) or {}

        for field, expected_value in expected_limits.items():
            actual_value = limits.get(field)
            if actual_value is None or abs(float(actual_value) - expected_value) > 1e-6:
                issues.append(
                    f"Cycle {idx} (ts={ts}): reflex {field}={actual_value} "
                    f"(expected {expected_value})"
                )

        quorum = arbi.get("quorum") if isinstance(arbi, dict) else None
        if not isinstance(quorum, dict):
            issues.append(f"Cycle {idx} (ts={ts}): missing arbi.quorum block")
            quorum_status = None
        else:
            quorum_status = quorum.get("status")

        reflex_halt = bool(isinstance(reflex_block, dict) and reflex_block.get("halt"))
        quorum_skipped = quorum_status in {"skipped", "not_invoked"}

        if reflex_halt and quorum_skipped:
            combined_streak += 1
        else:
            max_combined_streak = max(max_combined_streak, combined_streak)
            combined_streak = 0

        print(
            json.dumps(
                {
                    "cycle_index": idx,
                    "ts": ts,
                    "reflex_halt": reflex_halt,
                    "reflex_limits": limits,
                    "quorum_status": quorum_status,
                    "quorum_reason": (
                        quorum.get("reason") if isinstance(quorum, dict) else None
                    ),
                },
                indent=2,
            )
        )

    max_combined_streak = max(max_combined_streak, combined_streak)
    alert_threshold = max(1, int(args.alert_threshold))
    if max_combined_streak >= alert_threshold:
        issues.append(
            f"Reflex halted and quorum skipped for {max_combined_streak} "
            f"consecutive cycle(s) (threshold={alert_threshold})"
        )

    if issues:
        logger.error(
            "Operational check failures:\n%s", "\n".join(f"- {msg}" for msg in issues)
        )
        raise SystemExit(1)

    logger.info(
        "Operational check passed for %s recent cycle(s); reflex limits match and quorum status present",
        len(recent),
    )


def cmd_data_backfill_memory(args: argparse.Namespace) -> None:
    """Load JSONL agent memory into SQL AgentMemory for analytics."""

    import json
    from uuid import uuid4

    path_value = args.path or os.getenv("AGENT_MEMORY_PATH") or "db/agent_memory.jsonl"
    path = Path(path_value).expanduser()
    if not path.exists():
        logger.error("Agent memory log not found: %s", path)
        return

    try:
        from sqlmodel import Session

        from db.models import AgentMemory
        from db.session import create_db_and_tables, get_engine
    except Exception as exc:
        logger.error("SQL backfill dependencies missing: %s", exc)
        return

    def _coerce_dt(*candidates: object) -> datetime:
        for candidate in candidates:
            if candidate is None:
                continue
            if isinstance(candidate, (int, float)):
                try:
                    return datetime.fromtimestamp(candidate, tz=timezone.utc)
                except Exception:
                    continue
            if isinstance(candidate, str):
                raw = candidate.strip()
                if not raw:
                    continue
                try:
                    return datetime.fromtimestamp(float(raw), tz=timezone.utc)
                except Exception:
                    pass
                try:
                    normalized = (
                        raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
                    )
                    return datetime.fromisoformat(normalized)
                except Exception:
                    continue
        return datetime.now(timezone.utc)

    def _extract_agent(payload: dict[str, object]) -> str:
        for key in ("agent", "actor", "name"):
            value = payload.get(key)
            if value:
                return str(value)
        agents = payload.get("agents")
        if isinstance(agents, dict) and agents:
            return str(next(iter(agents.keys())))
        return "system"

    def _extract_cycle_id(
        source: dict[str, object], payload: dict[str, object]
    ) -> str | None:
        for key in ("cycle_id", "cycleId", "cycleID"):
            value = payload.get(key) or source.get(key)
            if value:
                return str(value)
        return None

    create_db_and_tables()
    engine = get_engine()
    inserted = 0
    skipped = 0
    errors = 0
    batch_size = 256

    with path.open("r", encoding="utf-8") as fh, Session(engine) as session:  # type: ignore[call-arg]
        for line_no, raw_line in enumerate(fh, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                errors += 1
                logger.warning("Skipping line %s (invalid JSON): %s", line_no, exc)
                continue
            if not isinstance(entry, dict):
                skipped += 1
                continue
            payload = entry.get("cycle")
            if not isinstance(payload, dict):
                skipped += 1
                continue
            created_at = _coerce_dt(
                payload.get("created_at"), payload.get("ts"), entry.get("ts")
            )
            row = AgentMemory(
                id=uuid4().hex,
                agent=_extract_agent(payload),
                cycle_id=_extract_cycle_id(entry, payload),
                decision_id=None,
                created_at=created_at,
                payload=payload,
            )
            session.add(row)
            inserted += 1
            if inserted % batch_size == 0:
                session.commit()
        session.commit()

    logger.info(
        "Agent memory backfill complete: inserted=%s skipped=%s errors=%s source=%s",
        inserted,
        skipped,
        errors,
        path,
    )


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
