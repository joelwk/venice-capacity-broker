from __future__ import annotations

from agents.quorum.core import Quorum, QuorumMember, QuorumVote


def test_quorum_weighted_confidence():
    members = [
        QuorumMember(
            name="yield",
            weight=1.0,
            vote=lambda: QuorumVote(approve=True, confidence=0.9),
        ),
        QuorumMember(
            name="risk",
            weight=1.0,
            vote=lambda: QuorumVote(approve=False, confidence=0.8, reason="volatility"),
        ),
        QuorumMember(
            name="demand",
            weight=0.5,
            vote=lambda: True,
        ),
    ]
    quorum = Quorum(members=members, threshold=0.55)
    decision, info = quorum.decide_with_details()

    assert decision is True
    assert 0 < info["ratio"] <= 1
    breakdown = {entry["name"]: entry for entry in info["breakdown"]}
    assert breakdown["risk"]["approve"] is False
    assert "volatility" in breakdown["risk"]["reason"]
    # Calling decide() afterwards should reuse the stored result
    assert quorum.decide() is True
    assert quorum.last_info()["ratio"] == info["ratio"]
