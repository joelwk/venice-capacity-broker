#!/usr/bin/env python3
"""Environment and readiness validator for the Venice broker stack.

Usage:
    python scripts/validate_broker_env.py [--json] [--export]
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Ensure repo root is importable when executed as a script (`python scripts/...`).
CURRENT_FILE = Path(__file__).resolve()
DEFAULT_REPO_ROOT = CURRENT_FILE.parents[1]
if str(DEFAULT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_REPO_ROOT))

REPO_ROOT = DEFAULT_REPO_ROOT

# Load shared dotenv files so local execution reflects repo configuration.
# Skip loading .env files when running in Docker - docker-compose already sets env vars
if not Path("/.dockerenv").exists():
    try:
        from apps._path import REPO_ROOT  # type: ignore
        from libs.env import load_dotenv_if_present  # type: ignore

        load_dotenv_if_present(path=str(REPO_ROOT / ".env"), override=False)
        docker_env = REPO_ROOT / ".env.docker"
        if docker_env.exists():
            load_dotenv_if_present(path=str(docker_env), override=True)
        local_env = REPO_ROOT / "docker" / ".env.local"
        if local_env.exists():
            load_dotenv_if_present(path=str(local_env), override=True)
    except Exception:
        REPO_ROOT = Path(__file__).resolve().parents[1]
else:
    try:
        from apps._path import REPO_ROOT  # type: ignore
    except Exception:
        REPO_ROOT = Path(__file__).resolve().parents[1]


STAGES: dict[str, str] = {
    "core_infra": "Core Infrastructure",
    "broker_api": "Broker API",
    "orchestrator_dry_run": "Single-loop Orchestrator (dry-run)",
    "orchestrator_live": "Single-loop Orchestrator (live trading)",
    "token_watcher": "Token Watcher Helper",
}

STAGE_NOTES: dict[str, list[str]] = {
    "orchestrator_dry_run": [
        "Default loop now runs StakeMaster -> quorum-gated ArbiDiem -> CapacityBroker -> AI Treasurer guidance. Disable quorum by setting QUORUM_ENABLE=0.",
    ],
    "orchestrator_live": [
        "Progressive-live gating (STAKEMASTER_PROGRESSIVE_ENABLE) is expected before enabling live trades.",
        "ArbiDiem guardrails still need calibration; review RISK_* thresholds before relying on unattended live execution.",
    ],
    "token_watcher": [
        "Watcher auto-starts in stack runs when AUTOSTART_TOKEN_WATCHER_ALLOW_NO_KEY=1; set AUTOSTART_TOKEN_WATCHER=0 to skip.",
        "Telemetry overlap is acceptable in v1; disable if another watcher already publishes the same signals.",
    ],
}

SEVERITY_ORDER = ("critical", "high", "medium", "low")
CRITICAL_SEVERITIES = {"critical", "high"}

PLACEHOLDER_MARKERS = {
    "<set",
    "changeme",
    "sample",
    "placeholder",
    "todo",
    "set-in-secrets",
    "xxxx",
}

_SCHEMA_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# Canonical addresses for Base mainnet (public contract addresses)
# Used as reference values for validation - these are public on-chain addresses, not secrets
CANONICAL_ADDRESSES = {
    "VVV_TOKEN_ADDRESS": "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",  # gitleaks:allow Base mainnet contract
    "VVV_STAKING_ADDRESS": "0x321b7ff75154472B18EDb199033fF4D116F340Ff",  # gitleaks:allow Base mainnet contract
    "DIEM_TOKEN_ADDRESS": "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # gitleaks:allow Base mainnet contract
    "QUOTE_TOKEN_ADDRESS": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # gitleaks:allow Base mainnet contract
    "WETH_ADDRESS": "0x4200000000000000000000000000000000000006",  # gitleaks:allow Base mainnet contract
    "DIEM_VVV_PAIR_ADDRESS": "0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d",  # gitleaks:allow Base mainnet contract
    "VVV_USDC_POOL_ADDRESS": "0x67A11022B7B6ed66f81233F6C8Ed6e48F7826530",  # gitleaks:allow Base mainnet contract
}


@dataclass
class Issue:
    severity: str
    category: str
    message: str
    impact: str
    remediation: str | None = None
    affects: tuple[str, ...] = ()


@dataclass
class StageCheck:
    name: str
    ok: bool
    detail: str | None = None


def env_value(name: str) -> str:
    return os.getenv(name, "").strip()


def is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def is_placeholder(value: str) -> bool:
    val = value.strip()
    if not val:
        return True
    lower = val.lower()
    return any(marker in lower for marker in PLACEHOLDER_MARKERS)


def is_eth_address(value: str) -> bool:
    if not value.startswith("0x") or len(value) != 42:
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


def add_issue(
    issues: list[Issue],
    severity: str,
    category: str,
    message: str,
    impact: str,
    remediation: str | None = None,
    affects: Iterable[str] = (),
) -> None:
    issues.append(
        Issue(
            severity=severity,
            category=category,
            message=message,
            impact=impact,
            remediation=remediation,
            affects=tuple(affects),
        )
    )


def record_stage_check(
    stage_checks: dict[str, list[StageCheck]],
    stages: Iterable[str],
    name: str,
    ok: bool,
    detail: str | None = None,
) -> None:
    for stage in stages:
        stage_checks.setdefault(stage, []).append(
            StageCheck(name=name, ok=ok, detail=detail)
        )


def configured_schema() -> str | None:
    raw = (
        os.getenv("DATABASE_SCHEMA")
        or os.getenv("SQL_DATABASE_SCHEMA")
        or os.getenv("SQL_SCHEMA")
        or os.getenv("SCHEMA")
    )
    if raw is None:
        return None
    schema = raw.strip()
    if not schema:
        return None
    if not _SCHEMA_NAME_RE.match(schema):
        return None
    return schema


def _is_placeholder_replit_kv_url(url: str) -> bool:
    """Detect Replit KV URLs that are missing the signed token segment.

    Valid Replit DB URLs look like: https://kv.replit.com/v0/<token>
    Anything that stops at /v0/ or lacks a second path segment cannot authenticate.
    """
    if not url:
        return True
    try:
        parsed = urlparse(url)
    except Exception:
        return True
    host = (parsed.netloc or "").strip().lower()
    if host not in {"kv.replit.com"}:
        return False
    segments = [seg for seg in parsed.path.strip("/").split("/") if seg]
    if not segments:
        return True
    if segments[0] != "v0":
        return False
    return len(segments) < 2


def _detect_execution_context() -> str:
    """Detect whether running in Docker, Replit, or local environment.

    Returns:
        "docker", "replit", or "local"
    """
    # Check for Docker
    if Path("/.dockerenv").exists():
        return "docker"

    # Check for Replit
    if (
        os.getenv("REPLIT_DB_URL")
        or os.getenv("REPLIT_ENVIRONMENT")
        or os.getenv("REPLIT_ENV")
        or os.getenv("REPLIT_WORKSPACE_ID")
        or os.getenv("REPLIT_APP_ID")
    ):
        return "replit"

    return "local"


# --- Helpers: SQL connectivity & Alembic head detection ---


def _sql_live_connection_ok() -> tuple[bool, str | None]:
    try:
        from sqlalchemy import text  # type: ignore

        from db.session import get_engine  # type: ignore

        eng = get_engine()
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:
        return False, str(exc)


def _alembic_head_revision() -> str | None:
    versions_dir = REPO_ROOT / "db" / "migrations" / "versions"
    if not versions_dir.exists():
        return None
    revs: list[str] = []
    for p in versions_dir.glob("*.py"):
        try:
            text = p.read_text(encoding="utf-8")
            m = re.search(r"revision\s*=\s*[\"']([^\"']+)[\"']", text)
            if m:
                revs.append(m.group(1).strip())
        except Exception:
            continue
    if not revs:
        return None
    # Prefer max lexicographically which works for zero-padded numeric ids (e.g., 0001, 0002)
    try:
        return sorted(revs)[-1]
    except Exception:
        return revs[-1]


def _alembic_db_revision() -> tuple[str | None, str | None]:
    try:
        from db.session import get_engine  # type: ignore

        eng = get_engine()
        with eng.connect() as c:
            schema = configured_schema()
            candidates = []
            if schema:
                candidates.append(f'"{schema}".alembic_version')
            candidates.append("alembic_version")
            last_error: Exception | None = None
            for table_sql in candidates:
                try:
                    res = c.exec_driver_sql(f"SELECT version_num FROM {table_sql}")
                    row = res.first()
                    return (row[0] if row else None), None
                except Exception as exc:
                    last_error = exc
                    try:
                        c.rollback()
                    except Exception:
                        pass
                    continue
            if last_error is not None:
                return None, str(last_error)
            return None, "missing alembic_version"
    except Exception as exc:
        return None, str(exc)


def validate_env() -> dict[str, Any]:
    issues: list[Issue] = []
    suggestions: list[dict[str, str]] = []
    stage_checks: dict[str, list[StageCheck]] = {k: [] for k in STAGES}

    def require_env(
        var: str,
        *,
        stages: Iterable[str],
        severity: str,
        category: str,
        message: str,
        impact: str,
        remediation: str | None = None,
        allow_placeholder: bool = False,
    ) -> str:
        value = env_value(var)
        ok = bool(value)
        detail = None

        if ok and not allow_placeholder and is_placeholder(value):
            ok = False
            detail = "placeholder value detected"

        record_stage_check(stage_checks, stages, f"{var} configured", ok, detail)

        if not ok:
            add_issue(
                issues,
                severity=severity,
                category=category,
                message=message,
                impact=impact,
                remediation=remediation,
                affects=stages,
            )
            if remediation and "=" in remediation:
                key, _, value_hint = remediation.partition("=")
                suggestions.append(
                    {
                        "key": key.strip(),
                        "value": value_hint.strip() or "<set-value>",
                        "reason": message,
                    }
                )

        return value

    # --- Core service configuration ---
    require_env(
        "BROKER_ADMIN_TOKEN",
        stages=("broker_api", "orchestrator_dry_run", "orchestrator_live"),
        severity="critical",
        category="security",
        message="BROKER_ADMIN_TOKEN is missing or placeholder",
        impact="Admin endpoints and tenant provisioning cannot be secured",
        remediation="BROKER_ADMIN_TOKEN=<strong-random-token>",
    )

    if not is_truthy(os.getenv("BROKER_REQUIRE_ADMIN_TOKEN")):
        record_stage_check(
            stage_checks,
            ("broker_api",),
            "Admin auth enforced",
            False,
            "BROKER_REQUIRE_ADMIN_TOKEN=false",
        )
        add_issue(
            issues,
            severity="high",
            category="security",
            message="BROKER_REQUIRE_ADMIN_TOKEN is not enabled",
            impact="Admin endpoints exposed without bearer token guard",
            remediation="BROKER_REQUIRE_ADMIN_TOKEN=true",
            affects=("broker_api",),
        )
        suggestions.append(
            {
                "key": "BROKER_REQUIRE_ADMIN_TOKEN",
                "value": "true",
                "reason": "Force admin routes to require the configured token",
            }
        )
    else:
        record_stage_check(stage_checks, ("broker_api",), "Admin auth enforced", True)

    require_env(
        "SESSION_SECRET",
        stages=("broker_api",),
        severity="high",
        category="security",
        message="SESSION_SECRET is missing or placeholder",
        impact="Session signing and CSRF protections degrade",
        remediation="SESSION_SECRET=<32+byte-random-secret>",
    )

    # --- Storage backends ---
    execution_context = _detect_execution_context()

    sql_url = require_env(
        "SQL_DATABASE_URL",
        stages=(
            "core_infra",
            "broker_api",
            "orchestrator_dry_run",
            "orchestrator_live",
        ),
        severity="critical",
        category="storage",
        message="SQL_DATABASE_URL is not configured",
        impact="Broker and orchestrator cannot access the Postgres database",
        remediation="SQL_DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/database",  # gitleaks:allow example connection string
    )

    if sql_url:
        lowered = sql_url.lower()
        if "sqlite" in lowered:
            record_stage_check(
                stage_checks,
                STAGES.keys(),
                "Postgres backend in use",
                False,
                "SQLite DSN detected",
            )

            # Enhanced warning for local runs
            if execution_context == "local":
                add_issue(
                    issues,
                    severity="high",
                    category="storage",
                    message="Local runs detected - SQL_DATABASE_URL points to SQLite",
                    impact="Host-side runs require Postgres for proper broker persistence; SQLite will cause data corruption under concurrency",
                    remediation="SQL_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres or set ALLOW_SQLITE_FALLBACK=1 for testing only",  # gitleaks:allow example connection string
                    affects=tuple(STAGES.keys()),
                )
            else:
                add_issue(
                    issues,
                    severity="high",
                    category="storage",
                    message="SQL_DATABASE_URL points to SQLite",
                    impact="Multi-process services require Postgres; SQLite will corrupt data under concurrency",
                    remediation="SQL_DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/database",  # gitleaks:allow example connection string
                    affects=tuple(STAGES.keys()),
                )
        else:
            record_stage_check(
                stage_checks, STAGES.keys(), "Postgres backend in use", True
            )
    elif execution_context == "local" and not is_truthy(
        os.getenv("ALLOW_SQLITE_FALLBACK")
    ):
        # Specific warning for local runs without SQL_DATABASE_URL and without explicit fallback
        record_stage_check(
            stage_checks,
            STAGES.keys(),
            "Local Postgres configured",
            False,
            "missing SQL_DATABASE_URL",
        )
        add_issue(
            issues,
            severity="high",
            category="storage",
            message="Local runs require explicit Postgres configuration",
            impact="Without SQL_DATABASE_URL, the system will attempt SQLite fallback which is disabled without ALLOW_SQLITE_FALLBACK=1",
            remediation="Configure SQL_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres or set ALLOW_SQLITE_FALLBACK=1 for dev/test",  # gitleaks:allow example connection string
            affects=tuple(STAGES.keys()),
        )

    # Replit production database validation
    if execution_context == "replit":
        replit_prod_db_ok = True
        replit_issues = []

        if not sql_url:
            replit_prod_db_ok = False
            replit_issues.append("SQL_DATABASE_URL not configured")
        elif sql_url:
            # Check if it's actually Postgres
            if "sqlite" in sql_url.lower():
                replit_prod_db_ok = False
                replit_issues.append("Using SQLite instead of Postgres")
            elif not ("postgresql://" in sql_url or "postgres://" in sql_url):
                replit_prod_db_ok = False
                replit_issues.append("DSN does not appear to be Postgres")

        # Check for DATABASE_SCHEMA (recommended for Replit production)
        schema = configured_schema()
        if not schema:
            replit_issues.append(
                "DATABASE_SCHEMA not set (recommended to avoid collisions)"
            )

        # Warn if SQL_CREATE_ALL_ON_START is set
        if is_truthy(os.getenv("SQL_CREATE_ALL_ON_START")):
            replit_issues.append(
                "SQL_CREATE_ALL_ON_START=1 detected; use Alembic migrations instead"
            )
            add_issue(
                issues,
                severity="medium",
                category="storage",
                message="SQL_CREATE_ALL_ON_START should not be used in Replit production",
                impact="Schema changes should be managed via Alembic migrations for production safety",
                remediation="Remove SQL_CREATE_ALL_ON_START and use: alembic upgrade head in replit_prestart.sh",
                affects=tuple(STAGES.keys()),
            )

        detail = None if replit_prod_db_ok else "; ".join(replit_issues)
        record_stage_check(
            stage_checks,
            STAGES.keys(),
            "Replit production database configured",
            replit_prod_db_ok,
            detail,
        )

        if not replit_prod_db_ok:
            remediation_steps = [
                "1. Provision production database in Replit dashboard (PostgreSQL 16 on Neon)",
                "2. Copy connection URL to Replit Secrets as SQL_DATABASE_URL",
                "3. Set DATABASE_SCHEMA to unique value (e.g., 'venice_broker')",
                "4. Run: alembic upgrade head via replit_prestart.sh",
                "See: https://docs.replit.com/cloud-services/storage-and-databases/production-databases.md",
            ]
            add_issue(
                issues,
                severity="high",
                category="storage",
                message="Replit production database not properly configured",
                impact="Replit deployments require production Postgres database for persistence and reliability",
                remediation="\n".join(remediation_steps),
                affects=tuple(STAGES.keys()),
            )

    # Live connection check
    ok, err = _sql_live_connection_ok()
    record_stage_check(
        stage_checks,
        STAGES.keys(),
        "Postgres live connection",
        ok,
        None if ok else str(err or "failed"),
    )
    if not ok:
        add_issue(
            issues,
            severity="critical",
            category="storage",
            message="Database connection failed (SELECT 1)",
            impact="Production must not start without a live Postgres connection",
            remediation="Verify SQL_DATABASE_URL credentials and network; ensure Postgres reachable",
            affects=tuple(STAGES.keys()),
        )

    # Alembic head consistency
    head = _alembic_head_revision()
    db_rev, db_err = _alembic_db_revision()
    if head and db_rev:
        match = head == db_rev
        record_stage_check(
            stage_checks,
            STAGES.keys(),
            "Migrations at head",
            match,
            None if match else f"db={db_rev} head={head}",
        )
        if not match:
            add_issue(
                issues,
                severity="high",
                category="storage",
                message="Alembic migrations are out of date",
                impact="Schema drift can break persistence and queries; upgrade required",
                remediation="Run: alembic upgrade head",
                affects=tuple(STAGES.keys()),
            )
    elif head and not db_rev:
        record_stage_check(
            stage_checks,
            STAGES.keys(),
            "Migrations at head",
            False,
            str(db_err or "missing alembic_version"),
        )
        add_issue(
            issues,
            severity="high",
            category="storage",
            message="Database missing alembic_version or unreadable",
            impact="Migrations not applied; tables may be missing",
            remediation="Run: alembic upgrade head",
            affects=tuple(STAGES.keys()),
        )

    redis_stages = (
        "core_infra",
        "broker_api",
        "orchestrator_dry_run",
        "orchestrator_live",
        "token_watcher",
    )
    redis_url = env_value("REDIS_URL") or env_value("KV_REDIS_URL")
    kv_url = env_value("KV_URL")
    replit_db_url = env_value("REPLIT_DB_URL")
    kv_placeholder_reason: str | None = None
    kv_fallback_url = ""

    # Check for live mode flags
    is_live_mode = (
        is_truthy(os.getenv("AUTOSTART_ORCHESTRATOR_LIVE"))
        or is_truthy(os.getenv("AUTOSTART_STAKEMASTER_LIVE"))
        or env_value("RUN_STACK_MODE") == "live"
    )

    if replit_db_url:
        if _is_placeholder_replit_kv_url(replit_db_url):
            kv_placeholder_reason = "REPLIT_DB_URL missing /v0/<token>"
        else:
            kv_fallback_url = replit_db_url
    elif kv_url:
        if _is_placeholder_replit_kv_url(kv_url):
            kv_placeholder_reason = (
                "KV_URL points to https://kv.replit.com/v0/ without token"
            )
        else:
            kv_fallback_url = kv_url

    if redis_url:
        record_stage_check(stage_checks, redis_stages, "REDIS_URL configured", True)
    elif kv_fallback_url:
        record_stage_check(
            stage_checks, redis_stages, "REDIS_URL configured", True, "kv fallback"
        )
    else:
        detail = kv_placeholder_reason or "missing"
        record_stage_check(
            stage_checks, redis_stages, "REDIS_URL configured", False, detail
        )
        add_issue(
            issues,
            severity="high",
            category="storage",
            message="REDIS_URL is not configured",
            impact="Rate limits, KV counters, and orchestrator coordination will fail",
            remediation="REDIS_URL=redis://redis:6379/0",
            affects=redis_stages,
        )
        suggestions.append(
            {
                "key": "REDIS_URL",
                "value": "redis://redis:6379/0",
                "reason": "REDIS_URL is not configured",
            }
        )
    if kv_placeholder_reason:
        add_issue(
            issues,
            severity="high",
            category="storage",
            message="Replit KV URL is a placeholder",
            impact="Requests hit https://kv.replit.com/v0/ without the signed token and return 403, so StakeMaster cannot send heartbeats or enforce rate limits.",
            remediation="Set REPLIT_DB_URL to the full value from `echo $REPLIT_DB_URL` (includes /v0/<token>) or provide REDIS_URL/KV_REDIS_URL.",
            affects=redis_stages,
        )
        suggestions.append(
            {
                "key": "REPLIT_DB_URL",
                "value": "<copy-from-echo-$REPLIT_DB_URL>",
                "reason": kv_placeholder_reason,
            }
        )

    # Critical check: Live mode must not use in-memory KV fallback
    if is_live_mode:
        has_durable_kv = bool(redis_url or kv_fallback_url)
        allow_inmem = is_truthy(os.getenv("ALLOW_INMEMORY_KV_FALLBACK"))

        if allow_inmem:
            record_stage_check(
                stage_checks,
                ("orchestrator_live",),
                "Durable KV store configured for live mode",
                False,
                "ALLOW_INMEMORY_KV_FALLBACK=1",
            )
            add_issue(
                issues,
                severity="critical",
                category="storage",
                message="Live mode cannot use in-memory KV fallback",
                impact="Rate limits, orchestrator coordination, and heartbeats will be lost on restart; data loss guaranteed",
                remediation="Set ALLOW_INMEMORY_KV_FALLBACK=0 (or remove it) and configure REDIS_URL or REPLIT_DB_URL",
                affects=("orchestrator_live",),
            )
            suggestions.append(
                {
                    "key": "ALLOW_INMEMORY_KV_FALLBACK",
                    "value": "0",
                    "reason": "Live mode requires durable KV store",
                }
            )
        elif not has_durable_kv:
            record_stage_check(
                stage_checks,
                ("orchestrator_live",),
                "Durable KV store configured for live mode",
                False,
                "no durable KV",
            )
            add_issue(
                issues,
                severity="critical",
                category="storage",
                message="Live mode requires durable KV store (REDIS_URL or REPLIT_DB_URL)",
                impact="Orchestrator coordination and rate limiting will fail in live mode",
                remediation="Configure REDIS_URL=redis://redis:6379/0 or set REPLIT_DB_URL in secrets",
                affects=("orchestrator_live",),
            )
        else:
            record_stage_check(
                stage_checks,
                ("orchestrator_live",),
                "Durable KV store configured for live mode",
                True,
            )

    # --- Venice API integration ---
    venice_base = require_env(
        "VENICE_API_BASE_URL",
        stages=tuple(STAGES.keys()),
        severity="critical",
        category="venice",
        message="VENICE_API_BASE_URL is not set",
        impact="Broker cannot reach Venice API for sub-keys or metrics",
        remediation="VENICE_API_BASE_URL=https://api.venice.ai/api/v1",
    )

    if venice_base:
        cleaned = venice_base.rstrip("/")
        if not cleaned.endswith("/api/v1"):
            record_stage_check(
                stage_checks,
                STAGES.keys(),
                "Venice base URL includes /api/v1",
                False,
                cleaned,
            )
            add_issue(
                issues,
                severity="high",
                category="venice",
                message="VENICE_API_BASE_URL must include /api/v1",
                impact="SDK calls hit wrong Venice path prefix and will 404",
                remediation="VENICE_API_BASE_URL=https://api.venice.ai/api/v1",
                affects=tuple(STAGES.keys()),
            )
        else:
            record_stage_check(
                stage_checks, STAGES.keys(), "Venice base URL includes /api/v1", True
            )

    require_env(
        "VENICE_PARENT_KEY",
        stages=tuple(STAGES.keys()),
        severity="critical",
        category="venice",
        message="VENICE_PARENT_KEY is missing or placeholder",
        impact="Broker cannot mint scoped API keys or revoke compromised keys",
        remediation="VENICE_PARENT_KEY=<parent-key-from-venice>",
    )

    require_env(
        "VENICE_API_KEY",
        stages=tuple(STAGES.keys()),
        severity="high",
        category="venice",
        message="VENICE_API_KEY is missing or placeholder",
        impact="Broker cannot proxy inference requests to Venice",
        remediation="VENICE_API_KEY=<parent-or-sub-key>",
    )

    # --- On-chain connectivity ---
    require_env(
        "BASE_RPC_URL",
        stages=("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
        severity="high",
        category="onchain",
        message="BASE_RPC_URL is missing",
        impact="On-chain staking and DEX pricing cannot execute",
        remediation="BASE_RPC_URL=https://mainnet.base.org",
    )

    base_chain_id = env_value("BASE_CHAIN_ID")
    if base_chain_id:
        record_stage_check(
            stage_checks,
            ("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
            "BASE_CHAIN_ID configured",
            True,
        )
        try:
            parsed_id = int(base_chain_id, 10)
        except ValueError:
            record_stage_check(
                stage_checks,
                ("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
                "Valid Base chain id",
                False,
                base_chain_id,
            )
            add_issue(
                issues,
                severity="high",
                category="onchain",
                message="BASE_CHAIN_ID is not an integer",
                impact="Web3 client cannot enforce chain guards",
                remediation="BASE_CHAIN_ID=8453",
                affects=("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
            )
        else:
            if parsed_id != 8453:
                record_stage_check(
                    stage_checks,
                    ("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
                    "Using Base mainnet chain id",
                    False,
                    base_chain_id,
                )
                add_issue(
                    issues,
                    severity="medium",
                    category="onchain",
                    message=f"BASE_CHAIN_ID={parsed_id} does not match Base mainnet (8453)",
                    impact="Addresses baked into the bundle expect Base mainnet",
                    remediation="BASE_CHAIN_ID=8453",
                    affects=(
                        "orchestrator_dry_run",
                        "orchestrator_live",
                        "token_watcher",
                    ),
                )
            else:
                record_stage_check(
                    stage_checks,
                    ("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
                    "Using Base mainnet chain id",
                    True,
                )
    else:
        record_stage_check(
            stage_checks,
            ("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
            "BASE_CHAIN_ID configured",
            False,
            "missing",
        )
        add_issue(
            issues,
            severity="high",
            category="onchain",
            message="BASE_CHAIN_ID is missing",
            impact="Web3 client cannot validate the target network",
            remediation="BASE_CHAIN_ID=8453",
            affects=("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
        )

    # --- Contract address validation ---
    for env_key, expected in CANONICAL_ADDRESSES.items():
        value = env_value(env_key)
        stages = ("orchestrator_dry_run", "orchestrator_live", "token_watcher")
        # ID-004: Include DIEM/VVV addresses in broker_api stage checks
        if env_key in {
            "VVV_TOKEN_ADDRESS",
            "QUOTE_TOKEN_ADDRESS",
            "DIEM_TOKEN_ADDRESS",
            "DIEM_VVV_PAIR_ADDRESS",
            "VVV_USDC_POOL_ADDRESS",
        }:
            stages = (
                "broker_api",
                "orchestrator_dry_run",
                "orchestrator_live",
                "token_watcher",
            )

        if not value:
            record_stage_check(
                stage_checks, stages, f"{env_key} configured", False, "missing"
            )
            add_issue(
                issues,
                severity="high",
                category="contracts",
                message=f"{env_key} is not set",
                impact="On-chain integrations will fail for the referenced asset",
                remediation=f"{env_key}={expected}",
                affects=stages,
            )
            continue

        if not is_eth_address(value):
            record_stage_check(
                stage_checks, stages, f"{env_key} valid address", False, value
            )
            add_issue(
                issues,
                severity="high",
                category="contracts",
                message=f"{env_key} is not a valid Ethereum address ({value})",
                impact="Transactions will revert due to malformed address",
                remediation=f"{env_key}={expected}",
                affects=stages,
            )
            continue

        record_stage_check(stage_checks, stages, f"{env_key} valid address", True)

        if value.lower() != expected.lower():
            record_stage_check(
                stage_checks,
                stages,
                f"{env_key} matches canonical Base mainnet",
                False,
                value,
            )
            add_issue(
                issues,
                severity="medium",
                category="contracts",
                message=f"{env_key}={value} does not match canonical Base mainnet address",
                impact="Bundle assumptions (ABIs, pool routing) expect Base mainnet defaults",
                remediation=f"{env_key}={expected}",
                affects=stages,
            )
        else:
            record_stage_check(
                stage_checks, stages, f"{env_key} matches canonical Base mainnet", True
            )

    # --- Wallet address validation ---
    wallet_address_vars = [
        (
            "TREASURY_ADDRESS",
            "broker_api",
            "Portfolio tracking and broker treasury operations",
        ),
        ("COLD_WALLET_ADDRESS", "orchestrator_live", "Cold wallet sweep operations"),
    ]
    for env_key, primary_stage, description in wallet_address_vars:
        value = env_value(env_key)
        stages = (primary_stage, "orchestrator_dry_run", "orchestrator_live")

        # Check if placeholder value is set
        if value and is_placeholder(value):
            record_stage_check(
                stage_checks,
                stages,
                f"{env_key} configured",
                False,
                "placeholder detected",
            )
            add_issue(
                issues,
                severity="high",
                category="wallet",
                message=f"{env_key} is set to a placeholder value ({value})",
                impact=f"{description} will fail; portfolio tracking unavailable",
                remediation=f"Set {env_key} to a valid Base address (0x...) or leave unset to use default provider",
                affects=stages,
            )
            continue

        # If set, validate it's a proper address
        if value:
            if not is_eth_address(value):
                record_stage_check(
                    stage_checks, stages, f"{env_key} valid address", False, value
                )
                add_issue(
                    issues,
                    severity="high",
                    category="wallet",
                    message=f"{env_key} is not a valid Ethereum address ({value})",
                    impact=f"{description} will fail",
                    remediation=f"Set {env_key} to a valid Base address (0x...)",
                    affects=stages,
                )
            else:
                record_stage_check(
                    stage_checks, stages, f"{env_key} valid address", True
                )

    # --- Broker feature toggles ---
    if not is_truthy(os.getenv("QUOTES_ENABLED")):
        record_stage_check(
            stage_checks, ("broker_api",), "Quote endpoint enabled", False
        )
        add_issue(
            issues,
            severity="high",
            category="features",
            message="QUOTES_ENABLED is false",
            impact="Buyers cannot preview capacity quotes",
            remediation="QUOTES_ENABLED=true",
            affects=("broker_api",),
        )
        suggestions.append(
            {
                "key": "QUOTES_ENABLED",
                "value": "true",
                "reason": "Enable quote previews",
            }
        )
    else:
        record_stage_check(
            stage_checks, ("broker_api",), "Quote endpoint enabled", True
        )

    if not is_truthy(os.getenv("PURCHASES_ENABLED")):
        record_stage_check(
            stage_checks, ("broker_api",), "Purchase endpoint enabled", False
        )
        add_issue(
            issues,
            severity="critical",
            category="features",
            message="PURCHASES_ENABLED is false",
            impact="Verification and fulfillment endpoints stay disabled",
            remediation="PURCHASES_ENABLED=true",
            affects=("broker_api",),
        )
        suggestions.append(
            {
                "key": "PURCHASES_ENABLED",
                "value": "true",
                "reason": "Enable tenant verification flow",
            }
        )
    else:
        record_stage_check(
            stage_checks, ("broker_api",), "Purchase endpoint enabled", True
        )

    cors_enabled = is_truthy(os.getenv("CORS_ENABLED"))
    cors_origins = env_value("CORS_ALLOW_ORIGINS")
    if cors_enabled and cors_origins:
        record_stage_check(stage_checks, ("broker_api",), "CORS configured", True)
    elif cors_enabled and not cors_origins:
        record_stage_check(
            stage_checks, ("broker_api",), "CORS configured", False, "no origins listed"
        )
        add_issue(
            issues,
            severity="medium",
            category="cors",
            message="CORS_ENABLED=true but CORS_ALLOW_ORIGINS is empty",
            impact="Browser-based admin UI cannot authenticate cross-origin",
            remediation="CORS_ALLOW_ORIGINS=https://your-admin-host",
            affects=("broker_api",),
        )
        suggestions.append(
            {
                "key": "CORS_ALLOW_ORIGINS",
                "value": "https://your-admin-host",
                "reason": "Allow admin UI to call the broker API",
            }
        )
    elif not cors_enabled:
        record_stage_check(
            stage_checks, ("broker_api",), "CORS configured", False, "disabled"
        )

    # --- Pricing routes ---
    trade_path = env_value("TRADE_PATH")
    dynamic_paths = is_truthy(os.getenv("TRADE_PATHS_DYNAMIC", "true"))
    if trade_path:
        record_stage_check(
            stage_checks,
            ("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
            "Static DIEM trade path present",
            True,
        )
    elif not dynamic_paths:
        add_issue(
            issues,
            severity="medium",
            category="pricing",
            message="No static TRADE_PATH and dynamic discovery disabled",
            impact="DEX routing may fail without a discoverable path",
            remediation="Enable dynamic discovery (TRADE_PATHS_DYNAMIC=true) or set a TRADE_PATH override",
            affects=("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
        )
    else:
        record_stage_check(
            stage_checks,
            ("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
            "Dynamic trade path discovery enabled (no static override)",
            True,
        )

    # --- Debug instrumentation flags ---
    if is_truthy(os.getenv("MARKETDATA_DEBUG_SANITY")):
        add_issue(
            issues,
            severity="low",
            category="debug",
            message="MARKETDATA_DEBUG_SANITY is enabled",
            impact="Verbose pricing logs increase noise; disable after investigations",
            remediation="MARKETDATA_DEBUG_SANITY=0",
            affects=("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
        )

    if is_truthy(os.getenv("DIEM_DEBUG_ROUTES")):
        add_issue(
            issues,
            severity="low",
            category="debug",
            message="DIEM_DEBUG_ROUTES is enabled",
            impact="Runtime logs include internal routing diagnostics; disable for production",
            remediation="DIEM_DEBUG_ROUTES=0",
            affects=("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
        )

    # --- Live trading prerequisites ---
    eth_private_key = env_value("ETH_PRIVATE_KEY")
    live_secret_ok = bool(eth_private_key) and not is_placeholder(eth_private_key)
    record_stage_check(
        stage_checks,
        ("orchestrator_live",),
        "ETH private key present",
        live_secret_ok,
        None if live_secret_ok else "missing",
    )
    if not live_secret_ok:
        add_issue(
            issues,
            severity="critical",
            category="live",
            message="ETH_PRIVATE_KEY is missing or placeholder",
            impact="Live staking, minting, and trading cannot sign transactions",
            remediation="ETH_PRIVATE_KEY=<hex-encoded-private-key>",
            affects=("orchestrator_live",),
        )

    cdp_required = {
        "CDP_API_KEY_ID": "CDP API key id is missing",
        "CDP_API_KEY_SECRET": "CDP API key secret is missing",
        "CDP_WALLET_SECRET": "CDP wallet secret is missing",
    }
    for env_key, msg in cdp_required.items():
        val = env_value(env_key)
        ok = bool(val) and not is_placeholder(val)
        record_stage_check(
            stage_checks,
            ("orchestrator_live",),
            f"{env_key} configured",
            ok,
            None if ok else "missing",
        )
        if not ok:
            add_issue(
                issues,
                severity="high",
                category="live",
                message=msg,
                impact="Coinbase smart wallet flows cannot run in live mode",
                remediation=f"{env_key}=<value-from-coinbase-cloud>",
                affects=("orchestrator_live",),
            )

    if is_truthy(os.getenv("AGENTS_PAUSED")):
        add_issue(
            issues,
            severity="medium",
            category="orchestrator",
            message="AGENTS_PAUSED=true pauses orchestrator actions",
            impact="Loop remains idle even if other checks pass",
            remediation="AGENTS_PAUSED=false",
            affects=("orchestrator_dry_run", "orchestrator_live"),
        )

    # --- Artifact presence ---
    is_replit_workspace = bool(
        os.getenv("REPLIT_DB_URL")
        or os.getenv("REPLIT_APP_ID")
        or os.getenv("REPLIT_ENV")
        or os.getenv("REPLIT_PLATFORM")
    )

    required_files = [
        (
            ".env",
            ("core_infra",),
            "Base environment file expected for shared configuration",
        ),
        (
            ".env.docker",
            ("core_infra",),
            "Docker-specific overrides required for `docker compose --env-file .env.docker up`",
        ),
        (
            "docker-compose.yml",
            ("core_infra",),
            "Docker Compose manifest required for standard `docker compose --env-file .env.docker up` run",
        ),
        (
            "abi/diem.json",
            ("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
            "DIEM ABI required for staking helpers",
        ),
        ("libs/venice_sdk/client.py", ("broker_api",), "Venice SDK client missing"),
    ]
    for rel_path, stages, description in required_files:
        file_path = REPO_ROOT / rel_path
        if not file_path.exists():
            if rel_path == ".env.docker" and is_replit_workspace:
                record_stage_check(
                    stage_checks,
                    stages,
                    f"{rel_path} present",
                    True,
                    "skipped in replit",
                )
                continue
            record_stage_check(
                stage_checks, stages, f"{rel_path} present", False, "missing"
            )
            add_issue(
                issues,
                severity="critical",
                category="artifacts",
                message=f"{rel_path} is missing",
                impact=description,
                remediation=f"Ensure {rel_path} exists in the repository build context",
                affects=stages,
            )
        else:
            record_stage_check(stage_checks, stages, f"{rel_path} present", True)

    compose_text = ""
    compose_path = REPO_ROOT / "docker-compose.yml"
    if compose_path.exists():
        try:
            compose_text = compose_path.read_text()
        except Exception:
            compose_text = ""

    if compose_text:

        def _compose_has_service(name: str) -> bool:
            pattern = rf"^\s*{re.escape(name)}:\s*$"
            return re.search(pattern, compose_text, re.MULTILINE) is not None

        compose_requirements = [
            (
                "postgres",
                ("core_infra",),
                "critical",
                "docker-compose.yml lacks 'postgres' service",
            ),
            (
                "redis",
                ("core_infra",),
                "critical",
                "docker-compose.yml lacks 'redis' service",
            ),
            (
                "broker",
                ("core_infra", "broker_api"),
                "critical",
                "docker-compose.yml lacks 'broker' service",
            ),
            (
                "orchestrator",
                ("orchestrator_dry_run", "orchestrator_live"),
                "high",
                "docker-compose.yml lacks 'orchestrator' service",
            ),
            (
                "token-watcher",
                ("token_watcher",),
                "low",
                "docker-compose.yml lacks 'token-watcher' helper",
            ),
        ]
        for service_name, stages, severity, message in compose_requirements:
            present = _compose_has_service(service_name)
            record_stage_check(
                stage_checks,
                stages,
                f"docker-compose service '{service_name}' defined",
                present,
                None if present else "missing",
            )
            if not present:
                impact = "Service cannot be started via docker compose command"
                remediation = f"Add '{service_name}' definition to docker-compose.yml"
                add_issue(
                    issues,
                    severity=severity,
                    category="compose",
                    message=message,
                    impact=impact,
                    remediation=remediation,
                    affects=stages,
                )

    # --- Stage readiness calculation ---
    stage_status: dict[str, dict[str, Any]] = {}
    for stage_key, stage_label in STAGES.items():
        checks = stage_checks.get(stage_key, [])
        stage_issues = [issue for issue in issues if stage_key in issue.affects]
        blockers = [
            issue for issue in stage_issues if issue.severity in CRITICAL_SEVERITIES
        ]
        warnings = [
            issue for issue in stage_issues if issue.severity not in CRITICAL_SEVERITIES
        ]
        missing_checks = [check for check in checks if not check.ok]

        status = "ready"
        if blockers or any(
            check
            for check in missing_checks
            if check.detail in {"missing", "placeholder value detected"}
        ):
            status = "blocked"
        elif warnings or missing_checks:
            status = "degraded"

        notes = STAGE_NOTES.get(stage_key, [])

        stage_status[stage_key] = {
            "label": stage_label,
            "status": status,
            "blockers": [issue.message for issue in blockers],
            "warnings": [issue.message for issue in warnings],
            "checks": [asdict(check) for check in checks],
            "notes": notes,
        }

    counts = dict.fromkeys(SEVERITY_ORDER, 0)
    for issue in issues:
        counts[issue.severity] = counts.get(issue.severity, 0) + 1

    report = {
        "issues": [asdict(issue) for issue in issues],
        "suggestions": suggestions,
        "counts": counts,
        "stages": stage_status,
    }

    return report


def print_report(report: dict[str, Any], export: bool = False) -> None:
    stage_values = list(report["stages"].values())
    print("Stage readiness")
    print("=" * 60)
    for stage in stage_values:
        label = stage["label"]
        status = stage["status"].upper()
        print(f"{label}: {status}")
        for blocker in stage["blockers"]:
            print(f"  blocker: {blocker}")
        for warning in stage["warnings"]:
            print(f"  warning: {warning}")
        failed_checks = [
            check for check in stage.get("checks", []) if not check.get("ok")
        ]
        for check in failed_checks:
            detail = check.get("detail")
            suffix = f" ({detail})" if detail else ""
            print(f"  check failed: {check['name']}{suffix}")
        for note in stage.get("notes", []):
            print(f"  note: {note}")
        print()

    issues = report["issues"]
    counts = report["counts"]

    if not issues:
        non_ready = [stage for stage in stage_values if stage["status"] != "ready"]
        if non_ready:
            labels = ", ".join(stage["label"] for stage in non_ready)
            print(f"[warn] Stage readiness requires attention: {labels}")
        else:
            print("[ok] Environment configuration looks good!")
        return

    total = sum(counts.get(sev, 0) for sev in SEVERITY_ORDER)
    summary_parts = [
        f"{counts.get(sev, 0)} {sev}" for sev in SEVERITY_ORDER if counts.get(sev, 0)
    ]
    summary = ", ".join(summary_parts)

    print("Detailed issues")
    print("=" * 60)
    print(f"Found {total} issues ({summary})\n")

    for idx, issue in enumerate(issues, 1):
        sev = issue["severity"].upper()
        cat = issue["category"].upper()
        print(f"{idx}. [{sev}] {cat}")
        print(f"   {issue['message']}")
        print(f"   Impact: {issue['impact']}")
        if issue.get("remediation"):
            print(f"   Remediation: {issue['remediation']}")
        print(f"   Affects: {', '.join(issue['affects']) or 'n/a'}\n")

    suggestions = report["suggestions"]
    if not suggestions:
        return

    print("=" * 60)
    if export:
        print("Suggested fixes (export statements):\n")
        for item in suggestions:
            key = item["key"]
            value = item["value"]
            print(f'export {key}="{value}"')
    else:
        print("Suggested fixes (update your env files or secrets):\n")
        for item in suggestions:
            print(f"{item['key']}={item['value']}  # {item['reason']}")


def main() -> None:
    report = validate_env()

    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
        return

    export = "--export" in sys.argv
    print_report(report, export=export)

    counts: dict[str, int] = report["counts"]
    if counts.get("critical", 0) > 0:
        sys.exit(2)
    if counts.get("high", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
