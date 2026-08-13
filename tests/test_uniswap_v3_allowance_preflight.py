from __future__ import annotations

from unittest.mock import MagicMock, patch

from libs.dex.composite import (
    _preflight_allowance_low,
    execute_with_uniswap_v3_stf_retry,
)
from libs.dex.routes import make_route


def _addr(seed: str) -> str:
    return "0x" + (seed * 40)[:40]


class _Provider:
    name = "uniswap_v3"

    def __init__(self) -> None:
        self.router_addr = _addr("1")
        self.recipient = _addr("2")
        self.ensure_calls: list[tuple[str, str, str, int]] = []

    def _ensure_allowance(
        self, token: str, owner: str, spender: str, required: int
    ) -> str:
        self.ensure_calls.append((token, owner, spender, int(required)))
        return "0x" + "a" * 64


def test_preflight_marks_allowance_low():
    provider = _Provider()
    route = make_route([_addr("3"), _addr("4")])

    with (
        patch("libs.agentkit_ext.web3_utils.get_web3") as mock_web3,
        patch("libs.agentkit_ext.web3_utils.get_contract") as mock_contract,
    ):
        mock_web3.return_value = MagicMock()

        mock_erc20 = MagicMock()
        mock_erc20.functions.allowance.return_value.call.return_value = 0
        mock_contract.side_effect = lambda w3, addr, abi: mock_erc20

        snap = _preflight_allowance_low(provider, route, required=123)

    assert snap["status"] == "low"
    assert snap["allowance"] == 0
    assert snap["required"] == 123
    assert snap["spender"] == provider.router_addr


def test_stf_retry_injects_approval_and_succeeds():
    provider = _Provider()
    provider.router_addr = _addr("7")
    provider.recipient = _addr("8")
    route = make_route([_addr("9"), _addr("a")])

    with (
        patch("libs.dex.composite._preflight_allowance_low") as mock_preflight,
    ):
        mock_preflight.return_value = {
            "status": "low",
            "token_in": _addr("9"),
            "owner": provider.recipient,
            "spender": provider.router_addr,
            "allowance": 0,
            "required": 123,
        }

        calls: list[int] = []

        def _attempt():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("execution reverted: STF")
            return {"status": "sent", "tx_hash": "0x" + "b" * 64}

        retry_state: dict[str, object] = {}
        result, info = execute_with_uniswap_v3_stf_retry(
            provider=provider,
            route=route,
            required_allowance=123,
            attempt=_attempt,
            correlation_id="cid-1",
            retry_state=retry_state,
        )

    assert result["tx_hash"] == "0x" + "b" * 64
    assert info["retried"] is True
    assert info["approval_tx"] == "0x" + "a" * 64
    assert info["attempts"] == 2
    assert retry_state.get("stf_retry_used") is True
    assert len(provider.ensure_calls) == 1
