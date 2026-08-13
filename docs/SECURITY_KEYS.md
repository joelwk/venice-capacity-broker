# Security & Key Management

## Parent vs Sub-keys (Venice)

- Parent key (`VENICE_PARENT_KEY`) is used only for admin flows and sub-key issuance.
- Sub-keys must include `consumptionLimit` and `expiresAt` and are per-tenant.
- Store issuance audit trails and rotate sub-keys on anomaly.

## Rotation & Revocation

- Rotate sub-keys daily or upon suspicious usage.
- Revoke immediately when violations occur; re-issue with tighter limits.

```bash
uv run python apps/cli/main.py venice:keys:cleanup --prefix T1 --dry-run
```

## Tenant Hygiene

- Enforce quotas and expiries on every sub-key; never issue unlimited keys.
- Periodically list tenants and check usage drift.

```bash
uv run python apps/cli/main.py broker:tenants:list
```

## Wallet Guidance

- Prefer MPC or smart wallet custody; use a dev EOA only for local tests.
- Maintain a gas buffer on Base for staking and DIEM flows.

## References

- Operations → `./OPERATIONS.md`
- Configuration → `./CONFIGURATION.md`
- API Reference → `./API_REFERENCE.md`


