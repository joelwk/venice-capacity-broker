from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from hexbytes import HexBytes  # type: ignore

from db.models import DexFactoryCursor, DexPool
from db.session import create_db_and_tables, get_session
from libs.dex.routes import RoutePlan, make_route
from libs.telemetry.logger import get_logger

try:  # pragma: no cover - optional dependency in some CI images
    from sqlmodel import Session, select
    from sqlalchemy import and_, or_  # type: ignore
except Exception:  # noqa: BLE001
    Session = None  # type: ignore
    select = None  # type: ignore
    and_ = None  # type: ignore
    or_ = None  # type: ignore


logger = get_logger("marketdata.pools")


UNISWAP_V2_PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
AERODROME_PAIR_CREATED_TOPIC = "0xc4805696c66d7cf352fc1d6bb633ad5ee82f6cb577c453024b6e0eb8306c6fc9"
UNISWAP_V3_POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"


def _truthy_env(name: str, default: str = "false") -> bool:
    return (os.getenv(name) or default).strip().lower() in {"1", "true", "yes", "on"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_hex(value: str) -> str:
    raw = value.lower().strip()
    if raw.startswith("0x"):
        raw = raw[2:]
    padded = raw.rjust(40, "0")
    return "0x" + padded[-40:]


def _topic_address(topic: HexBytes | bytes | str | None) -> Optional[str]:
    if topic is None:
        return None
    if isinstance(topic, HexBytes):
        return _normalize_hex(topic.hex())
    if isinstance(topic, bytes):
        return _normalize_hex(topic.hex())
    if isinstance(topic, str):
        return _normalize_hex(topic)
    return None


def _data_words(data: HexBytes | bytes | str | None) -> List[str]:
    if data is None:
        return []
    if isinstance(data, HexBytes):
        payload = data.hex()
    elif isinstance(data, bytes):
        payload = data.hex()
    else:
        payload = data.lower().strip()
        if payload.startswith("0x"):
            payload = payload[2:]
    if not payload:
        return []
    if len(payload) % 64 != 0:
        # Left-pad to whole 32-byte words if needed
        payload = payload.rjust(((len(payload) // 64) + 1) * 64, "0")
    return [payload[i : i + 64] for i in range(0, len(payload), 64)]


def _word_address(word: str) -> str:
    return "0x" + word[-40:]


def _word_int(word: str) -> int:
    return int(word, 16)


def _hex_str(value: HexBytes | bytes | str | None) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, HexBytes):
        return "0x" + value.hex()
    if isinstance(value, bytes):
        return "0x" + value.hex()
    s = str(value).strip()
    if s.startswith("0x"):
        return s.lower()
    return "0x" + s.lower()


@contextmanager
def _session_scope() -> Iterable[Session]:  # type: ignore[misc]
    if Session is None:
        raise RuntimeError("sqlmodel is required for pool watcher operations")
    with next(get_session()) as session:  # type: ignore[call-arg]
        yield session


@dataclass
class PoolDiscovery:
    factory_name: str
    factory_address: str
    pool_address: str
    token0: str
    token1: str
    chain_id: Optional[int]
    block_number: Optional[int]
    tx_hash: Optional[str]
    fee: Optional[int] = None
    stable: Optional[bool] = None
    tick_spacing: Optional[int] = None


@dataclass
class FactorySpec:
    name: str
    env_var: str
    topic: str
    decoder: "EventDecoder"
    stable_default: Optional[bool] = None

    @property
    def address(self) -> Optional[str]:
        raw = (os.getenv(self.env_var) or "").strip()
        if not raw:
            return None
        try:
            from web3 import Web3  # type: ignore

            return Web3.to_checksum_address(raw)
        except Exception:
            return _normalize_hex(raw)


EventDecoder = Callable[["FactorySpec", Dict[str, object], Optional[int]], Optional[PoolDiscovery]]


def _decode_uniswap_v2(spec: FactorySpec, log: Dict[str, object], chain_id: Optional[int]) -> Optional[PoolDiscovery]:
    topics = log.get("topics") or []
    if len(topics) < 3:
        return None
    token0 = _topic_address(topics[1])
    token1 = _topic_address(topics[2])
    if not token0 or not token1:
        return None
    words = _data_words(log.get("data"))
    if not words:
        return None
    pool = _normalize_hex(_word_address(words[0]))
    return PoolDiscovery(
        factory_name=spec.name,
        factory_address=_normalize_hex(spec.address or ""),
        pool_address=pool,
        token0=token0,
        token1=token1,
        chain_id=chain_id,
        block_number=int(log.get("blockNumber")) if log.get("blockNumber") is not None else None,
        tx_hash=_hex_str(log.get("transactionHash")),
    )


def _decode_aerodrome(spec: FactorySpec, log: Dict[str, object], chain_id: Optional[int]) -> Optional[PoolDiscovery]:
    topics = log.get("topics") or []
    if len(topics) < 3:
        return None
    token0 = _topic_address(topics[1])
    token1 = _topic_address(topics[2])
    if not token0 or not token1:
        return None
    words = _data_words(log.get("data"))
    if len(words) < 2:
        return None
    stable: Optional[bool]
    try:
        stable = bool(_word_int(words[0]))
    except Exception:
        stable = spec.stable_default
    pool = _normalize_hex(_word_address(words[1] if len(words) > 1 else words[0]))
    return PoolDiscovery(
        factory_name=spec.name,
        factory_address=_normalize_hex(spec.address or ""),
        pool_address=pool,
        token0=token0,
        token1=token1,
        chain_id=chain_id,
        block_number=int(log.get("blockNumber")) if log.get("blockNumber") is not None else None,
        tx_hash=_hex_str(log.get("transactionHash")),
        stable=stable,
    )


def _decode_uniswap_v3(spec: FactorySpec, log: Dict[str, object], chain_id: Optional[int]) -> Optional[PoolDiscovery]:
    topics = log.get("topics") or []
    if len(topics) < 3:
        return None
    token0 = _topic_address(topics[1])
    token1 = _topic_address(topics[2])
    if not token0 or not token1:
        return None
    words = _data_words(log.get("data"))
    if len(words) < 3:
        return None
    fee = _word_int(words[0])
    tick_spacing = None
    try:
        tick_spacing = int.from_bytes(bytes.fromhex(words[1][-8:]), byteorder="big", signed=True)
    except Exception:
        try:
            tick_spacing = _word_int(words[1])
        except Exception:
            tick_spacing = None
    pool = _normalize_hex(_word_address(words[2]))
    return PoolDiscovery(
        factory_name=spec.name,
        factory_address=_normalize_hex(spec.address or ""),
        pool_address=pool,
        token0=token0,
        token1=token1,
        chain_id=chain_id,
        block_number=int(log.get("blockNumber")) if log.get("blockNumber") is not None else None,
        tx_hash=_hex_str(log.get("transactionHash")),
        fee=fee,
        tick_spacing=tick_spacing,
    )


FACTORY_SPECS: Tuple[FactorySpec, ...] = (
    FactorySpec("uniswap_v2", "UNISWAP_V2_FACTORY_ADDRESS", UNISWAP_V2_PAIR_CREATED_TOPIC, _decode_uniswap_v2),
    FactorySpec("aerodrome_vol", "AERODROME_FACTORY_VOLATILE", AERODROME_PAIR_CREATED_TOPIC, _decode_aerodrome, stable_default=False),
    FactorySpec("aerodrome_stable", "AERODROME_FACTORY_STABLE", AERODROME_PAIR_CREATED_TOPIC, _decode_aerodrome, stable_default=True),
    FactorySpec("uniswap_v3", "UNISWAP_V3_FACTORY_ADDRESS", UNISWAP_V3_POOL_CREATED_TOPIC, _decode_uniswap_v3),
)


def _active_specs() -> List[FactorySpec]:
    specs: List[FactorySpec] = []
    for spec in FACTORY_SPECS:
        addr = spec.address
        if not addr:
            continue
        specs.append(spec)
    return specs


def _ensure_tables() -> None:
    if _truthy_env("SQL_CREATE_ALL_ON_START", "true"):
        try:
            create_db_and_tables()
        except Exception as exc:  # noqa: BLE001
            logger.warning("create_db_and_tables failed: %s", exc)


def _persist_discovery(session: Session, discovery: PoolDiscovery) -> Tuple[DexPool, bool]:  # type: ignore[misc]
    pool_addr = discovery.pool_address.lower()
    existing = session.get(DexPool, pool_addr)
    created = False
    now = _now()
    if existing is None:
        created = True
        existing = DexPool(
            pool_address=pool_addr,
            factory_address=discovery.factory_address.lower(),
            factory_type=discovery.factory_name,
            chain_id=discovery.chain_id,
            token0=discovery.token0.lower(),
            token1=discovery.token1.lower(),
            fee=discovery.fee,
            stable=discovery.stable,
            tick_spacing=discovery.tick_spacing,
            block_number=discovery.block_number,
            tx_hash=(discovery.tx_hash.lower() if discovery.tx_hash else None),
            discovered_at=now,
            updated_at=now,
        )
        session.add(existing)
    else:
        updated = False
        if discovery.factory_name and existing.factory_type != discovery.factory_name:
            existing.factory_type = discovery.factory_name
            updated = True
        if discovery.chain_id is not None and existing.chain_id != discovery.chain_id:
            existing.chain_id = discovery.chain_id
            updated = True
        if discovery.fee is not None and existing.fee != discovery.fee:
            existing.fee = discovery.fee
            updated = True
        if discovery.stable is not None and existing.stable != discovery.stable:
            existing.stable = discovery.stable
            updated = True
        if discovery.tick_spacing is not None and existing.tick_spacing != discovery.tick_spacing:
            existing.tick_spacing = discovery.tick_spacing
            updated = True
        if discovery.block_number is not None and existing.block_number != discovery.block_number:
            existing.block_number = discovery.block_number
            updated = True
        if discovery.tx_hash and existing.tx_hash != discovery.tx_hash.lower():
            existing.tx_hash = discovery.tx_hash.lower()
            updated = True
        if updated:
            existing.updated_at = now
    return existing, created


def _cursor_for(session: Session, spec: FactorySpec, chain_id: Optional[int]) -> DexFactoryCursor:  # type: ignore[misc]
    addr = (spec.address or "").lower()
    cursor = session.get(DexFactoryCursor, addr)
    if cursor is None:
        cursor = DexFactoryCursor(
            factory_address=addr,
            factory_type=spec.name,
            chain_id=chain_id,
            last_block=None,
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(cursor)
        session.commit()
    return cursor


def _fetch_logs(w3, spec: FactorySpec, start: int, end: int) -> List[Dict[str, object]]:
    address = spec.address
    if not address:
        return []
    return list(
        w3.eth.get_logs(  # type: ignore[attr-defined]
            {
                "fromBlock": start,
                "toBlock": end,
                "address": address,
                "topics": [spec.topic],
            }
        )
    )


def _sync_factory(w3, spec: FactorySpec, chain_id: Optional[int], *, backfill: int, block_span: int) -> Tuple[int, int]:
    latest = int(w3.eth.block_number)  # type: ignore[attr-defined]
    new_pools = 0
    scanned_blocks = 0
    with _session_scope() as session:
        cursor = _cursor_for(session, spec, chain_id)
        start_block: int
        if cursor.last_block is not None:
            start_block = cursor.last_block + 1
        else:
            start_block = max(0, latest - backfill)
        if start_block > latest:
            cursor.last_block = latest
            cursor.updated_at = _now()
            session.add(cursor)
            session.commit()
            return new_pools, scanned_blocks

        current = start_block
        chunk = max(1, block_span)
        while current <= latest:
            end = min(current + chunk - 1, latest)
            try:
                logs = _fetch_logs(w3, spec, current, end)
            except ValueError as exc:  # noqa: PERF203
                if "block range" in str(exc).lower() or "more than" in str(exc).lower():
                    if chunk <= 50:
                        logger.warning("get_logs range too large for %s: %s", spec.name, exc)
                        break
                    chunk = max(50, chunk // 2)
                    logger.debug("reducing block span for %s to %s", spec.name, chunk)
                    continue
                logger.warning("get_logs failed for %s: %s", spec.name, exc)
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("get_logs error for %s: %s", spec.name, exc)
                break

            processed = 0
            for log in logs:
                discovery = spec.decoder(spec, log, chain_id)
                if not discovery:
                    continue
                _persist_discovery(session, discovery)
                processed += 1
            if processed:
                session.commit()
            new_pools += processed
            scanned_blocks += end - current + 1
            cursor.last_block = end
            cursor.updated_at = _now()
            session.add(cursor)
            session.commit()
            current = end + 1
    return new_pools, scanned_blocks


def run_pool_watch_loop() -> None:
    specs = _active_specs()
    if not specs:
        raise RuntimeError("No factory addresses configured for pool watcher")
    _ensure_tables()
    try:
        from libs.agentkit_ext.web3_utils import get_web3

        w3 = get_web3()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Web3 connection required for pool watcher: {exc}") from exc

    chain_id = None
    try:
        chain_id = int(w3.eth.chain_id)  # type: ignore[attr-defined]
    except Exception:
        chain_id = None

    interval = int(os.getenv("POOL_WATCH_INTERVAL_SECONDS") or "120")
    backfill = int(os.getenv("POOL_WATCH_BACKFILL_BLOCKS") or "5000")
    block_span = int(os.getenv("POOL_WATCH_BLOCK_SPAN") or "2000")
    once = _truthy_env("POOL_WATCH_ONCE") or _truthy_env("WATCH_POOLS_ONCE")

    logger.info(
        "pool watcher starting: factories=%s interval=%ss backfill=%s span=%s",
        ",".join(spec.name for spec in specs),
        interval,
        backfill,
        block_span,
    )

    while True:
        total_new = 0
        total_blocks = 0
        for spec in specs:
            try:
                new_count, scanned = _sync_factory(w3, spec, chain_id, backfill=backfill, block_span=block_span)
                if new_count:
                    logger.info("%s discovered %s new pools (scanned %s blocks)", spec.name, new_count, scanned)
                total_new += new_count
                total_blocks += scanned
            except Exception as exc:  # noqa: BLE001
                logger.error("error processing factory %s: %s", spec.name, exc)
        if once:
            break
        if interval <= 0:
            break
        logger.debug("pool watcher sleeping %ss (new=%s scanned=%s)", interval, total_new, total_blocks)
        time.sleep(interval)


def list_pools(*, factory: Optional[str] = None, token: Optional[str] = None, limit: Optional[int] = 50) -> List[DexPool]:
    if Session is None or select is None:
        raise RuntimeError("sqlmodel is required to list pools")
    with _session_scope() as session:
        stmt = select(DexPool)
        if factory:
            needle = factory.strip().lower()
            stmt = stmt.where(
                or_(DexPool.factory_address == needle, DexPool.factory_type == needle)
            )
        if token:
            tok = token.strip().lower()
            if tok.startswith("0x") and len(tok) >= 4:
                tok = _normalize_hex(tok)
            stmt = stmt.where(
                or_(DexPool.token0 == tok.lower(), DexPool.token1 == tok.lower())
            )
        stmt = stmt.order_by(DexPool.discovered_at.desc())
        if limit is not None and limit > 0:
            stmt = stmt.limit(int(limit))
        return list(session.exec(stmt).all())


def _pool_style(pool: DexPool) -> str:
    if pool.fee is not None:
        return "v3"
    return "v2"


def _other_token(pool: DexPool, token: str) -> Optional[str]:
    t = token.lower()
    if pool.token0.lower() == t:
        return pool.token1.lower()
    if pool.token1.lower() == t:
        return pool.token0.lower()
    return None


def suggest_routes_for_tokens(
    token_in: str,
    token_out: str,
    *,
    max_routes: int = 8,
) -> List[RoutePlan]:
    if Session is None or select is None:
        return []
    src = _normalize_hex(token_in)
    dst = _normalize_hex(token_out)
    if src == dst:
        return []

    routes: List[RoutePlan] = []
    seen: set[Tuple[Tuple[str, ...], Tuple[Optional[int], ...]]] = set()

    def _add(route: RoutePlan) -> None:
        key = (tuple(route.tokens), tuple(hop.fee for hop in route.hops))
        if key in seen:
            return
        seen.add(key)
        routes.append(route)

    with _session_scope() as session:
        direct_stmt = select(DexPool).where(
            or_(
                and_(DexPool.token0 == src, DexPool.token1 == dst),
                and_(DexPool.token0 == dst, DexPool.token1 == src),
            )
        )
        direct_pools = list(session.exec(direct_stmt).all())
        for pool in direct_pools:
            try:
                if pool.fee is not None:
                    _add(make_route([src, dst], [pool.fee]))
                else:
                    _add(make_route([src, dst]))
            except Exception as exc:  # noqa: BLE001
                logger.debug("skip route due to error: %s", exc)

        if len(routes) >= max_routes:
            return routes[:max_routes]

        pools_in = list(
            session.exec(
                select(DexPool).where(or_(DexPool.token0 == src, DexPool.token1 == src))
            ).all()
        )
        pools_out = list(
            session.exec(
                select(DexPool).where(or_(DexPool.token0 == dst, DexPool.token1 == dst))
            ).all()
        )

        for pool_in in pools_in:
            mid = _other_token(pool_in, src)
            if not mid or mid in {src, dst}:
                continue
            style_in = _pool_style(pool_in)
            for pool_out in pools_out:
                mid_out = _other_token(pool_out, dst)
                if not mid_out or mid_out != mid:
                    continue
                style_out = _pool_style(pool_out)
                if style_in != style_out:
                    continue
                try:
                    if style_in == "v3":
                        if pool_in.fee is None or pool_out.fee is None:
                            continue
                        route = make_route([src, mid, dst], [pool_in.fee, pool_out.fee])
                    else:
                        route = make_route([src, mid, dst])
                    _add(route)
                except Exception:
                    continue
                if len(routes) >= max_routes:
                    return routes[:max_routes]

    return routes[:max_routes]


def routes_as_dict(routes: Sequence[RoutePlan]) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for route in routes:
        out.append(
            {
                "tokens": list(route.tokens),
                "fees": [hop.fee for hop in route.hops],
                "length": len(route.tokens),
            }
        )
    return out

