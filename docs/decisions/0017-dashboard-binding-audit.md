# 0017 Dashboard counts, phone binding and the audit walk (M5, part 2)

**Dashboard counts.** Every tag of the store counts, bound and unbound (an unbound tag
shows its "unassigned" label and is up to date once that is displayed). Each tag has
exactly one status (`admin_queries.tag_status`), from its hashes and its newest job
(highest `update_job.id`):

- **No label**: `desired_hash IS NULL`; no label could be built (for example a bound
  product without a price).
- **Up to date**: `desired_hash IS NOT NULL AND displayed_hash = desired_hash`.
- **Failed**: not up to date, and the newest job is `failed` (waiting for a retry) or
  `dead`. **Dead** is also shown on its own and is a subset of failed.
- **Pending**: not up to date and the newest job is neither failed nor dead; in
  practice queued or claimed.

So `up_to_date + pending + failed + no_label = total`, and `bound + unbound = total`.
These cut across all statuses:

- **Unseen for 24 h**: `last_seen_at IS NULL OR last_seen_at < now - 24 h`.
- **Low battery**: `battery_pct < 20`, or with no percentage `battery_mv < 2600`;
  unknown is not low. The thresholds are constants in `core/app/routers/ui_dashboard.py`.
- **Failing labels**, from what the database records (no new column): tags with
  `desired_hash IS NULL`, plus bound tags still behind a promo start or end more than
  5 minutes after it (decision 0014 retries those on every claim).
- **Last import**: `max(import_run.applied_at)`; never-applied previews are ignored. It
  is stale when strictly older than `STALE_IMPORT_DAYS` days, or when there is none.
- **Gateway**: warns when there is no gateway row, or its last heartbeat is missing or
  older than 5 minutes (the agent sends one every 30 s).

The Tags page uses the same statuses, including a "no label" filter.

**Phone binding.**
- The binding steps are plain GET forms, so a USB scanner typing into the EAN field
  works the same as the camera. The camera uses `BarcodeDetector`, or vendored ZXing
  (`@zxing/library` 0.23.0, Apache-2.0) when the browser has none. It is offered only
  over HTTPS.
- Short codes are accepted in any case.
- Rebinding a tag that is already bound always asks first, even for the same
  product. The confirm form carries the binding it was shown, so a binding that
  changed meanwhile is asked again.
- Templates offered: `unit_price` only when the product has a pack size and unit;
  `promo` never for Rx products, and not on tag types without red. The server
  re-checks the choice.
- Creating a product from the phone is admin only. It locks the store row first, like
  an import, so it is serialised with imports (tested on two connections). It writes
  the product, its price and a `manual` history row, and sets `manually_changed_at`.
  It sets no brand.

**Audit walk.** It is read only. A tag is looked up by its short code or, ignoring case,
by its `vendor_id`. Bound tags do not show their short code on screen, so auditing a
bound tag needs a code printed on the tag or another lookup; this is an open question
until the real tags arrive (M4).

**Expired sessions.** A logged-out form POST is refused before its body is read. A
normal POST is redirected to `/login?next=…`, taken from a same-host `Referer`. An HTMX
POST gets 401 with `HX-Redirect`. A logged-in session without a CSRF token still gets
403.
