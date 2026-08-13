from unittest.mock import Mock

from agents.arbi_diem.agent import ArbiDiem


def test_build_stake_recommendation_shortfall(monkeypatch):
    diem = Mock()
    agent = ArbiDiem(diem=diem)

    rec = agent._build_stake_recommendation(
        mint_needed_units=1_000_000,
        mint_check={"required_svvv": 300, "available_svvv": 100},
        mint_rate=2.5,
        corr_id="abc123",
    )

    assert rec is not None
    assert rec["action"] == "stake_vvv"
    assert rec["source"] == "arbi_diem"
    assert rec["shortfall_units"] == 200
    assert rec["correlation_id"] == "abc123"
