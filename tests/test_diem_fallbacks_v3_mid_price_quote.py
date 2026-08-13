from __future__ import annotations

import math
from unittest.mock import patch


def _sqrt_price_x96_from_price_token1_per_token0(
    price_token1_per_token0: float, *, token0_decimals: int, token1_decimals: int
) -> int:
    # UniswapV3 sqrtPriceX96 encodes sqrt(token1_raw/token0_raw) * 2**96.
    price_raw = float(price_token1_per_token0) * (
        10 ** (token1_decimals - token0_decimals)
    )
    sqrt_price = math.sqrt(price_raw)
    return int(sqrt_price * (2**96))


class _StubFn:
    def __init__(self, value):
        self._value = value

    def call(self):
        return self._value


class _StubContract:
    def __init__(self, *, sqrt_price_x96: int, token0: str, token1: str):
        self._sqrt_price_x96 = int(sqrt_price_x96)
        self._token0 = str(token0)
        self._token1 = str(token1)

        class _Functions:
            def __init__(self, outer):
                self._outer = outer

            def slot0(self):
                # slot0 returns a tuple; only [0] is used by the fallback.
                return _StubFn((self._outer._sqrt_price_x96, 0, 0, 0, 0, 0, True))

            def token0(self):
                return _StubFn(self._outer._token0)

            def token1(self):
                return _StubFn(self._outer._token1)

        self.functions = _Functions(self)


class _StubWeb3:
    def __init__(self, *, contract: _StubContract):
        self._contract = contract

        class _Eth:
            def __init__(self, outer):
                self._outer = outer

            def contract(self, address=None, abi=None):
                return self._outer._contract

        self.eth = _Eth(self)

    def to_checksum_address(self, value: str) -> str:
        return str(value)


def test_vvv_usdc_v3_mid_price_quote_exact_in_handles_decimals(monkeypatch):
    from libs.dex.diem_fallbacks import vvv_usdc_v3_mid_price_quote

    vvv = "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    usdc = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

    monkeypatch.setenv("DIEM_VVV_USDC_V3_ANALYTIC_FALLBACK_ENABLE", "1")
    monkeypatch.setenv("VVV_USDC_POOL_V3_ADDRESS", "0xpool")
    monkeypatch.setenv("VVV_USDC_POOL_FEE", "3000")

    monkeypatch.setenv("VVV_TOKEN_ADDRESS", vvv)
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", usdc)
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")

    price_usdc_per_vvv = 1.2
    sqrt_price_x96 = _sqrt_price_x96_from_price_token1_per_token0(
        price_usdc_per_vvv,
        token0_decimals=18,
        token1_decimals=6,
    )
    w3 = _StubWeb3(
        contract=_StubContract(sqrt_price_x96=sqrt_price_x96, token0=vvv, token1=usdc)
    )

    with patch("libs.agentkit_ext.web3_utils.get_web3", return_value=w3):
        # 1 VVV -> ~1.2 USDC (minus fee)
        amount_in = 10**18
        quote = vvv_usdc_v3_mid_price_quote(amount_in, vvv, usdc)
        assert quote is not None
        assert quote.amount_in == amount_in
        assert quote.amount_out > 0

        fee_multiplier = 1.0 + (3000 / 1_000_000.0)
        expected_out = int((1.0 * price_usdc_per_vvv / fee_multiplier) * 10**6)
        assert abs(int(quote.amount_out) - expected_out) <= 2

        # 1 USDC -> ~0.833 VVV (minus fee)
        amount_in_usdc = 10**6
        quote_rev = vvv_usdc_v3_mid_price_quote(amount_in_usdc, usdc, vvv)
        assert quote_rev is not None
        expected_vvv = int(
            ((1.0 * (1.0 / price_usdc_per_vvv)) / fee_multiplier) * 10**18
        )
        assert abs(int(quote_rev.amount_out) - expected_vvv) <= 10_000_000_000


def test_vvv_usdc_v3_mid_price_quote_exact_out_handles_decimals(monkeypatch):
    from libs.dex.diem_fallbacks import vvv_usdc_v3_mid_price_quote_exact_out

    vvv = "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    usdc = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

    monkeypatch.setenv("DIEM_VVV_USDC_V3_ANALYTIC_FALLBACK_ENABLE", "1")
    monkeypatch.setenv("VVV_USDC_POOL_V3_ADDRESS", "0xpool")
    monkeypatch.setenv("VVV_USDC_POOL_FEE", "3000")

    monkeypatch.setenv("VVV_TOKEN_ADDRESS", vvv)
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", usdc)
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")

    price_usdc_per_vvv = 1.2
    sqrt_price_x96 = _sqrt_price_x96_from_price_token1_per_token0(
        price_usdc_per_vvv,
        token0_decimals=18,
        token1_decimals=6,
    )
    w3 = _StubWeb3(
        contract=_StubContract(sqrt_price_x96=sqrt_price_x96, token0=vvv, token1=usdc)
    )

    fee_multiplier = 1.0 + (3000 / 1_000_000.0)

    with patch("libs.agentkit_ext.web3_utils.get_web3", return_value=w3):
        # Want 1 USDC out -> need ~0.833 VVV in (plus fee)
        amount_out = 10**6
        quote = vvv_usdc_v3_mid_price_quote_exact_out(amount_out, vvv, usdc)
        assert quote is not None
        assert quote.amount_out == amount_out
        expected_in = int(((1.0 / price_usdc_per_vvv) * fee_multiplier) * 10**18)
        assert abs(int(quote.amount_in) - expected_in) <= 10_000_000_000

        # Want 1 VVV out -> need ~1.2 USDC in (plus fee)
        amount_out_vvv = 10**18
        quote_rev = vvv_usdc_v3_mid_price_quote_exact_out(amount_out_vvv, usdc, vvv)
        assert quote_rev is not None
        expected_in_usdc = int(((1.0 * price_usdc_per_vvv) * fee_multiplier) * 10**6)
        assert abs(int(quote_rev.amount_in) - expected_in_usdc) <= 2
