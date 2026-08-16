from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("sqlmodel")


def test_persist_and_seed_price_ticks(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "ticks.db"
    monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("RISK_VOL_PERSIST", "true")
    monkeypatch.setenv("SQL_CREATE_ALL_ON_START", "true")

    from db.session import create_db_and_tables

    create_db_and_tables()

    from services.risk.pricetick import load_recent_prices, persist_price_tick

    persist_price_tick("DIEM", 1.10)
    persist_price_tick("DIEM", 1.20)
    persist_price_tick("DIEM", 1.25)
    hist = load_recent_prices("DIEM", limit=16)
    assert hist == pytest.approx([1.10, 1.20, 1.25])

    from graph.workflows.orchestrator import Orchestrator

    orch = Orchestrator(market=SimpleNamespace(), arbi=SimpleNamespace())
    orch._seed_px_hist()
    assert orch._px_hist == pytest.approx([1.10, 1.20, 1.25])


def test_vol_persist_defaults_on_when_sql_url_set(monkeypatch) -> None:
    monkeypatch.delenv("RISK_VOL_PERSIST", raising=False)
    monkeypatch.setenv("SQL_DATABASE_URL", "sqlite:///tmp.db")
    from services.risk.pricetick import vol_persist_enabled

    assert vol_persist_enabled() is True

    monkeypatch.setenv("RISK_VOL_PERSIST", "false")
    assert vol_persist_enabled() is False
