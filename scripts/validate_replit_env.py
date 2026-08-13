#!/usr/bin/env python3
"""
Validate Replit deployment environment configuration.

Checks for:
1. Database variables correctly marked as "set-in-secrets" vs blank in .env
2. Replit Secrets configuration requirements
3. Docker-specific vars are blank in .env (should be in docker/.env.local)
4. Replit auto-provided vars are blank
5. Missing or misaligned configuration
"""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

# Ensure repo root is importable
CURRENT_FILE = Path(__file__).resolve()
REPO_ROOT = CURRENT_FILE.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load environment helpers
try:
    from libs.env import load_dotenv_if_present  # type: ignore

    load_dotenv_if_present(path=str(REPO_ROOT / ".env"), override=False)
except Exception:
    pass


@dataclass
class ValidationIssue:
    """Represents a validation issue."""

    severity: str  # "critical", "high", "medium", "low"
    category: str  # "database", "secrets", "config", "replit"
    variable: str | None
    message: str
    expected: str | None = None
    actual: str | None = None
    remediation: str | None = None
    file_location: str | None = None
    secrets_location: str | None = None


# Database variables that should be "set-in-secrets" for Replit
REPLIT_SECRETS_DB_VARS = {
    "SQL_DATABASE_URL",
    "SQL_DATABASE_URL_READONLY",
}

# Database variables that are Docker-specific and should be blank in .env
DOCKER_SPECIFIC_DB_VARS = {
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
}

# Variables that Replit provides automatically (should be blank in .env)
REPLIT_AUTO_PROVIDED = {
    "DATABASE_URL",  # Replit provides this when SQL database is added
    # Replit SQL database environment helpers
    "PGHOST",
    "PGUSER",
    "PGPASSWORD",
    "PGDATABASE",
    "PGPORT",
}

# Placeholder markers that indicate "set-in-secrets"
PLACEHOLDER_MARKERS = {
    "set-in-secrets",
    "xxxx",
    "<set-in-secrets>",
    "changeme",
    "placeholder",
    "todo",
}

# Required Replit Secrets (must be set in Secrets Manager)
REQUIRED_REPLIT_SECRETS = {
    "SQL_DATABASE_URL": (
        "critical",
        "PostgreSQL connection string from Replit Database tool",
    ),
    "VENICE_API_KEY": ("critical", "Venice API key for broker operations"),
    "VENICE_PARENT_KEY": ("critical", "Venice parent key for sub-key creation"),
    "BROKER_ADMIN_TOKEN": ("critical", "Admin authentication token"),
}

# Required config vars (can be in .env, not secrets)
REQUIRED_REPLIT_CONFIG = {
    "VENICE_API_BASE_URL": ("critical", "Venice API base URL (must include /api/v1)"),
    "BASE_RPC_URL": ("high", "Base RPC endpoint"),
    "DIEM_TOKEN_ADDRESS": ("high", "DIEM token contract address on Base"),
    "VVV_TOKEN_ADDRESS": ("high", "VVV token contract address on Base"),
    "DIEM_VVV_PAIR_ADDRESS": ("high", "DIEM/VVV pair address for bridge pricing"),
    "VVV_USDC_POOL_ADDRESS": ("high", "VVV/USDC pool address for bridge pricing"),
    "QUOTE_TOKEN_ADDRESS": ("high", "Quote token address (USDC) for pricing"),
    "BASE_CHAIN_ID": ("high", "Base chain ID (should be 8453)"),
}

# Optional but recommended Replit Secrets
OPTIONAL_REPLIT_SECRETS = {
    "SQL_DATABASE_URL_READONLY": (
        "medium",
        "Read-only PostgreSQL connection string (if using read replicas)",
    ),
    "ETH_PRIVATE_KEY": (
        "high",
        "Private key for live trading (if running orchestrator)",
    ),
    "CDP_API_KEY_ID": ("high", "Coinbase Cloud API key ID (if using smart wallet)"),
    "CDP_API_KEY_SECRET": (
        "high",
        "Coinbase Cloud API key secret (if using smart wallet)",
    ),
    "CDP_WALLET_SECRET": (
        "high",
        "Coinbase Cloud wallet secret (if using smart wallet)",
    ),
}


