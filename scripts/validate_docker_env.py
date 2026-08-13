#!/usr/bin/env python3
"""
Validate Docker deployment environment configuration.

Checks for:
1. Required environment variables in docker/.env.local
2. Database configuration (SQL_DATABASE_URL vs POSTGRES_* vars)
3. Docker Compose service definitions
4. Secrets properly set vs placeholders
5. Missing or misaligned configuration
"""

import sys
from dataclasses import dataclass
from pathlib import Path

# Ensure repo root is importable
CURRENT_FILE = Path(__file__).resolve()
REPO_ROOT = CURRENT_FILE.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load environment helpers
try:
    from libs.env import load_dotenv_if_present  # type: ignore

    load_dotenv_if_present(path=str(REPO_ROOT / ".env"), override=False)
    docker_env = REPO_ROOT / "docker" / ".env.local"
    if docker_env.exists():
        load_dotenv_if_present(path=str(docker_env), override=True)
except Exception:
    pass


@dataclass
class ValidationIssue:
    """Represents a validation issue."""

    severity: str  # "critical", "high", "medium", "low"
    category: str  # "database", "secrets", "config", "docker"
    variable: str | None
    message: str
    expected: str | None = None
    actual: str | None = None
    remediation: str | None = None
    file_location: str | None = None


PLACEHOLDER_MARKERS = {
    "set-in-secrets",
    "xxxx",
    "<set-in-secrets>",
    "changeme",
    "placeholder",
    "todo",
}


def is_placeholder(value: str) -> bool:
    """Check if value is a placeholder."""
    if not value:
        return False
    val = value.strip().strip('"').strip("'")
    return any(marker.lower() in val.lower() for marker in PLACEHOLDER_MARKERS)


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


def check_docker_compose_services() -> list[ValidationIssue]:
    """Check docker-compose.yml has required services."""
    issues = []
    compose_path = REPO_ROOT / "docker-compose.yml"

    if not compose_path.exists():
        issues.append(
            ValidationIssue(
                severity="critical",
                category="docker",
                variable=None,
                message="docker-compose.yml not found",
                remediation="Create docker-compose.yml with required services",
                file_location="docker-compose.yml",
            )
        )
        return issues

    try:
        compose_text = compose_path.read_text()
    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="critical",
                category="docker",
                variable=None,
                message=f"Cannot read docker-compose.yml: {e}",
                remediation="Ensure docker-compose.yml is readable",
                file_location="docker-compose.yml",
            )
        )
        return issues

    required_services = {
        "postgres": ("critical", "PostgreSQL database service"),
        "redis": ("critical", "Redis KV store service"),
        "broker": ("critical", "Broker API service"),
        "orchestrator": ("high", "Orchestrator service"),
    }

    for service_name, (severity, description) in required_services.items():
        pattern = rf"^\s*{service_name}:\s*$"
        import re

        if not re.search(pattern, compose_text, re.MULTILINE):
            issues.append(
                ValidationIssue(
                    severity=severity,
                    category="docker",
                    variable=None,
                    message=f"docker-compose.yml missing '{service_name}' service",
                    expected=f"service: {service_name}",
                    remediation=f"Add '{service_name}' service definition to docker-compose.yml",
                    file_location="docker-compose.yml",
                )
            )

    return issues


