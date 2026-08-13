from __future__ import annotations

import argparse
import os

from db.models import Key, Tenant
from db.session import create_db_and_tables, get_session

DEFAULT_SEEDED_QUOTA = int(os.getenv("TEST_TENANT_DIEM_QUOTA", "100"))


def main() -> None:
    p = argparse.ArgumentParser(
        description="Seed a minimal SQL tenant with a dummy subkey"
    )
    p.add_argument("--tenant", required=True, help="Tenant id (e.g., t1)")
    p.add_argument("--label", default="Team A", help="Tenant label")
    args = p.parse_args()

    create_db_and_tables()

    with next(get_session()) as s:  # type: ignore[call-arg]
        t = s.get(Tenant, args.tenant)
        if t is None:
            s.add(Tenant(id=args.tenant, label=args.label, status="active"))
            # Ensure tenant row exists before inserting dependent key
            s.flush()
        s.add(
            Key(
                tenant_id=args.tenant,
                label=args.label,
                subkey="dummy",
                quota=DEFAULT_SEEDED_QUOTA,
                expires_at=None,
            )
        )
        s.commit()
    print(f"seeded tenant {args.tenant} (quota={DEFAULT_SEEDED_QUOTA})")


if __name__ == "__main__":
    main()
