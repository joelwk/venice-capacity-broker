#!/usr/bin/env python3
"""
Diagnostic script to test Venice API connectivity and API key propagation from Docker containers.

Run this inside a Docker container to diagnose networking and API key issues.
"""

import os
import sys

import requests


def test_env_var(name: str) -> tuple[bool, str | None]:
    """Check if environment variable exists and return its value."""
    value = os.getenv(name)
    exists = value is not None and value.strip() != ""
    return exists, value


def test_dns_resolution(hostname: str) -> bool:
    """Test DNS resolution for a hostname."""
    try:
        import socket

        socket.gethostbyname(hostname)
        return True
    except Exception:
        return False


def test_http_connectivity(url: str, timeout: int = 10) -> tuple[bool, str | None]:
    """Test HTTP connectivity to a URL."""
    try:
        response = requests.get(url, timeout=timeout)
        return True, f"HTTP {response.status_code}"
    except requests.exceptions.Timeout:
        return False, "Timeout"
    except requests.exceptions.ConnectionError as e:
        return False, f"Connection error: {e}"
    except Exception as e:
        return False, f"Error: {e}"


def test_venice_api_models(
    base_url: str, api_key: str | None
) -> tuple[bool, str | None]:
    """Test Venice API /models endpoint."""
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return True, f"Success: {len(response.json().get('data', []))} models"
        if response.status_code == 401:
            return False, "401 Unauthorized - API key invalid or missing"
        if response.status_code == 404:
            return False, f"404 Not Found - URL: {url}"
        return False, f"HTTP {response.status_code}: {response.text[:200]}"
    except requests.exceptions.Timeout:
        return False, "Timeout"
    except requests.exceptions.ConnectionError as e:
        return False, f"Connection error: {e}"
    except Exception as e:
        return False, f"Error: {e}"


def test_venice_api_chat(
    base_url: str, api_key: str | None, model: str = "qwen3-4b"
) -> tuple[bool, str | None]:
    """Test Venice API /chat/completions endpoint."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        if response.status_code == 200:
            return True, "Success"
        if response.status_code == 401:
            return False, "401 Unauthorized - API key invalid or missing"
        if response.status_code == 404:
            return (
                False,
                f"404 Not Found - URL: {url} (check base_url includes /api/v1)",
            )
        return False, f"HTTP {response.status_code}: {response.text[:200]}"
    except requests.exceptions.Timeout:
        return False, "Timeout"
    except requests.exceptions.ConnectionError as e:
        return False, f"Connection error: {e}"
    except Exception as e:
        return False, f"Error: {e}"


def main() -> None:
    """Run diagnostics."""
    print("=" * 80)
    print("Venice API Docker Diagnostics")
    print("=" * 80)
    print()

    # Test environment variables
    print("1. Environment Variables:")
    print("-" * 80)

    api_key_exists, api_key_value = test_env_var("VENICE_API_KEY")
    base_url_exists, base_url_value = test_env_var("VENICE_API_BASE_URL")
    model_exists, model_value = test_env_var("VENICE_HEARTBEAT_MODEL")

    print(f"  VENICE_API_KEY: {'✓ SET' if api_key_exists else '✗ MISSING'}")
    if api_key_exists:
        masked_key = (
            f"{api_key_value[:8]}...{api_key_value[-4:]}"
            if len(api_key_value) > 12
            else "***"
        )
        print(f"    Value: {masked_key}")
    else:
        print("    WARNING: API key not found in environment")

    print(f"  VENICE_API_BASE_URL: {'✓ SET' if base_url_exists else '✗ MISSING'}")
    if base_url_exists:
        print(f"    Value: {base_url_value}")
    else:
        print("    Using default: https://api.venice.ai/api/v1")

    print(f"  VENICE_HEARTBEAT_MODEL: {'✓ SET' if model_exists else '✗ MISSING'}")
    if model_exists:
        print(f"    Value: {model_value}")
    else:
        print("    Using default: qwen3-4b")

    print()

    # Test DNS resolution
    print("2. DNS Resolution:")
    print("-" * 80)
    dns_ok = test_dns_resolution("api.venice.ai")
    print(f"  api.venice.ai: {'✓ Resolved' if dns_ok else '✗ Failed'}")
    print()

    # Test HTTP connectivity
    print("3. HTTP Connectivity:")
    print("-" * 80)
    http_ok, http_msg = test_http_connectivity("https://api.venice.ai")
    print(f"  https://api.venice.ai: {'✓' if http_ok else '✗'} {http_msg}")
    print()

    # Test Venice API endpoints
    base_url = base_url_value or "https://api.venice.ai/api/v1"
    api_key = api_key_value if api_key_exists else None
    model = model_value or "qwen3-4b"

    print("4. Venice API Endpoints:")
    print("-" * 80)

    # Test /models endpoint
    models_ok, models_msg = test_venice_api_models(base_url, api_key)
    print(f"  GET /models: {'✓' if models_ok else '✗'} {models_msg}")

    # Test /chat/completions endpoint
    chat_ok, chat_msg = test_venice_api_chat(base_url, api_key, model)
    print(
        f"  POST /chat/completions (model={model}): {'✓' if chat_ok else '✗'} {chat_msg}"
    )
    print()

    # Summary
    print("=" * 80)
    print("Summary:")
    print("-" * 80)

    issues = []
    if not api_key_exists:
        issues.append("VENICE_API_KEY not set in environment")
    if not dns_ok:
        issues.append("DNS resolution failed for api.venice.ai")
    if not http_ok:
        issues.append("HTTP connectivity failed to api.venice.ai")
    if not models_ok:
        issues.append(f"Venice API /models endpoint failed: {models_msg}")
    if not chat_ok:
        issues.append(f"Venice API /chat/completions endpoint failed: {chat_msg}")

    if issues:
        print("✗ Issues found:")
        for issue in issues:
            print(f"  - {issue}")
        print()
        print("Recommendations:")
        if not api_key_exists:
            print(
                "  1. Ensure VENICE_API_KEY is set in .env, .env.docker, or docker/.env.local"
            )
            print(
                "  2. Verify docker-compose.yml env_file chain includes the file with the key"
            )
        if not dns_ok or not http_ok:
            print("  3. Check Docker network configuration and DNS settings")
            print("  4. Verify container can reach external networks")
        if not models_ok or not chat_ok:
            print("  5. Verify API key is valid and has proper permissions")
            print("  6. Check if base_url includes '/api/v1' suffix")
        sys.exit(1)
    else:
        print("✓ All checks passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()
