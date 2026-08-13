from __future__ import annotations

from agents.arbi_diem.agent import _utilization_log_value
from libs.telemetry import metrics


def _reset_metrics() -> None:
    # Tests need deterministic counters.
    # We reset in-process telemetry only (no external side effects).
    with metrics._lock:  # type: ignore[attr-defined]
        metrics._counters.clear()  # type: ignore[attr-defined]
        metrics._gauges.clear()  # type: ignore[attr-defined]


def test_utilization_none_logs_na_and_increments_missing_metric():
    _reset_metrics()
    assert _utilization_log_value(None) == "n/a"
    prom = metrics.render_prom()
    assert "vvv_arbi_diem_utilization_missing_total" in prom


def test_utilization_zero_logs_zero_percent_and_does_not_increment_missing_metric():
    _reset_metrics()
    assert _utilization_log_value(0.0) == "0.00%"
    prom = metrics.render_prom()
    assert "vvv_arbi_diem_utilization_missing_total" not in prom