def validate_docker_env() -> tuple[bool, list[ValidationIssue]]:
    """Validate Docker deployment configuration."""
    issues: list[ValidationIssue] = []

    # Check required files
    docker_env_local = REPO_ROOT / "docker" / ".env.local"
    env_file = REPO_ROOT / ".env"

    if not docker_env_local.exists():
        issues.append(
            ValidationIssue(
                severity="critical",
                category="config",
                variable=None,
                message="docker/.env.local not found",
                remediation="Create docker/.env.local with Docker-specific configuration",
                file_location="docker/.env.local",
            )
        )
        return False, issues

    # Parse environment files
    env_vars = parse_env_file(env_file)
    docker_vars = parse_env_file(docker_env_local)

    # Check for duplicates
    seen_in_docker = {}
    for var_name, (value, line_num) in docker_vars.items():
        if var_name in seen_in_docker:
            issues.append(
                ValidationIssue(
                    severity="medium",
                    category="config",
                    variable=var_name,
                    message=f"Duplicate variable '{var_name}' in docker/.env.local",
                    remediation=f"Remove duplicate at line {seen_in_docker[var_name]} or {line_num}",
                    file_location=f"docker/.env.local:{line_num}",
                )
            )
        else:
            seen_in_docker[var_name] = line_num

    # Database configuration checks
    sql_url_docker = docker_vars.get("SQL_DATABASE_URL", ("", 0))[0]

    # SQL_DATABASE_URL should be set in docker/.env.local, not .env
    if not sql_url_docker or is_placeholder(sql_url_docker):
        issues.append(
            ValidationIssue(
                severity="critical",
                category="database",
                variable="SQL_DATABASE_URL",
                message="SQL_DATABASE_URL not configured in docker/.env.local",
                expected="postgresql+psycopg2://user:pass@postgres:5432/database",
                actual=sql_url_docker if sql_url_docker else "missing",
                remediation="Set SQL_DATABASE_URL in docker/.env.local with full PostgreSQL connection string",
                file_location="docker/.env.local",
            )
        )
    elif "sqlite" in sql_url_docker.lower():
        issues.append(
            ValidationIssue(
                severity="high",
                category="database",
                variable="SQL_DATABASE_URL",
                message="SQL_DATABASE_URL points to SQLite (should be PostgreSQL)",
                expected="postgresql+psycopg2://...",
                actual="sqlite://...",
                remediation="Use PostgreSQL connection string: postgresql+psycopg2://postgres:postgres@postgres:5432/postgres",
                file_location="docker/.env.local",
            )
        )

    # POSTGRES_* vars should NOT be in docker/.env.local if SQL_DATABASE_URL is set
    postgres_vars = [
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    ]
    if sql_url_docker and not is_placeholder(sql_url_docker):
        for var_name in postgres_vars:
            if var_name in docker_vars:
                value, line_num = docker_vars[var_name]
                if value:  # Only warn if it has a value
                    issues.append(
                        ValidationIssue(
                            severity="low",
                            category="database",
                            variable=var_name,
                            message=f"{var_name} set in docker/.env.local but SQL_DATABASE_URL already contains connection info",
                            remediation=f"Remove {var_name} from docker/.env.local (SQL_DATABASE_URL is sufficient)",
                            file_location=f"docker/.env.local:{line_num}",
                        )
                    )

    # POSTGRES_* vars should be blank in .env (Docker-specific)
    for var_name in postgres_vars:
        if var_name in env_vars:
            value, line_num = env_vars[var_name]
            if value and not is_placeholder(value):
                issues.append(
                    ValidationIssue(
                        severity="medium",
                        category="database",
                        variable=var_name,
                        message=f"{var_name} should be blank in .env (Docker-specific, set in docker/.env.local if needed)",
                        expected="(blank)",
                        actual=value,
                        remediation=f"Set {var_name}= in .env and configure in docker/.env.local if needed",
                        file_location=f".env:{line_num}",
                    )
                )

    # Required Docker secrets
    required_docker_secrets = {
        "BROKER_ADMIN_TOKEN": ("critical", "Admin authentication token"),
        "VENICE_API_KEY": ("critical", "Venice API key for broker operations"),
        "VENICE_PARENT_KEY": ("critical", "Venice parent key for sub-key creation"),
        "VENICE_API_BASE_URL": ("critical", "Venice API base URL"),
    }

    # Check VENICE_API_BASE_URL format
    venice_base = (
        docker_vars.get("VENICE_API_BASE_URL", ("", 0))[0]
        or env_vars.get("VENICE_API_BASE_URL", ("", 0))[0]
    )
    if venice_base:
        if not venice_base.rstrip("/").endswith("/api/v1"):
            issues.append(
                ValidationIssue(
                    severity="high",
                    category="config",
                    variable="VENICE_API_BASE_URL",
                    message="VENICE_API_BASE_URL must include /api/v1",
                    expected="https://api.venice.ai/api/v1",
                    actual=venice_base,
                    remediation="Set VENICE_API_BASE_URL=https://api.venice.ai/api/v1",
                    file_location="docker/.env.local",
                )
            )

    for var_name, (severity, description) in required_docker_secrets.items():
        value = docker_vars.get(var_name, ("", 0))[0]
        if not value or is_placeholder(value):
            issues.append(
                ValidationIssue(
                    severity=severity,
                    category="secrets",
                    variable=var_name,
                    message=f"{var_name} missing or placeholder in docker/.env.local",
                    expected=f"<{description}>",
                    actual=value if value else "missing",
                    remediation=f"Set {var_name} in docker/.env.local with actual value",
                    file_location="docker/.env.local",
                )
            )

    # REDIS_URL should be set for Docker
    redis_url = docker_vars.get("REDIS_URL", ("", 0))[0]
    if not redis_url or is_placeholder(redis_url):
        issues.append(
            ValidationIssue(
                severity="critical",
                category="config",
                variable="REDIS_URL",
                message="REDIS_URL not configured in docker/.env.local",
                expected="redis://redis:6379/0",
                actual=redis_url if redis_url else "missing",
                remediation="Set REDIS_URL=redis://redis:6379/0 in docker/.env.local",
                file_location="docker/.env.local",
            )
        )

    # BASE_RPC_URL should be set (can be in .env or docker/.env.local)
    base_rpc = (
        docker_vars.get("BASE_RPC_URL", ("", 0))[0]
        or env_vars.get("BASE_RPC_URL", ("", 0))[0]
    )
    if not base_rpc or is_placeholder(base_rpc):
        issues.append(
            ValidationIssue(
                severity="high",
                category="config",
                variable="BASE_RPC_URL",
                message="BASE_RPC_URL not configured",
                expected="https://mainnet.base.org",
                actual=base_rpc if base_rpc else "missing",
                remediation="Set BASE_RPC_URL=https://mainnet.base.org in docker/.env.local or .env",
                file_location="docker/.env.local or .env",
            )
        )

    # BASE_CHAIN_ID should be 8453 (Base mainnet)
    base_chain_id = (
        docker_vars.get("BASE_CHAIN_ID", ("", 0))[0]
        or env_vars.get("BASE_CHAIN_ID", ("", 0))[0]
    )
    if base_chain_id:
        try:
            if int(base_chain_id) != 8453:
                issues.append(
                    ValidationIssue(
                        severity="medium",
                        category="config",
                        variable="BASE_CHAIN_ID",
                        message=f"BASE_CHAIN_ID={base_chain_id} does not match Base mainnet (8453)",
                        expected="8453",
                        actual=base_chain_id,
                        remediation="Set BASE_CHAIN_ID=8453",
                        file_location="docker/.env.local or .env",
                    )
                )
        except ValueError:
            issues.append(
                ValidationIssue(
                    severity="high",
                    category="config",
                    variable="BASE_CHAIN_ID",
                    message="BASE_CHAIN_ID is not an integer",
                    expected="8453",
                    actual=base_chain_id,
                    remediation="Set BASE_CHAIN_ID=8453",
                    file_location="docker/.env.local or .env",
                )
            )

    # Check docker-compose services
    compose_issues = check_docker_compose_services()
    issues.extend(compose_issues)

    # Check .env.docker exists (optional but recommended)
    env_docker = REPO_ROOT / ".env.docker"
    if not env_docker.exists():
        issues.append(
            ValidationIssue(
                severity="low",
                category="config",
                variable=None,
                message=".env.docker not found (optional but recommended)",
                remediation="Create .env.docker for docker compose --env-file .env.docker up",
                file_location=".env.docker",
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

    print("Docker Deployment Environment Validation")
    print("=" * 70)
    print()

    if not issues:
        print("OK: Docker environment configuration is valid")
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
    elif high_count > 0:
        print(f"WARNING: {high_count} high-priority issue(s) should be fixed")
    else:
        print("OK: No critical or high-priority issues")


def main() -> int:
    """Run Docker environment validation."""
    is_valid, issues = validate_docker_env()

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
