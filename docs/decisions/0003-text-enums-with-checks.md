# 0003 Enumerations are text columns with CHECK constraints

**Decision.** `role`, `pack_unit`, `kind`, `source`, `palette` and job `state` are
`varchar` columns with a CHECK constraint, not Postgres `ENUM` types. The allowed values
live once in `core/db/models.py`; Python-side enums (`JobState`, `PriceKind`, ...) mirror them.

**Why.** Adding a value is a plain constraint change in a migration; Postgres enum
changes cannot run inside a transaction and cannot drop values.
