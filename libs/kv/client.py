from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any
from urllib.parse import quote, urljoin

import requests

# Optional env + metrics
try:
    from libs.env import env_flag, is_production  # type: ignore
except Exception:

    def is_production() -> bool:  # type: ignore
        return (os.getenv("APP_ENV") or "").strip().lower() in {"production", "prod"}

    def env_flag(name: str, default: bool = False) -> bool:  # type: ignore
        v = os.getenv(name)
        if v is None:
            return default
        return str(v).strip().lower() in {"1", "true", "yes", "on"}


try:
    from libs.telemetry.metrics import inc as _metrics_inc  # type: ignore
except Exception:

    def _metrics_inc(
        name: str, value: int = 1, labels: dict[str, str] | None = None
    ) -> None:  # type: ignore
        return


class KVStore:
    """
    Minimal KV client with in-memory fallback.

    - Intended for Redis or Replit DB via env-configured URL/token.
    - In production, a durable backend is required; in-memory fallback is disabled.
    - In dev/test, in-memory fallback requires ALLOW_INMEMORY_KV_FALLBACK=1.
    """

    def __init__(self) -> None:
        # Prefer Replit's managed KV when available so hosted deployments never rely on the
        # placeholder KV_URL from .env. Redis remains the default in Docker via REDIS_URL.
        self.base_url = os.getenv("REPLIT_DB_URL") or os.getenv("KV_URL")
        self.api_token = os.getenv("KV_API_TOKEN")
        self.api_header = os.getenv("KV_API_HEADER")
        self.namespace = os.getenv("KV_NAMESPACE")
        self.prefix = os.getenv("KV_PREFIX", "")
        self._mem: dict[str, tuple[Any, float | None]] = {}
        self._lock = threading.Lock()
        # Optional Redis backend for atomic counters
        self.redis_url = os.getenv("REDIS_URL") or os.getenv("KV_REDIS_URL")
        self._redis = None
        self._last_backend_error: str | None = None

        # Enforce durable backend in production
        if is_production() and not (self.redis_url or self.base_url):
            raise RuntimeError(
                "Production requires durable KV (REDIS_URL or REPLIT_DB_URL)"
            )

        self._logger = logging.getLogger("kv.store")

    def _record_backend_error(
        self, source: str, error: Exception | None = None
    ) -> None:
        msg = source
        if error:
            msg = f"{source}: {error}"
        self._last_backend_error = msg
        try:
            self._logger.debug("kv backend error: %s", msg)
        except Exception:
            pass

    def _clear_backend_error(self) -> None:
        self._last_backend_error = None

    def _get_redis(self):
        if not self.redis_url:
            return None
        if self._redis is None:
            try:
                import redis  # type: ignore

                self._redis = redis.from_url(self.redis_url, decode_responses=True)
            except Exception:
                self._redis = None
        return self._redis

    def has_atomic_counters(self) -> bool:
        """Return True when atomic counter semantics are available (Redis)."""
        return self._get_redis() is not None

    def _k(self, key: str) -> str:
        if self.namespace:
            key = f"{self.namespace}:{key}"
        if self.prefix:
            key = f"{self.prefix}{key}"
        return key

    def _request_headers(self) -> dict[str, str]:
        token = (self.api_token or "").strip()
        if not token:
            return {}
        header_name = (self.api_header or "Authorization").strip()
        if not header_name:
            header_name = "Authorization"
        if header_name.lower() == "authorization":
            lowered = token.lower()
            if (
                lowered.startswith("bearer ")
                or lowered.startswith("basic ")
                or lowered.startswith("token ")
            ):
                value = token
            else:
                value = f"Bearer {token}"
            return {header_name: value}
        return {header_name: token}

    # --- In-memory fallback implementation ---
    def _ensure_inmem_allowed(self) -> None:
        if is_production() or not env_flag("ALLOW_INMEMORY_KV_FALLBACK", False):
            detail = self._last_backend_error or "configure REDIS_URL or REPLIT_DB_URL"
            raise RuntimeError(f"In-memory KV fallback disabled; {detail}")

    def _mem_get(self, key: str) -> str | None:
        self._ensure_inmem_allowed()
        _metrics_inc("fallback_inmemory_kv_total", labels={"op": "get"})
        with self._lock:
            rec = self._mem.get(key)
            if not rec:
                return None
            val, exp = rec
            if exp is not None and exp < time.time():
                # expired
                del self._mem[key]
                return None
            return str(val) if val is not None else None

    def _mem_set(self, key: str, value: Any, ttl_s: int | None = None) -> None:
        self._ensure_inmem_allowed()
        _metrics_inc("fallback_inmemory_kv_total", labels={"op": "set"})
        with self._lock:
            exp = time.time() + ttl_s if ttl_s and ttl_s > 0 else None
            self._mem[key] = (value, exp)

    def _mem_incrby(self, key: str, by: int = 1, ttl_s: int | None = None) -> int:
        self._ensure_inmem_allowed()
        _metrics_inc("fallback_inmemory_kv_total", labels={"op": "incrby"})
        with self._lock:
            cur_v, exp = self._mem.get(key, (0, None))
            if exp is not None and exp < time.time():
                cur_v = 0
            new_v = int(cur_v) + int(by)
            new_exp = time.time() + ttl_s if ttl_s and ttl_s > 0 else exp
            self._mem[key] = (new_v, new_exp)
            return new_v

    # --- Public API ---
    def get(self, key: str) -> str | None:
        k = self._k(key)
        r = self._get_redis()
        if r is not None:
            try:
                value = r.get(k)
                self._clear_backend_error()
                return value
            except Exception as exc:
                self._record_backend_error("redis get failed", exc)
        if self.base_url:
            # Best-effort remote get; support simple Replit DB style API
            try:
                # Handle TTL via companion exp key
                exp_raw = None
                headers = self._request_headers() or None
                try:
                    exp_raw = requests.get(
                        urljoin(self.base_url.rstrip("/") + "/", quote(k + ":exp")),
                        timeout=3,
                        headers=headers,
                    )
                    if exp_raw.ok and exp_raw.text:
                        try:
                            exp = float(exp_raw.text)
                            if exp and exp < time.time():
                                # expired; delete and return None
                                requests.delete(
                                    urljoin(self.base_url.rstrip("/") + "/", quote(k)),
                                    timeout=3,
                                    headers=headers,
                                )
                                requests.delete(
                                    urljoin(
                                        self.base_url.rstrip("/") + "/",
                                        quote(k + ":exp"),
                                    ),
                                    timeout=3,
                                    headers=headers,
                                )
                                return None
                        except Exception:
                            pass
                except Exception:
                    pass
                r = requests.get(
                    urljoin(self.base_url.rstrip("/") + "/", quote(k)),
                    timeout=3,
                    headers=headers,
                )
                if r.status_code == 404:
                    self._clear_backend_error()
                    return None
                if r.ok:
                    self._clear_backend_error()
                    return r.text
                r.raise_for_status()
            except Exception as exc:
                # fall back to in-mem on network errors
                self._record_backend_error("http get failed", exc)
        return self._mem_get(k)

    def set(self, key: str, value: Any, ttl_s: int | None = None) -> None:
        k = self._k(key)
        r = self._get_redis()
        if r is not None:
            try:
                if ttl_s and ttl_s > 0:
                    r.set(k, str(value), ex=int(ttl_s))
                else:
                    r.set(k, str(value))
                self._clear_backend_error()
                return
            except Exception as exc:
                self._record_backend_error("redis set failed", exc)
        if self.base_url:
            try:
                # Replit DB: PUT /key with raw body
                headers = self._request_headers() or None
                resp = requests.put(
                    urljoin(self.base_url.rstrip("/") + "/", quote(k)),
                    data=str(value),
                    timeout=3,
                    headers=headers,
                )
                if not resp.ok:
                    resp.raise_for_status()
                if ttl_s and ttl_s > 0:
                    exp = time.time() + ttl_s
                    exp_resp = requests.put(
                        urljoin(self.base_url.rstrip("/") + "/", quote(k + ":exp")),
                        data=str(exp),
                        timeout=3,
                        headers=headers,
                    )
                    if not exp_resp.ok:
                        exp_resp.raise_for_status()
                self._clear_backend_error()
                return
            except Exception as exc:
                self._record_backend_error("http set failed", exc)
        self._mem_set(k, value, ttl_s)

    def incrby(self, key: str, by: int = 1, ttl_s: int | None = None) -> int:
        k = self._k(key)
        r = self._get_redis()
        if r is not None:
            try:
                # Atomic INCRBY and set TTL if not set
                pipe = r.pipeline()
                pipe.incrby(k, int(by))
                pipe.ttl(k)
                new_v, cur_ttl = pipe.execute()
                if ttl_s and ttl_s > 0 and (cur_ttl is None or int(cur_ttl) < 0):
                    r.expire(k, int(ttl_s))
                self._clear_backend_error()
                return int(new_v)
            except Exception as exc:
                self._record_backend_error("redis incrby failed", exc)
        if self.base_url:
            # Not atomic on Replit DB; best-effort read-modify-write
            try:
                cur = self.get(key)
                try:
                    cur_v = int(cur) if cur is not None else 0
                except Exception:
                    cur_v = 0
                new_v = cur_v + int(by)
                self.set(key, new_v, ttl_s=ttl_s)
                self._clear_backend_error()
                return new_v
            except Exception as exc:
                self._record_backend_error("http incrby failed", exc)
        return self._mem_incrby(k, by, ttl_s)

    def delete(self, key: str) -> None:
        k = self._k(key)
        r = self._get_redis()
        if r is not None:
            try:
                r.delete(k)
                self._clear_backend_error()
                return
            except Exception as exc:
                self._record_backend_error("redis delete failed", exc)
        if self.base_url:
            try:
                requests.delete(
                    urljoin(self.base_url.rstrip("/") + "/", quote(k)),
                    timeout=3,
                    headers=self._request_headers() or None,
                )
                self._clear_backend_error()
            except Exception as exc:
                self._record_backend_error("http delete failed", exc)
        with self._lock:
            self._mem.pop(k, None)

    # --- Key listing (best-effort) ---
    def keys(self, prefix: str) -> list[str]:
        """
        List keys with a given prefix (without namespace/prefix). Best-effort:
        - Redis: uses SCAN and strips config prefix/namespace
        - Replit DB: tries GET /?prefix= and strips prefix
        - In-memory: returns matching keys (dev-only, gated)
        """
        # Compute the fully-qualified prefix used in storage
        fq_prefix = self._k(prefix)

        # Redis backend
        r = self._get_redis()
        if r is not None:
            try:
                out: list[str] = []
                pattern = fq_prefix + "*"
                for k in r.scan_iter(match=pattern):  # type: ignore[attr-defined]
                    k_str = str(k)
                    # Strip namespace/prefix to return logical keys
                    logical = (
                        k_str[len(self._k("")) :]
                        if k_str.startswith(self._k(""))
                        else k_str
                    )
                    out.append(logical)
                self._clear_backend_error()
                return out
            except Exception as exc:
                self._record_backend_error("redis keys failed", exc)

        # Replit DB style HTTP
        if self.base_url:
            try:
                # Build query without urljoin to avoid dropping path segments
                base = self.base_url.rstrip("/") + "/"
                url = f"{base}?prefix={quote(fq_prefix)}"
                r = requests.get(
                    url, timeout=5, headers=self._request_headers() or None
                )
                if r.ok:
                    try:
                        data = r.json()
                    except Exception:
                        data = None
                    if isinstance(data, list):
                        out = []
                        for k in data:
                            k_str = str(k)
                            logical = (
                                k_str[len(self._k("")) :]
                                if k_str.startswith(self._k(""))
                                else k_str
                            )
                            out.append(logical)
                        self._clear_backend_error()
                        return out
                r.raise_for_status()
            except Exception as exc:
                self._record_backend_error("http keys failed", exc)

        # In-memory fallback (dev-only)
        self._ensure_inmem_allowed()
        _metrics_inc("fallback_inmemory_kv_total", labels={"op": "keys"})
        out: list[str] = []
        with self._lock:
            pfx = self._k("")
            for k, (v, exp) in list(self._mem.items()):
                # Drop expired
                if exp is not None and exp < time.time():
                    continue
                if k.startswith(fq_prefix):
                    logical = k[len(pfx) :] if k.startswith(pfx) else k
                    out.append(logical)
        return out
