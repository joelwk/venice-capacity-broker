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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

# Ensure repo root is importable when executed as a script (`python scripts/...`).
CURRENT_FILE = Path(__file__).resolve()
DEFAULT_REPO_ROOT = CURRENT_FILE.parents[1]
if str(DEFAULT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_REPO_ROOT))

REPO_ROOT = DEFAULT_REPO_ROOT

# Load shared dotenv files so local execution reflects repo configuration.
try:
    from libs.env import load_dotenv_if_present  # type: ignore
    from apps._path import REPO_ROOT  # type: ignore

    load_dotenv_if_present(path=str(REPO_ROOT / ".env"), override=False)
    docker_env = REPO_ROOT / ".env.docker"
    if docker_env.exists():
        load_dotenv_if_present(path=str(docker_env), override=True)
except Exception:
    REPO_ROOT = Path(__file__).resolve().parents[1]


STAGES: Dict[str, str] = {
    "core_infra": "Core Infrastructure",
    "broker_api": "Broker API",
    "orchestrator_dry_run": "Single-loop Orchestrator (dry-run)",
    "orchestrator_live": "Single-loop Orchestrator (live trading)",
    "token_watcher": "Token Watcher Helper",
}

STAGE_NOTES: Dict[str, List[str]] = {
    "orchestrator_dry_run": [
        "Quorum coordinator remains staged for post-v1; current loop runs StakeMaster -> ArbiDiem -> CapacityBroker.",
    ],
    "orchestrator_live": [
        "Progressive-live gating (STAKEMASTER_PROGRESSIVE_ENABLE) is expected before enabling live trades.",
    ],
    "token_watcher": [
        "Helper runs under docker compose profile 'helpers'; enable with `docker compose --profile helpers up` when needed.",
        "Watcher is optional in v1; leave disabled if orchestrator already covers your telemetry requirements.",
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
    affects: Tuple[str, ...] = ()


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
    issues: List[Issue],
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
    stage_checks: Dict[str, List[StageCheck]],
    stages: Iterable[str],
    name: str,
    ok: bool,
    detail: str | None = None,
) -> None:
    for stage in stages:
        stage_checks.setdefault(stage, []).append(StageCheck(name=name, ok=ok, detail=detail))


def validate_env() -> Dict[str, Any]:
    issues: List[Issue] = []
    suggestions: List[Dict[str, str]] = []
    stage_checks: Dict[str, List[StageCheck]] = {k: [] for k in STAGES}

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
    admin_token = require_env(
        "BROKER_ADMIN_TOKEN",
        stages=("broker_api", "orchestrator_dry_run", "orchestrator_live"),
        severity="critical",
        category="security",
        message="BROKER_ADMIN_TOKEN is missing or placeholder",
        impact="Admin endpoints and tenant provisioning cannot be secured",
        remediation="BROKER_ADMIN_TOKEN=<strong-random-token>",
    )

    if not is_truthy(os.getenv("BROKER_REQUIRE_ADMIN_TOKEN")):
        record_stage_check(stage_checks, ("broker_api",), "Admin auth enforced", False, "BROKER_REQUIRE_ADMIN_TOKEN=false")
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

    session_secret = require_env(
        "SESSION_SECRET",
        stages=("broker_api",),
        severity="high",
        category="security",
        message="SESSION_SECRET is missing or placeholder",
        impact="Session signing and CSRF protections degrade",
        remediation="SESSION_SECRET=<32+byte-random-secret>",
    )

    # --- Storage backends ---
    sql_url = require_env(
        "SQL_DATABASE_URL",
        stages=("core_infra", "broker_api", "orchestrator_dry_run", "orchestrator_live"),
        severity="critical",
        category="storage",
        message="SQL_DATABASE_URL is not configured",
        impact="Broker and orchestrator cannot access the Postgres database",
        remediation="SQL_DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/database",
    )

    if sql_url:
        lowered = sql_url.lower()
        if "sqlite" in lowered:
            record_stage_check(stage_checks, STAGES.keys(), "Postgres backend in use", False, "SQLite DSN detected")
            add_issue(
                issues,
                severity="high",
                category="storage",
                message="SQL_DATABASE_URL points to SQLite",
                impact="Multi-process services require Postgres; SQLite will corrupt data under concurrency",
                remediation="SQL_DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/database",
                affects=tuple(STAGES.keys()),
            )
        else:
            record_stage_check(stage_checks, STAGES.keys(), "Postgres backend in use", True)

    redis_url = require_env(
        "REDIS_URL",
        stages=("core_infra", "broker_api", "orchestrator_dry_run", "orchestrator_live", "token_watcher"),
        severity="high",
        category="storage",
        message="REDIS_URL is not configured",
        impact="Rate limits, KV counters, and orchestrator coordination will fail",
        remediation="REDIS_URL=redis://redis:6379/0",
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
            record_stage_check(stage_checks, STAGES.keys(), "Venice base URL includes /api/v1", False, cleaned)
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
            record_stage_check(stage_checks, STAGES.keys(), "Venice base URL includes /api/v1", True)

    parent_key = require_env(
        "VENICE_PARENT_KEY",
        stages=tuple(STAGES.keys()),
        severity="critical",
        category="venice",
        message="VENICE_PARENT_KEY is missing or placeholder",
        impact="Broker cannot mint scoped API keys or revoke compromised keys",
        remediation="VENICE_PARENT_KEY=<parent-key-from-venice>",
    )

    api_key = require_env(
        "VENICE_API_KEY",
        stages=tuple(STAGES.keys()),
        severity="high",
        category="venice",
        message="VENICE_API_KEY is missing or placeholder",
        impact="Broker cannot proxy inference requests to Venice",
        remediation="VENICE_API_KEY=<parent-or-sub-key>",
    )

    # --- On-chain connectivity ---
    base_rpc = require_env(
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
        record_stage_check(stage_checks, ("orchestrator_dry_run", "orchestrator_live", "token_watcher"), "BASE_CHAIN_ID configured", True)
        try:
            parsed_id = int(base_chain_id, 10)
        except ValueError:
            record_stage_check(stage_checks, ("orchestrator_dry_run", "orchestrator_live", "token_watcher"), "Valid Base chain id", False, base_chain_id)
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
                record_stage_check(stage_checks, ("orchestrator_dry_run", "orchestrator_live", "token_watcher"), "Using Base mainnet chain id", False, base_chain_id)
                add_issue(
                    issues,
                    severity="medium",
                    category="onchain",
                    message=f"BASE_CHAIN_ID={parsed_id} does not match Base mainnet (8453)",
                    impact="Addresses baked into the bundle expect Base mainnet",
                    remediation="BASE_CHAIN_ID=8453",
                    affects=("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
                )
            else:
                record_stage_check(stage_checks, ("orchestrator_dry_run", "orchestrator_live", "token_watcher"), "Using Base mainnet chain id", True)
    else:
        record_stage_check(stage_checks, ("orchestrator_dry_run", "orchestrator_live", "token_watcher"), "BASE_CHAIN_ID configured", False, "missing")
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
        if env_key in {"VVV_TOKEN_ADDRESS", "QUOTE_TOKEN_ADDRESS"}:
            stages = ("broker_api", "orchestrator_dry_run", "orchestrator_live", "token_watcher")

        if not value:
            record_stage_check(stage_checks, stages, f"{env_key} configured", False, "missing")
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
            record_stage_check(stage_checks, stages, f"{env_key} valid address", False, value)
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
            record_stage_check(stage_checks, stages, f"{env_key} matches canonical Base mainnet", False, value)
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
            record_stage_check(stage_checks, stages, f"{env_key} matches canonical Base mainnet", True)

    # --- Broker feature toggles ---
    if not is_truthy(os.getenv("QUOTES_ENABLED")):
        record_stage_check(stage_checks, ("broker_api",), "Quote endpoint enabled", False)
        add_issue(
            issues,
            severity="high",
            category="features",
            message="QUOTES_ENABLED is false",
            impact="Buyers cannot preview capacity quotes",
            remediation="QUOTES_ENABLED=true",
            affects=("broker_api",),
        )
        suggestions.append({"key": "QUOTES_ENABLED", "value": "true", "reason": "Enable quote previews"})
    else:
        record_stage_check(stage_checks, ("broker_api",), "Quote endpoint enabled", True)

    if not is_truthy(os.getenv("PURCHASES_ENABLED")):
        record_stage_check(stage_checks, ("broker_api",), "Purchase endpoint enabled", False)
        add_issue(
            issues,
            severity="critical",
            category="features",
            message="PURCHASES_ENABLED is false",
            impact="Verification and fulfillment endpoints stay disabled",
            remediation="PURCHASES_ENABLED=true",
            affects=("broker_api",),
        )
        suggestions.append({"key": "PURCHASES_ENABLED", "value": "true", "reason": "Enable tenant verification flow"})
    else:
        record_stage_check(stage_checks, ("broker_api",), "Purchase endpoint enabled", True)

    cors_enabled = is_truthy(os.getenv("CORS_ENABLED"))
    cors_origins = env_value("CORS_ALLOW_ORIGINS")
    if cors_enabled and cors_origins:
        record_stage_check(stage_checks, ("broker_api",), "CORS configured", True)
    elif cors_enabled and not cors_origins:
        record_stage_check(stage_checks, ("broker_api",), "CORS configured", False, "no origins listed")
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
        record_stage_check(stage_checks, ("broker_api",), "CORS configured", False, "disabled")

    # --- Pricing routes ---
    trade_path = env_value("TRADE_PATH")
    if not trade_path:
        add_issue(
            issues,
            severity="medium",
            category="pricing",
            message="TRADE_PATH fallback is not defined",
            impact="DEX discovery must succeed every cycle; no static DIEM -> USDC path available",
            remediation="TRADE_PATH=0xf4d97f2da56e8c3098f3a8d538db630a2606a024@3000,0x4200000000000000000000000000000000000006@500,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            affects=("orchestrator_dry_run", "orchestrator_live", "token_watcher"),
        )
    else:
        record_stage_check(stage_checks, ("orchestrator_dry_run", "orchestrator_live", "token_watcher"), "Static DIEM trade path present", True)

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
    record_stage_check(stage_checks, ("orchestrator_live",), "ETH private key present", live_secret_ok, None if live_secret_ok else "missing")
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
        record_stage_check(stage_checks, ("orchestrator_live",), f"{env_key} configured", ok, None if ok else "missing")
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
    required_files = [
        (".env", ("core_infra",), "Base environment file expected for shared configuration"),
        (".env.docker", ("core_infra",), "Docker-specific overrides required for `docker compose --env-file .env.docker up`"),
        ("docker-compose.yml", ("core_infra",), "Docker Compose manifest required for standard `docker compose --env-file .env.docker up` run"),
        ("abi/diem.json", ("orchestrator_dry_run", "orchestrator_live", "token_watcher"), "DIEM ABI required for staking helpers"),
        ("libs/venice_sdk/client.py", ("broker_api",), "Venice SDK client missing"),
    ]
    for rel_path, stages, description in required_files:
        file_path = REPO_ROOT / rel_path
        if not file_path.exists():
            record_stage_check(stage_checks, stages, f"{rel_path} present", False, "missing")
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
            ("postgres", ("core_infra",), "critical", "docker-compose.yml lacks 'postgres' service"),
            ("redis", ("core_infra",), "critical", "docker-compose.yml lacks 'redis' service"),
            ("broker", ("core_infra", "broker_api"), "critical", "docker-compose.yml lacks 'broker' service"),
            ("orchestrator", ("orchestrator_dry_run", "orchestrator_live"), "high", "docker-compose.yml lacks 'orchestrator' service"),
            ("token-watcher", ("token_watcher",), "low", "docker-compose.yml lacks 'token-watcher' helper"),
        ]
        for service_name, stages, severity, message in compose_requirements:
            present = _compose_has_service(service_name)
            record_stage_check(stage_checks, stages, f"docker-compose service '{service_name}' defined", present, None if present else "missing")
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
    stage_status: Dict[str, Dict[str, Any]] = {}
    for stage_key, stage_label in STAGES.items():
        checks = stage_checks.get(stage_key, [])
        stage_issues = [issue for issue in issues if stage_key in issue.affects]
        blockers = [issue for issue in stage_issues if issue.severity in CRITICAL_SEVERITIES]
        warnings = [issue for issue in stage_issues if issue.severity not in CRITICAL_SEVERITIES]
        missing_checks = [check for check in checks if not check.ok]

        status = "ready"
        if blockers or any(check for check in missing_checks if check.detail in {"missing", "placeholder value detected"}):
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

    counts = {sev: 0 for sev in SEVERITY_ORDER}
    for issue in issues:
        counts[issue.severity] = counts.get(issue.severity, 0) + 1

    report = {
        "issues": [asdict(issue) for issue in issues],
        "suggestions": suggestions,
        "counts": counts,
        "stages": stage_status,
    }

    return report


def print_report(report: Dict[str, Any], export: bool = False) -> None:
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
        for note in stage.get("notes", []):
            print(f"  note: {note}")
        print()

    issues = report["issues"]
    counts = report["counts"]

    if not issues:
        print("✓ Environment configuration looks good!")
        return

    total = sum(counts.get(sev, 0) for sev in SEVERITY_ORDER)
    summary_parts = [f"{counts.get(sev, 0)} {sev}" for sev in SEVERITY_ORDER if counts.get(sev, 0)]
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

    counts: Dict[str, int] = report["counts"]
    if counts.get("critical", 0) > 0:
        sys.exit(2)
    if counts.get("high", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
