Migration scaffolding (Alembic)

- Install: `pip install alembic`
- Init: `alembic init db/migrations`
- Configure `db/migrations/env.py` to import `db.models` and target `SQLModel.metadata`.
- Create migration: `alembic revision --autogenerate -m "init schema"`
- Apply: `alembic upgrade head`

Environment
- Uses `SQL_DATABASE_URL` or `DATABASE_URL`.

Config
- `alembic.ini` at repo root points to `db/migrations`.
- `db/migrations/env.py` targets `SQLModel.metadata` from `db.models`.

Run
1) Set `SQL_DATABASE_URL`.
2) Upgrade to head:
   `alembic upgrade head`
