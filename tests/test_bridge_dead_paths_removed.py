from __future__ import annotations

import inspect
from types import SimpleNamespace


def test_diem_service_has_no_bridge_execution_shim() -> None:
    from services.diem.client import DIEMService

    assert not hasattr(DIEMService, "_trade_via_bridge_exact_out")
    source = inspect.getsource(DIEMService)
    assert "DIEM_ENABLE_BRIDGE_EXECUTION" not in source


def test_two_tx_fallback_shim_removed() -> None:
    import libs.dex.diem_fallbacks as fallbacks

    assert not hasattr(fallbacks, "execute_two_tx_fallback")
    source = inspect.getsource(fallbacks)
    assert "DIEM_ENABLE_TWO_TX_FALLBACK" not in source


def test_bridge_path_leg2_uses_vvv_usdc_bridge_provider(monkeypatch) -> None:
    monkeypatch.setenv("VVV_USDC_BRIDGE_PROVIDER", "aerodrome_cl")
    monkeypatch.setenv("VVV_USDC_POOL_FEE", "3000")

    from services.marketdata.pathing import fallbacks as path_fallbacks

    monkeypatch.setattr(path_fallbacks, "bridge_vvv_price", lambda _cfg: 1.25)
    config = SimpleNamespace(
        diem_token="0x1111111111111111111111111111111111111111",
        vvv_token="0x2222222222222222222222222222222222222222",
        quote_token="0x3333333333333333333333333333333333333333",
        vvv_usdc_pool="0x4444444444444444444444444444444444444444",
        diem_vvv_pair="0x5555555555555555555555555555555555555555",
    )
    meta = path_fallbacks.get_bridge_trade_path_with_metadata(config)
    assert meta is not None
    assert meta["legs"][1]["provider"] == "aerodrome_cl"
