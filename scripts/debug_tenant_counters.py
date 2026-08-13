from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from db.models import Counter, Key, Tenant
from db.session import create_db_and_tables, get_session
from libs.kv import KVStore


def _ts(ms: int) -> str:
    try:
        return datetime.utcfromtimestamp(int(ms)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return str(ms)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Inspect KV rate-limit keys and SQL counters for a tenant."
    )
    ap.add_argument("--tenant", required=True, help="Tenant id (e.g., t-sql-smoke)")
    ap.add_argument(
        "--minutes",
        type=int,
        default=60,
        help="Look back this many minutes in SQL counters (default 60)",
    )
    ap.add_argument(
        "--no-compact",
        action="store_true",
        help="Do not run data:compact-counters before inspecting SQL",
    )
    args = ap.parse_args()

    tenant_id = args.tenant

    print(
        f"[debug] tenant={tenant_id} minutes={args.minutes} no_compact={args.no_compact}"
    )

    # KV inspection
    kv = KVStore()
    rl_prefix = f"rl:tenant:{tenant_id}:chat:"
    rl_keys = sorted(kv.keys(rl_prefix))
    print(f"\n[debug] KV rate-limit keys with prefix '{rl_prefix}': {len(rl_keys)}")
    for k in rl_keys[:20]:
        raw = kv.get(k)
        bucket = k.rsplit(":", 1)[-1]
        print(f"  - key={k} bucket={_ts(bucket)} value={raw}")
    if len(rl_keys) > 20:
        print(f"  ... ({len(rl_keys) - 20} more keys omitted)")

    limits_key = f"broker:tenant:{tenant_id}:limits"
    limits_raw = kv.get(limits_key)
    print(f"\n[debug] KV broker limits key '{limits_key}': {limits_raw!r}")

    # Optional compaction pass (same logic as apps/cli data:compact-counters)
    if not args.no_compact:
        try:
            from apps.cli.main import cmd_compact_counters  # type: ignore[attr-defined]

            print(
                "\n[debug] Running data:compact-counters --force via cmd_compact_counters"
            )
            cmd_compact_counters(SimpleNamespace(force=True))  # type: ignore[arg-type]
        except Exception as exc:
            print(f"[warn] data:compact-counters failed: {exc!r}")

    # SQL inspection
    create_db_and_tables()
    with next(get_session()) as s:  # type: ignore[call-arg]
        print("\n[debug] SQL tenants (id, label, status):")
        for t in s.query(Tenant).all():
            print(f"  - {t.id} | {t.label} | {t.status}")

        print(f"\n[debug] SQL keys for tenant '{tenant_id}':")
        keys = (
            s.query(Key)
            .filter(Key.tenant_id == tenant_id)
            .order_by(Key.created_at.desc())
            .all()
        )
        if not keys:
            print("  (none)")
        else:
            for k in keys[:10]:
                print(
                    f"  - id={k.id} quota={k.quota} expires_at={k.expires_at} created_at={k.created_at}"
                )
            if len(keys) > 10:
                print(f"  ... ({len(keys) - 10} more keys omitted)")

        print(
            f"\n[debug] SQL counters for tenant '{tenant_id}' (last {args.minutes} minutes):"
        )
        since = datetime.now(timezone.utc) - timedelta(minutes=int(args.minutes))
        rows = (
            s.query(Counter)
            .filter(Counter.tenant_id == tenant_id, Counter.bucket_start >= since)
            .order_by(Counter.bucket_start.desc())
            .limit(50)
            .all()
        )
        if not rows:
            print("  (none)")
        else:
            for r in rows:
                print(
                    "  - bucket_start={start} scope={scope} bucket_seconds={bs} "
                    "count={count} model={model}".format(
                        start=r.bucket_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        scope=r.scope,
                        bs=r.bucket_seconds,
                        count=r.count,
                        model=r.model,
                    )
                )


if __name__ == "__main__":
    main()
