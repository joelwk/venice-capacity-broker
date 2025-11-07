from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any, Deque, Dict, List, Optional

# Optional env and metrics helpers
try:
	from libs.env import is_production, env_flag  # type: ignore
except Exception:  # noqa: BLE001
	def is_production() -> bool:  # type: ignore
		return (os.getenv("APP_ENV") or "").strip().lower() in {"production", "prod"}

	def env_flag(name: str, default: bool = False) -> bool:  # type: ignore
		v = os.getenv(name)
		if v is None:
			return default
		return str(v).strip().lower() in {"1", "true", "yes", "on"}

try:
	from libs.telemetry.metrics import inc as _metrics_inc  # type: ignore
except Exception:  # noqa: BLE001
	def _metrics_inc(name: str, value: int = 1, labels: Dict[str, str] | None = None) -> None:  # type: ignore
		return


class MemoryStore:
	"""Lightweight append-only store for agent cycle records.

	In production, writes to SQL `AgentMemory` with retention.
	In non-production, defaults to JSON lines unless disabled or SQL is preferred.
	"""

	def __init__(
		self,
		path: str | os.PathLike[str] | None = None,
		*,
		buffer_size: int = 256,
	) -> None:
		default_path = os.getenv("AGENT_MEMORY_PATH") or "db/agent_memory.jsonl"
		self._path = Path(path or default_path)
		self._lock = Lock()
		self._buffer: Deque[Dict[str, Any]] = deque(maxlen=max(1, int(buffer_size)))
		self._last_prune_epoch: float = 0.0

	# ------------------------------------------------------------------
	@property
	def path(self) -> Path:
		return self._path

	# ------------------------------------------------------------------
	def record_cycle(self, record: Dict[str, Any]) -> Dict[str, Any]:
		"""Persist a cycle record and keep a short in-memory buffer.

		Production: write to SQL AgentMemory; fail softly only in dev.
		"""

		entry = {
			"ts": record.get("ts"),
			"cycle": self._sanitize(record),
		}

		# Try SQL first when available or required
		use_sql = is_production() or env_flag("MEMORY_SQL_ENABLE", True)
		if use_sql:
			try:
				self._record_sql(entry)
				recorded = True
			except Exception as _e:  # noqa: BLE001
				_metrics_inc("sql_persist_error_total", labels={"entity": "agent_memory"})
				if is_production():
					# In production, propagate to alert operators
					raise
				# Dev-only: fall back to JSON if allowed
				recorded = False
		else:
			recorded = False

		if not recorded:
			if is_production() or not env_flag("ALLOW_JSON_FALLBACK", False):
				# Refuse silent JSON fallback in production/non-allowed envs
				raise RuntimeError("AgentMemory JSON fallback disabled; configure Postgres or set ALLOW_JSON_FALLBACK=1 in dev")
			_metrics_inc("fallback_json_store_total", labels={"component": "agent_memory"})
			payload = json.dumps(entry, separators=(",", ":"))
			with self._lock:
				self._path.parent.mkdir(parents=True, exist_ok=True)
				with self._path.open("a", encoding="utf-8") as fh:
					fh.write(payload)
					fh.write("\n")

		with self._lock:
			self._buffer.append(entry)
		return entry

	# ------------------------------------------------------------------
	def recent(self, limit: int = 10) -> List[Dict[str, Any]]:
		"""Return the most recent limit entries (oldest -> newest)."""

		if limit <= 0:
			return []
		with self._lock:
			if not self._path.exists():
				return list(self._buffer)[-limit:]
			return self._tail_locked(limit)

	# ------------------------------------------------------------------
	def most_recent(self) -> Optional[Dict[str, Any]]:
		"""Return the latest record if available."""

		entries = self.recent(1)
		return entries[0] if entries else None

	# ------------------------------------------------------------------
	def _tail_locked(self, count: int) -> List[Dict[str, Any]]:
		entries: Deque[Dict[str, Any]] = deque(maxlen=count)
		with self._path.open("r", encoding="utf-8") as fh:
			for line in fh:
				line = line.strip()
				if not line:
					continue
				try:
					obj = json.loads(line)
					if isinstance(obj, dict):
						entries.append(obj)
				except json.JSONDecodeError:
					continue
		return list(entries)

	# ------------------------------------------------------------------
	def _sanitize(self, value: Any) -> Any:
		if value is None:
			return None
		if isinstance(value, (str, int, float, bool)):
			return value
		if isinstance(value, dict):
			return {str(k): self._sanitize(v) for k, v in value.items()}
		if isinstance(value, (list, tuple, set)):
			return [self._sanitize(v) for v in value]
		return str(value)

	# ------------------------------------------------------------------
	def _record_sql(self, entry: Dict[str, Any]) -> None:
		# Lazy imports to avoid hard deps for non-SQL paths
		from datetime import datetime, timezone, timedelta
		from uuid import uuid4
		from sqlmodel import Session
		from db.session import get_engine
		from db.models import AgentMemory

		eng = get_engine()
		now = datetime.now(timezone.utc)
		with Session(eng) as s:  # type: ignore[call-attr]
			# Basic retention pruning throttled to once per 60s to limit overhead
			try:
				if is_production():
					self._prune_sql_retention(s, now)
			except Exception:
				# Never block writes due to prune failures
				pass

			payload = entry.get("cycle")
			agent = "system"
			try:
				if isinstance(payload, dict):
					agent = str(payload.get("agent") or payload.get("actor") or "system")
			except Exception:
				agent = "system"
			row = AgentMemory(
				id=uuid4().hex,
				agent=agent,
				cycle_id=None,
				decision_id=None,
				created_at=now,
				payload=payload if isinstance(payload, dict) else None,
			)
			s.add(row)
			s.commit()

	def _prune_sql_retention(self, session) -> None:  # type: ignore[no-untyped-def]
		from datetime import datetime, timezone, timedelta
		from sqlmodel import select
		from db.models import AgentMemory
		import sqlalchemy as sa  # type: ignore

		retention_days_raw = os.getenv("MEMORY_RETENTION_DAYS") or "30"
		try:
			retention_days = int(retention_days_raw)
		except Exception:
			retention_days = 30
		if retention_days <= 0:
			return
		now_epoch = time.time()
		if now_epoch - self._last_prune_epoch < 60.0:
			return
		self._last_prune_epoch = now_epoch
		cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
		try:
			# Use SQLAlchemy delete to avoid loading rows
			session.exec(sa.delete(AgentMemory).where(AgentMemory.created_at < cutoff))  # type: ignore[arg-type]
			session.commit()
		except Exception:
			# Best-effort; ignore failures
			try:
				session.rollback()
			except Exception:
				pass


__all__ = ["MemoryStore"]
