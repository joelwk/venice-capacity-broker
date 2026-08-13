"""
Test memory write, recall, and restart continuity across agent/orchestrator runs.

This test verifies that:
1. Memory writes occur after each orchestrator cycle
2. Key fields (decisions, signals, outcomes) are present in stored records
3. Memory recall works across sequential runs
4. State persistence enables restart continuity
"""

import json
from pathlib import Path

import pytest

from services.memory.store import MemoryStore


@pytest.fixture
def temp_memory_path(tmp_path):
    """Create a temporary memory file path."""
    memory_file = tmp_path / "test_agent_memory.jsonl"
    return str(memory_file)


@pytest.fixture
def memory_store(temp_memory_path, monkeypatch):
    """Create a MemoryStore instance with temporary path and JSON fallback enabled."""
    monkeypatch.setenv("ALLOW_JSON_FALLBACK", "1")
    monkeypatch.setenv("MEMORY_SQL_ENABLE", "0")
    return MemoryStore(path=temp_memory_path)


def test_memory_write_after_action(memory_store, temp_memory_path):
    """Test that memory writes occur after a deterministic agent cycle."""
    cycle_record = {
        "ts": 1234567890.0,
        "agent": "arbi_diem",
        "action": "buy_burn",
        "signals": {
            "premium": 0.728,
            "vol_bps": 0.0,
            "utilization_ratio": None,
        },
        "outcome": True,
        "execution": {
            "status": "dry_run",
            "executed": False,
        },
        "why": {
            "market_price": 133.55,
            "fair_value": 183.33,
            "decision": "buy_burn",
        },
    }

    # Record the cycle
    entry = memory_store.record_cycle(cycle_record)

    # Verify entry structure
    assert "ts" in entry
    assert "cycle" in entry
    assert entry["ts"] == 1234567890.0

    # Verify file was written
    memory_path = Path(temp_memory_path)
    assert memory_path.exists()

    # Read and verify contents
    with memory_path.open("r") as f:
        lines = [line.strip() for line in f if line.strip()]
        assert len(lines) == 1

        stored = json.loads(lines[0])
        assert stored["ts"] == 1234567890.0
        assert stored["cycle"]["agent"] == "arbi_diem"
        assert stored["cycle"]["action"] == "buy_burn"
        assert stored["cycle"]["signals"]["premium"] == 0.728
        assert stored["cycle"]["outcome"] is True


def test_memory_recall_across_runs(memory_store, temp_memory_path):
    """Test that memory recall works across sequential runs."""
    # First run: record cycle 1
    cycle1 = {
        "ts": 1000.0,
        "agent": "stake_master",
        "action": "claim",
        "outcome": True,
    }
    memory_store.record_cycle(cycle1)

    # Second run: record cycle 2
    cycle2 = {
        "ts": 2000.0,
        "agent": "arbi_diem",
        "action": "hold",
        "outcome": False,
    }
    memory_store.record_cycle(cycle2)

    # Create a new MemoryStore instance (simulating restart)
    memory_store2 = MemoryStore(path=temp_memory_path)

    # Verify recent() returns both cycles
    recent = memory_store2.recent(limit=10)
    assert len(recent) == 2
    assert recent[0]["ts"] == 1000.0
    assert recent[1]["ts"] == 2000.0

    # Verify most_recent() returns latest
    latest = memory_store2.most_recent()
    assert latest is not None
    assert latest["ts"] == 2000.0
    assert latest["cycle"]["agent"] == "arbi_diem"


