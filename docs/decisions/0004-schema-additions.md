# 0004 Columns and one table beyond section 5

Section 5 lists 14 tables with their key fields. The initial migration adds what the rest
of the plan needs, so workstreams rarely need migrations of their own:

| Where | Addition | Needed by |
| --- | --- | --- |
| `import_profile` (15th table) | `name`, `mapping` (JSONB), `encoding`, `delimiter` | Section 6 step 1: saved column mappings |
| `import_run` | `profile_id` (FK to `import_profile`), `created_by` | Section 6 |
| `product` | `manually_changed_at` | Section 6: flag manual edits in the next import preview |
| `product` | `pack_qty_milli` instead of `pack_qty` | Pack size in thousandths of `pack_unit` (4.8 g -> 4800), integer like money, so 4.8 g or 7.5 ml packs get an exact unit price |
| `price` | `promo_prior_cents` | Section 6: prior price entered by hand when history is under 30 days |
| `price_history` | `import_run_id`, `created_at` | Tracing a change to its import |
| `tag` | `battery_mv`, `battery_pct` instead of one `battery` | Tags report voltage or percent; unknown until M4 |
| `tag` | `desired_hash_changed_at` | Telling a stale `dead` job from a current one (reconciler) |
| `tag_type` | `key` (e.g. `gicisky-2.9-bwr`) | Stable identifier for render hashes and the gateway API |
| `update_job` | `cancelled` state, `claimed_at`, `error_code`, `created_at`, `updated_at` | Cancel-and-replace; 5-minute claim timeout |
| `gateway` | `driver` | Shown in monitoring |

Rules attached to them:

- `promo_prior_cents` belongs to one promo: it is cleared whenever the promo ends or
  changes, and setting it writes an `event_log` row (`promo.prior_set`).
- A partial unique index allows at most one active (`queued`, `claimed`, `failed`) job
  per tag. A claimed job is never cancelled; see `core/domain/reconciler.py`.
- `price_history` and `event_log` are append-only, enforced by a trigger.
- The migration seeds store 1 (`Europe/Athens`) and advances the `store` id sequence;
  every `store_id` defaults to 1.
- `binding.template_id` fixes the template *key*. Rendering always uses the current
  code version of that key, so bumping a template version needs no binding migration.
- New tags from discovery are created only when `(vendor, model)` matches a
  `tag_type` row; otherwise the report is counted as `ignored` (`TagsSeenResponse`).
