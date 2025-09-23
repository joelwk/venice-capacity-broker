import os

from utils.agentkit_integration import (
    CustomUniswapActionProvider,
    get_test_action_provider,
    get_test_agentkit_instance,
    reset_agentkit_for_tests,
)


def setup_function() -> None:  # pragma: no cover - pytest hook
    reset_agentkit_for_tests()


def test_agentkit_instance_contains_default_providers():
    instance = get_test_agentkit_instance()
    assert {p.provider_type for p in instance.action_providers} == {"weth", "pyth", "uniswap"}


def test_get_test_action_provider_caches_instances():
    first = get_test_action_provider("uniswap")
    second = get_test_action_provider("uniswap")
    assert first is second


def test_alias_resolution_for_uniswap_v2():
    provider = get_test_action_provider("uniswap_v2")
    assert provider.provider_type == "uniswap"


def test_custom_uniswap_resolves_default_tokens():
    provider = CustomUniswapActionProvider()
    assert provider.resolve_token_address("weth").lower() == os.getenv(
        "WETH_ADDRESS", "0x4200000000000000000000000000000000000006"
    ).lower()


def test_unknown_provider_raises_value_error():
    reset_agentkit_for_tests()
    try:
        get_test_action_provider("unknown")
    except ValueError as exc:  # pragma: no cover - ensure error raised
        assert "Unsupported provider type" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected ValueError for unknown provider")