def test_memory_contains_key_fields(memory_store, temp_memory_path):
    """Test that stored records contain key fields (decisions, signals, outcomes)."""
    cycle_record = {
        "ts": 3000.0,
        "agent": "arbi_diem",
        "action": "mint_sell",
        "signals": {
            "premium": 1.15,
            "vol_bps": 25.5,
            "utilization_ratio": 0.75,
        },
        "decision": "mint_sell",
        "outcome": True,
        "execution": {
            "status": "dry_run",
            "executed": False,
        },
        "quorum": {
            "status": "approved",
            "ratio": 0.65,
        },
    }

    memory_store.record_cycle(cycle_record)

    # Read back and verify key fields
    memory_path = Path(temp_memory_path)
    with memory_path.open("r") as f:
        stored = json.loads(f.read().strip())
        cycle = stored["cycle"]

        # Verify decision fields
        assert cycle["agent"] == "arbi_diem"
        assert cycle["action"] == "mint_sell"
        assert cycle["decision"] == "mint_sell"

        # Verify signal fields
        assert "signals" in cycle
        assert cycle["signals"]["premium"] == 1.15
        assert cycle["signals"]["vol_bps"] == 25.5
        assert cycle["signals"]["utilization_ratio"] == 0.75

        # Verify outcome fields
        assert cycle["outcome"] is True
        assert "execution" in cycle
        assert cycle["execution"]["status"] == "dry_run"

        # Verify quorum fields
        assert "quorum" in cycle
        assert cycle["quorum"]["status"] == "approved"


def test_memory_restart_continuity(memory_store, temp_memory_path):
    """Test that restart continuity works with progressive live state."""
    # Simulate progressive live state
    cycle_record = {
        "ts": 4000.0,
        "progressive": {
            "requested": True,
            "override": False,
            "live": False,
            "state": {
                "counter": 3,
                "live": False,
                "threshold": 5,
                "enabled": True,
            },
        },
        "reflex": {
            "halt": False,
            "lastCycleTs": 3990.0,
        },
    }

    memory_store.record_cycle(cycle_record)

    # Simulate restart: create new MemoryStore
    memory_store2 = MemoryStore(path=temp_memory_path)

    # Verify state can be loaded
    latest = memory_store2.most_recent()
    assert latest is not None
    assert latest["cycle"]["progressive"]["state"]["counter"] == 3
    assert latest["cycle"]["progressive"]["state"]["live"] is False
    assert latest["cycle"]["reflex"]["lastCycleTs"] == 3990.0


def test_memory_buffer_behavior(memory_store):
    """Test that in-memory buffer works correctly."""
    # Record multiple cycles
    for i in range(5):
        cycle = {
            "ts": 5000.0 + i,
            "agent": "test",
            "cycle_num": i,
        }
        memory_store.record_cycle(cycle)

    # Verify buffer contains recent entries
    recent = memory_store.recent(limit=3)
    assert len(recent) >= 3
    assert recent[-1]["ts"] >= 5000.0


def test_memory_sanitization(memory_store, temp_memory_path):
    """Test that memory sanitization handles various data types."""
    cycle_record = {
        "ts": 6000.0,
        "agent": "test",
        "nested": {
            "list": [1, 2, 3],
            "tuple": (4, 5, 6),
            "set": {7, 8, 9},
            "dict": {"key": "value"},
        },
        "none_value": None,
        "bool_value": True,
        "int_value": 42,
        "float_value": 3.14,
        "str_value": "test",
    }

    memory_store.record_cycle(cycle_record)

    # Verify sanitization worked (no exceptions, data preserved)
    memory_path = Path(temp_memory_path)
    with memory_path.open("r") as f:
        stored = json.loads(f.read().strip())
        cycle = stored["cycle"]

        assert cycle["nested"]["list"] == [1, 2, 3]
        assert cycle["nested"]["dict"]["key"] == "value"
        assert cycle["none_value"] is None
        assert cycle["bool_value"] is True
        assert cycle["int_value"] == 42
        assert cycle["float_value"] == 3.14
        assert cycle["str_value"] == "test"


def test_memory_limit_enforcement(memory_store):
    """Test that recent() respects limit parameter."""
    # Record 10 cycles
    for i in range(10):
        cycle = {"ts": 7000.0 + i, "agent": "test", "cycle": i}
        memory_store.record_cycle(cycle)

    # Request only 5 most recent
    recent = memory_store.recent(limit=5)
    assert len(recent) == 5
    assert recent[-1]["ts"] == 7009.0  # Most recent


def test_memory_empty_file_handling(memory_store):
    """Test that memory handles empty or missing files gracefully."""
    # Request recent from empty store
    recent = memory_store.recent(limit=10)
    assert isinstance(recent, list)
    assert len(recent) == 0

    # Most recent should return None
    latest = memory_store.most_recent()
    assert latest is None