def is_placeholder(value: str) -> bool:
    """Check if value is a placeholder indicating it should be set in secrets."""
    if not value:
        return False
    val = value.strip().strip('"').strip("'")
    return any(marker.lower() in val.lower() for marker in PLACEHOLDER_MARKERS)


def is_truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_env_file(filepath: Path) -> dict[str, tuple[str, int]]:
    """Parse .env file and return dict of var_name -> (value, line_number)."""
    vars_dict = {}
    if not filepath.exists():
        return vars_dict

    with open(filepath, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                parts = line.split("=", 1)
                var_name = parts[0].strip()
                value = parts[1].strip() if len(parts) > 1 else ""
                vars_dict[var_name] = (value, line_num)

    return vars_dict


def _parse_dex_providers(raw: str) -> list[dict[str, str]]:
    """Parse DEX_PROVIDERS into a list of provider specs matching runtime parsing."""
    value = (raw or "").strip()
    if not value:
        return []
    if value[0] in "[{":
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            parsed = [parsed]
        if isinstance(parsed, list):
            out: list[dict[str, str]] = []
            for item in parsed:
                if isinstance(item, dict):
                    out.append({k: str(v) for k, v in item.items()})
                else:
                    out.append({"name": str(item)})
            return out
    return [{"name": part.strip()} for part in value.split(",") if part.strip()]


def _is_placeholder_replit_kv_url(url: str) -> bool:
    """Detect Replit KV URLs that are missing the signed token segment."""
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


def validate_replit_env() -> tuple[bool, list[ValidationIssue]]:
    """Validate Replit deployment configuration."""
    issues: list[ValidationIssue] = []

    env_file = REPO_ROOT / ".env"
    docker_env = REPO_ROOT / "docker" / ".env.local"

    if not env_file.exists():
        issues.append(
            ValidationIssue(
                severity="critical",
                category="config",
                variable=None,
                message=".env file not found",
                remediation="Create .env file with base configuration",
                file_location=".env",
            )
        )
        return False, issues

    env_vars = parse_env_file(env_file)
    docker_vars = parse_env_file(docker_env) if docker_env.exists() else {}

    # Check for duplicates
    seen_vars = {}
    for var_name, (value, line_num) in env_vars.items():
        if var_name in seen_vars:
            issues.append(
                ValidationIssue(
                    severity="medium",
                    category="config",
                    variable=var_name,
                    message=f"Duplicate variable '{var_name}' in .env",
                    remediation=f"Remove duplicate at line {seen_vars[var_name]} or {line_num}",
                    file_location=f".env:{line_num}",
                )
            )
        else:
            seen_vars[var_name] = line_num

    # Validate Replit secrets markers in .env
    for var_name in REPLIT_SECRETS_DB_VARS:
        if var_name not in env_vars:
            if var_name == "SQL_DATABASE_URL":
                issues.append(
                    ValidationIssue(
                        severity="critical",
                        category="secrets",
                        variable=var_name,
                        message=f"{var_name} not found in .env",
                        expected='"set-in-secrets"',
                        actual="missing",
                        remediation=f'Add {var_name}="set-in-secrets" to .env and set actual value in Replit Secrets',
                        file_location=".env",
                        secrets_location="Replit Secrets Manager",
                    )
                )
            continue

        value, line_num = env_vars[var_name]
        if not is_placeholder(value) and value:
            issues.append(
                ValidationIssue(
                    severity="high",
                    category="secrets",
                    variable=var_name,
                    message=f"{var_name} should be 'set-in-secrets' in .env (not actual value)",
                    expected='"set-in-secrets"',
                    actual=value[:50] + "..." if len(value) > 50 else value,
                    remediation=f"Change {var_name} to 'set-in-secrets' in .env and set actual value in Replit Secrets",
                    file_location=f".env:{line_num}",
                    secrets_location="Replit Secrets Manager",
                )
            )
        elif not value and var_name == "SQL_DATABASE_URL":
            issues.append(
                ValidationIssue(
                    severity="critical",
                    category="secrets",
                    variable=var_name,
                    message=f"{var_name} is blank in .env (should be 'set-in-secrets')",
                    expected='"set-in-secrets"',
                    actual="(blank)",
                    remediation=f'Set {var_name}="set-in-secrets" in .env and configure in Replit Secrets',
                    file_location=f".env:{line_num}",
                    secrets_location="Replit Secrets Manager",
                )
            )

    # Validate Docker-specific vars are blank in .env
    for var_name in DOCKER_SPECIFIC_DB_VARS:
        if var_name not in env_vars:
            continue

        value, line_num = env_vars[var_name]
        if value and not is_placeholder(value):
            # Check if it's set in docker/.env.local
            if var_name in docker_vars:
                issues.append(
                    ValidationIssue(
                        severity="medium",
                        category="config",
                        variable=var_name,
                        message=f"{var_name} should be blank in .env (Docker-specific, already in docker/.env.local)",
                        expected="(blank)",
                        actual=value,
                        remediation=f"Set {var_name}= in .env (keep in docker/.env.local for Docker)",
                        file_location=f".env:{line_num}",
                    )
                )
            else:
                issues.append(
                    ValidationIssue(
                        severity="low",
                        category="config",
                        variable=var_name,
                        message=f"{var_name} has value in .env (should be blank, Docker-specific)",
                        expected="(blank)",
                        actual=value,
                        remediation=f"Set {var_name}= in .env (configure in docker/.env.local for Docker if needed)",
                        file_location=f".env:{line_num}",
                    )
                )

    # Validate Replit auto-provided vars are blank
    for var_name in REPLIT_AUTO_PROVIDED:
        if var_name not in env_vars:
            continue

        value, line_num = env_vars[var_name]
        if value and not is_placeholder(value):
            issues.append(
                ValidationIssue(
                    severity="low",
                    category="config",
                    variable=var_name,
                    message=f"{var_name} has value in .env (Replit provides this automatically)",
                    expected="(blank)",
                    actual=value,
                    remediation=f"Set {var_name}= in .env (Replit provides this when SQL database is added)",
                    file_location=f".env:{line_num}",
                )
            )

    # Check REPLIT_DB_URL (KV store)
    replit_db_url = os.getenv("REPLIT_DB_URL", "")
    if replit_db_url:
        if _is_placeholder_replit_kv_url(replit_db_url):
            issues.append(
                ValidationIssue(
                    severity="high",
                    category="secrets",
                    variable="REPLIT_DB_URL",
                    message="REPLIT_DB_URL is a placeholder (missing /v0/<token>)",
                    expected="https://kv.replit.com/v0/<token>",
                    actual=(
                        replit_db_url[:50] + "..."
                        if len(replit_db_url) > 50
                        else replit_db_url
                    ),
                    remediation="Set REPLIT_DB_URL in Replit Secrets to full value from `echo $REPLIT_DB_URL`",
                    secrets_location="Replit Secrets Manager",
                )
            )
    else:
        # Check if REDIS_URL is set as alternative
        redis_url = env_vars.get("REDIS_URL", ("", 0))[0]
        if not redis_url or is_placeholder(redis_url):
            issues.append(
                ValidationIssue(
                    severity="medium",
                    category="config",
                    variable="REPLIT_DB_URL",
                    message="REPLIT_DB_URL not set and REDIS_URL not configured",
                    expected="REPLIT_DB_URL in Secrets or REDIS_URL configured",
                    actual="missing",
                    remediation="Set REPLIT_DB_URL in Replit Secrets or configure REDIS_URL",
                    secrets_location="Replit Secrets Manager",
                )
            )

    # Validate required secrets are marked correctly
    for var_name, (severity, description) in REQUIRED_REPLIT_SECRETS.items():
        if var_name in REPLIT_SECRETS_DB_VARS:
            continue  # Already checked above

        if var_name not in env_vars:
            issues.append(
                ValidationIssue(
                    severity=severity,
                    category="secrets",
                    variable=var_name,
                    message=f"{var_name} not found in .env",
                    expected='"set-in-secrets" or actual value',
                    actual="missing",
                    remediation=f"Add {var_name} to .env (mark as 'set-in-secrets' or set actual value) and configure in Replit Secrets",
                    file_location=".env",
                    secrets_location="Replit Secrets Manager",
                )
            )
            continue

        value, line_num = env_vars[var_name]
        if not value or is_placeholder(value):
            if not is_placeholder(value):
                issues.append(
                    ValidationIssue(
                        severity=severity,
                        category="secrets",
                        variable=var_name,
                        message=f"{var_name} is blank or placeholder in .env",
                        expected="Actual value or 'set-in-secrets'",
                        actual=value if value else "(blank)",
                        remediation=f"Set {var_name} in Replit Secrets Manager ({description})",
                        file_location=f".env:{line_num}",
                        secrets_location="Replit Secrets Manager",
                    )
                )

    # Note: docker/.env.local is Docker-specific and not validated here
    # Use validate_docker_env.py for Docker configuration validation

    # Warn on DIEM_FAKE_* overrides in Replit (these force synthetic pricing in dry-run)
    fake_price_env = os.getenv("DIEM_FAKE_PRICE") or os.getenv("TEST_DIEM_PRICE")
    if fake_price_env:
        issues.append(
            ValidationIssue(
                severity="medium",
                category="config",
                variable="DIEM_FAKE_PRICE",
                message="DIEM_FAKE_PRICE/TEST_DIEM_PRICE is set; dry-run will use a synthetic DIEM price instead of live market data",
                expected="(unset for Replit deployments; reserve for offline simulations)",
                actual=fake_price_env,
                remediation="Unset DIEM_FAKE_PRICE and TEST_DIEM_PRICE in Replit for realistic DIEM pricing; use them only for local/offline tests.",
            )
        )
    fake_mint_rate_env = os.getenv("DIEM_FAKE_MINT_RATE")
    if fake_mint_rate_env:
        issues.append(
            ValidationIssue(
                severity="medium",
                category="config",
                variable="DIEM_FAKE_MINT_RATE",
                message="DIEM_FAKE_MINT_RATE is set; dry-run will use a synthetic DIEM mint rate",
                expected="(unset for Replit deployments; reserve for offline simulations)",
                actual=fake_mint_rate_env,
                remediation="Unset DIEM_FAKE_MINT_RATE in Replit unless you are intentionally simulating a custom mint curve.",
            )
        )

    # Validate required config vars
    for var_name, (severity, description) in REQUIRED_REPLIT_CONFIG.items():
        if var_name not in env_vars:
            issues.append(
                ValidationIssue(
                    severity=severity,
                    category="config",
                    variable=var_name,
                    message=f"{var_name} not found in .env",
                    expected=description,
                    actual="missing",
                    remediation=f"Add {var_name} to .env ({description})",
                    file_location=".env",
                )
            )
            continue

        value, line_num = env_vars[var_name]
        if not value or is_placeholder(value):
            issues.append(
                ValidationIssue(
                    severity=severity,
                    category="config",
                    variable=var_name,
                    message=f"{var_name} is blank or placeholder in .env",
                    expected=description,
                    actual=value if value else "(blank)",
                    remediation=f"Set {var_name} in .env ({description})",
                    file_location=f".env:{line_num}",
                )
            )

        # Special validation for VENICE_API_BASE_URL
        if var_name == "VENICE_API_BASE_URL" and value:
            if not value.rstrip("/").endswith("/api/v1"):
                issues.append(
                    ValidationIssue(
                        severity="high",
                        category="config",
                        variable=var_name,
                        message="VENICE_API_BASE_URL must include /api/v1",
                        expected="https://api.venice.ai/api/v1",
                        actual=value,
                        remediation="Set VENICE_API_BASE_URL=https://api.venice.ai/api/v1",
                        file_location=f".env:{line_num}",
                    )
                )

        # Special validation for BASE_CHAIN_ID
        if var_name == "BASE_CHAIN_ID" and value:
            try:
                chain_id = int(value)
                if chain_id != 8453:
                    issues.append(
                        ValidationIssue(
                            severity="medium",
                            category="config",
                            variable=var_name,
                            message=f"BASE_CHAIN_ID={chain_id} does not match Base mainnet (8453)",
                            expected="8453",
                            actual=value,
                            remediation="Set BASE_CHAIN_ID=8453",
                            file_location=f".env:{line_num}",
                        )
                    )
            except ValueError:
                issues.append(
                    ValidationIssue(
                        severity="high",
                        category="config",
                        variable=var_name,
                        message="BASE_CHAIN_ID is not an integer",
                        expected="8453",
                        actual=value,
                        remediation="Set BASE_CHAIN_ID=8453",
                        file_location=f".env:{line_num}",
                    )
                )

    # Validate DEX / DIEM routing basics so we fail fast when quotes/trades cannot execute.
    # Use the *runtime* environment as the source of truth (Replit Secrets + .env), while
    # still pointing back to .env for remediation hints.
    dex_file_val, dex_file_line = env_vars.get("DEX_PROVIDERS", ("", 0))
    dex_runtime_val = (os.getenv("DEX_PROVIDERS") or dex_file_val or "").strip()
    if not dex_runtime_val or is_placeholder(dex_runtime_val):
        issues.append(
            ValidationIssue(
                severity="high",
                category="config",
                variable="DEX_PROVIDERS",
                message=(
                    "DEX_PROVIDERS is not configured; the DEX aggregator will not be wired "
                    "for DIEM pricing, quotes, or ArbiDiem execution"
                ),
                expected="uniswap_v2,aerodrome,uniswap_v3 or JSON provider spec (see DEX Configuration in docs/CONFIGURATION.md)",
                actual=dex_runtime_val if dex_runtime_val else "(blank)",
                remediation=(
                    "Set DEX_PROVIDERS either in .env (preferred for baseline) or directly in "
                    "the Replit environment. A typical value is 'uniswap_v2,aerodrome,uniswap_v3' "
                    "as documented in docs/CONFIGURATION.md."
                ),
                file_location=f".env:{dex_file_line}"
                if dex_file_line
                else ".env or Replit env",
            )
        )
    else:
        # Check that required router/quoter addresses are set for each configured provider.
        # This catches the case where DEX_PROVIDERS is set but router addresses are missing,
        # which causes silent failures at runtime (providers return None, quotes fail).
        provider_specs = _parse_dex_providers(dex_runtime_val)
        missing_router_vars: list[tuple[str, str, str, str | None]] = []

        for spec in provider_specs:
            provider_name = str(spec.get("name", "")).strip().lower()
            if not provider_name:
                continue
            if provider_name == "uniswap_v2":
                v2_router = (
                    spec.get("router")
                    or os.getenv("UNISWAP_V2_ROUTER_ADDRESS")
                    or os.getenv("ROUTER_ADDRESS")
                )
                if not v2_router or is_placeholder(str(v2_router)):
                    missing_router_vars.append(
                        (
                            "UNISWAP_V2_ROUTER_ADDRESS",
                            provider_name,
                            "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",  # Base mainnet UniswapV2 router
                            "ROUTER_ADDRESS",
                        )
                    )
            elif provider_name == "uniswap_v3":
                v3_router = spec.get("router") or os.getenv("UNISWAP_V3_ROUTER_ADDRESS")
                v3_quoter = spec.get("quoter") or os.getenv("UNISWAP_V3_QUOTER_ADDRESS")
                if not v3_router or is_placeholder(str(v3_router)):
                    missing_router_vars.append(
                        (
                            "UNISWAP_V3_ROUTER_ADDRESS",
                            provider_name,
                            "0x2626664c2603336E57B271c5C0b26F421741e481",  # Base mainnet SwapRouter02
                            None,
                        )
                    )
                if not v3_quoter or is_placeholder(str(v3_quoter)):
                    missing_router_vars.append(
                        (
                            "UNISWAP_V3_QUOTER_ADDRESS",
                            provider_name,
                            "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",  # Base mainnet QuoterV2
                            None,
                        )
                    )
            elif provider_name == "aerodrome":
                aero_router = spec.get("router") or os.getenv(
                    "AERODROME_ROUTER_ADDRESS"
                )
                if not aero_router or is_placeholder(str(aero_router)):
                    missing_router_vars.append(
                        (
                            "AERODROME_ROUTER_ADDRESS",
                            provider_name,
                            "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43",  # Base mainnet Aerodrome router
                            None,
                        )
                    )

        for var_name, provider_name, example_addr, fallback in missing_router_vars:
            fallback_clause = f" or {fallback}" if fallback else ""
            issues.append(
                ValidationIssue(
                    severity="high",
                    category="config",
                    variable=var_name,
                    message=(
                        f"{var_name} is not set but '{provider_name}' is in DEX_PROVIDERS; "
                        f"the {provider_name} provider will be skipped and quotes will fail"
                    ),
                    expected=f"Base mainnet address, e.g. {example_addr}",
                    actual="(not set)",
                    remediation=(
                        f"Set {var_name}{fallback_clause} in Replit Secrets or .env. "
                        f"For Base mainnet, use: {var_name}={example_addr}"
                    ),
                    file_location=".env or Replit Secrets",
                )
            )

        aggregator = None
        # Attempt to build the DEX aggregator from env. This mirrors runtime behaviour and
        # catches missing router/quoter addresses that would later cause quote failures.
        try:
            from libs.dex.providers import build_aggregator_from_env  # type: ignore

            aggregator = build_aggregator_from_env()
            # Verify the aggregator actually has providers (not just an empty shell)
            provider_count = len(getattr(aggregator, "providers", []))
            if provider_count == 0:
                issues.append(
                    ValidationIssue(
                        severity="high",
                        category="config",
                        variable="DEX_PROVIDERS",
                        message=(
                            "DEX aggregator was built but has 0 active providers; "
                            "all configured providers failed to initialize (likely missing router addresses)"
                        ),
                        expected="At least 1 active DEX provider",
                        actual=f"0 providers (DEX_PROVIDERS={dex_runtime_val})",
                        remediation=(
                            "Check the router address variables above. Each provider in DEX_PROVIDERS "
                            "requires its corresponding router/quoter address to be set."
                        ),
                    )
                )
            else:
                exec_file_val, exec_file_line = env_vars.get(
                    "DEX_EXEC_PROVIDERS", ("", 0)
                )
                exec_runtime_val = (
                    os.getenv("DEX_EXEC_PROVIDERS") or exec_file_val or ""
                ).strip()
                exec_env_provided = bool(exec_runtime_val)
                if exec_env_provided:
                    parse_default = getattr(aggregator, "_discovery_providers", set())
                    try:
                        requested_exec = aggregator._parse_provider_list_raw(  # type: ignore[attr-defined]
                            exec_runtime_val, parse_default
                        )
                    except Exception:
                        requested_exec = set()
                    provider_map = getattr(aggregator, "_provider_name_map", {})
                    canonical_requested = {
                        provider_map.get(name, name) for name in requested_exec
                    }
                    effective_exec = set(
                        getattr(aggregator, "_execution_provider_names", [])
                    )
                    unknown_exec = sorted(
                        name for name in requested_exec if name not in provider_map
                    )

                    if not requested_exec:
                        issues.append(
                            ValidationIssue(
                                severity="high",
                                category="config",
                                variable="DEX_EXEC_PROVIDERS",
                                message=(
                                    "DEX_EXEC_PROVIDERS is set but parsed to an empty list; "
                                    "no DEX venues will be eligible for execution"
                                ),
                                expected=(
                                    "Comma-separated or JSON list of providers present in DEX_PROVIDERS"
                                ),
                                actual=exec_runtime_val or "(blank)",
                                remediation=(
                                    "Set DEX_EXEC_PROVIDERS to a subset of configured providers, "
                                    "e.g. uniswap_v3,uniswap_v2"
                                ),
                                file_location=f".env:{exec_file_line}"
                                if exec_file_line
                                else ".env or Replit env",
                            )
                        )
                    elif canonical_requested != effective_exec:
                        issues.append(
                            ValidationIssue(
                                severity="high",
                                category="config",
                                variable="DEX_EXEC_PROVIDERS",
                                message=(
                                    "Configured execution providers do not match aggregator state "
                                    f"(requested {sorted(canonical_requested)} vs effective {sorted(effective_exec)})"
                                ),
                                expected=(
                                    "Execution providers match DEX_EXEC_PROVIDERS after filtering to DEX_DISCOVERY_PROVIDERS"
                                ),
                                actual=f"effective={sorted(effective_exec)} requested={sorted(canonical_requested)}",
                                remediation=(
                                    "Ensure DEX_EXEC_PROVIDERS only lists providers present in DEX_PROVIDERS "
                                    "and uses commas or JSON list formatting"
                                ),
                                file_location=f".env:{exec_file_line}"
                                if exec_file_line
                                else ".env or Replit env",
                            )
                        )
                    if unknown_exec:
                        issues.append(
                            ValidationIssue(
                                severity="medium",
                                category="config",
                                variable="DEX_EXEC_PROVIDERS",
                                message=(
                                    "Unknown execution providers in DEX_EXEC_PROVIDERS: "
                                    + ", ".join(unknown_exec)
                                ),
                                expected="Names matching configured DEX providers",
                                actual=exec_runtime_val or "(blank)",
                                remediation=(
                                    "Limit DEX_EXEC_PROVIDERS to providers declared in DEX_PROVIDERS "
                                    "(e.g. uniswap_v2,aerodrome,uniswap_v3)"
                                ),
                                file_location=f".env:{exec_file_line}"
                                if exec_file_line
                                else ".env or Replit env",
                            )
                        )
        except OSError as exc:
            issues.append(
                ValidationIssue(
                    severity="high",
                    category="config",
                    variable="DEX_PROVIDERS",
                    message=(
                        "DEX aggregator could not be constructed from environment variables "
                        f"(likely missing router/quoter addresses): {exc}"
                    ),
                    expected=(
                        "DEX_PROVIDERS plus UNISWAP_V2_ROUTER_ADDRESS, "
                        "AERODROME_ROUTER_ADDRESS, and UNISWAP_V3_ROUTER_ADDRESS/"
                        "UNISWAP_V3_QUOTER_ADDRESS where applicable"
                    ),
                    actual=dex_runtime_val,
                    remediation=(
                        "Ensure all router and quoter env vars required by the configured "
                        "DEX_PROVIDERS are set. Use scripts/validate_dex_config.py and the "
                        "DEX Configuration section in docs/CONFIGURATION.md as references."
                    ),
                    file_location=f".env:{dex_file_line}"
                    if dex_file_line
                    else ".env or Replit env",
                )
            )
        except Exception as exc:
            issues.append(
                ValidationIssue(
                    severity="medium",
                    category="config",
                    variable="DEX_PROVIDERS",
                    message=f"Unexpected error while validating DEX aggregator configuration: {exc}",
                    remediation=(
                        "Run scripts/validate_dex_config.py for detailed DEX diagnostics and "
                        "confirm BASE_RPC_URL/ABI files are available."
                    ),
                )
            )

    # Validate DIEM/VVV pair address for bridge pricing fallback
    diem_vvv_pair = os.getenv("DIEM_VVV_PAIR_ADDRESS")
    if not diem_vvv_pair:
        issues.append(
            ValidationIssue(
                severity="high",
                category="config",
                variable="DIEM_VVV_PAIR_ADDRESS",
                message=(
                    "DIEM_VVV_PAIR_ADDRESS is not set; bridge_vvv pricing fallback will fail "
                    "and DIEM price discovery will be unreliable"
                ),
                expected="0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d (Base mainnet DIEM/VVV pair)",
                actual="(not set)",
                remediation=(
                    "Set DIEM_VVV_PAIR_ADDRESS=0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d "
                    "in .env or Replit Secrets"
                ),
                file_location=".env or Replit Secrets",
            )
        )

    # Validate DIEM trade paths unless dynamic discovery is enabled.
    trade_paths_dynamic = env_vars.get("TRADE_PATHS_DYNAMIC", ("", 0))[0] or os.getenv(
        "TRADE_PATHS_DYNAMIC", ""
    )
    if not is_truthy(trade_paths_dynamic):
        for var_name, (severity, description) in {
            "TRADE_PATH": (
                "high",
                "Sell-direction DIEM trade path (DIEM -> ... -> USDC) used for routing and quotes",
            ),
            "TRADE_PATH_BUY": (
                "medium",
                "Buy-direction DIEM trade path (USDC -> ... -> DIEM); recommended for orchestrator",
            ),
        }.items():
            value, line_num = env_vars.get(var_name, ("", 0))
            if not value or is_placeholder(value):
                issues.append(
                    ValidationIssue(
                        severity=severity,
                        category="config",
                        variable=var_name,
                        message=f"{var_name} is blank or placeholder in .env",
                        expected=description,
                        actual=value if value else "(blank)",
                        remediation=(
                            f"Set {var_name} in .env based on .env.example "
                            "so DIEM routing matches on-chain liquidity."
                        ),
                        file_location=f".env:{line_num}" if line_num else ".env",
                    )
                )

    # Check for SQL_CREATE_ALL_ON_START (should use Alembic instead)
    sql_create_all = env_vars.get("SQL_CREATE_ALL_ON_START", ("", 0))[0]
    if sql_create_all and sql_create_all.lower() in ("1", "true", "yes"):
        issues.append(
            ValidationIssue(
                severity="medium",
                category="config",
                variable="SQL_CREATE_ALL_ON_START",
                message="SQL_CREATE_ALL_ON_START should not be used in Replit production",
                expected="false or unset",
                actual=sql_create_all,
                remediation="Remove SQL_CREATE_ALL_ON_START and use 'alembic upgrade head' in replit_prestart.sh",
                file_location=".env",
            )
        )

    # Sort issues by severity
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    issues.sort(key=lambda x: (severity_order.get(x.severity, 99), x.variable or ""))

    return len([i for i in issues if i.severity in ("critical", "high")]) == 0, issues


def print_report(issues: list[ValidationIssue]) -> None:
    """Print validation report."""
    import io
    import sys

    # Fix Windows console encoding
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

    print("Replit Deployment Environment Validation")
    print("=" * 70)
    print()

    if not issues:
        print("OK: Replit environment configuration is valid")
        print("\nNext steps:")
        print("1. Ensure all required secrets are set in Replit Secrets Manager")
        print("2. Verify SQL_DATABASE_URL points to production PostgreSQL database")
        print("3. Run 'alembic upgrade head' via replit_prestart.sh")
        return

    # Group by severity
    by_severity: dict[str, list[ValidationIssue]] = {}
    for issue in issues:
        by_severity.setdefault(issue.severity, []).append(issue)

    severity_order = ["critical", "high", "medium", "low"]

    for severity in severity_order:
        if severity not in by_severity:
            continue

        print(f"{severity.upper()} ISSUES ({len(by_severity[severity])}):")
        print("-" * 70)

        for idx, issue in enumerate(by_severity[severity], 1):
            print(f"\n{idx}. [{issue.category.upper()}] {issue.message}")

            if issue.variable:
                print(f"   Variable: {issue.variable}")

            if issue.expected and issue.actual:
                print(f"   Expected: {issue.expected}")
                print(f"   Actual:   {issue.actual}")
            elif issue.expected:
                print(f"   Expected: {issue.expected}")
            elif issue.actual:
                print(f"   Actual:   {issue.actual}")

            if issue.file_location:
                print(f"   Location: {issue.file_location}")

            if issue.secrets_location:
                print(f"   Set in:   {issue.secrets_location}")

            if issue.remediation:
                print(f"   Fix:      {issue.remediation}")

        print()

    # Summary
    critical_count = len(by_severity.get("critical", []))
    high_count = len(by_severity.get("high", []))

    if critical_count > 0:
        print(
            f"ERROR: {critical_count} critical issue(s) must be fixed before deployment"
        )
        print("\nRequired actions:")
        print("1. Fix all critical issues above")
        print("2. Set required secrets in Replit Secrets Manager")
        print("3. Verify database connection string is correct")
    elif high_count > 0:
        print(f"WARNING: {high_count} high-priority issue(s) should be fixed")
    else:
        print("OK: No critical or high-priority issues")

    # Replit Secrets setup reminder
    if critical_count > 0 or high_count > 0:
        print("\n" + "=" * 70)
        print("REPLIT SECRETS SETUP:")
        print("=" * 70)
        print("1. Open Replit workspace")
        print("2. Click Secrets icon (🔒) in left sidebar")
        print("3. Add required secrets:")
        for var_name, (severity, description) in REQUIRED_REPLIT_SECRETS.items():
            if severity == "critical":
                print(f"   - {var_name}: {description}")
        print("\nSee docs/REPLIT_ENV_VALIDATION.md for detailed instructions")


def main() -> int:
    """Run Replit environment validation."""
    is_valid, issues = validate_replit_env()

    print_report(issues)

    critical_count = len([i for i in issues if i.severity == "critical"])
    high_count = len([i for i in issues if i.severity == "high"])

    if critical_count > 0:
        return 2
    if high_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
