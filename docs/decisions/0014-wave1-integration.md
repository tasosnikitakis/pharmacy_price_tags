# 0014 Wave 1 integration: when labels are refreshed, and in which lock order

How the catalogue (workstream A), the pricing rules and renderer (B) and the queue (C)
are wired together, so a price change reaches the tags (sections 3, 6 and 7).

**Who refreshes labels, and when.** `refresh_desired_hashes` runs in the same
transaction as the change that caused it, with the same `now`, so a change and its
update jobs commit together or not at all:

- `apply_import`: for the products whose price or fields changed (new products have no
  tags yet; unchanged rows write nothing and render nothing).
- `edit_price`: for the edited product, unless the price was already that value.
- `edit_product`: for the edited product, whenever a field changed.
- `bind_tag` (a minimal helper until workstream E's binding pages exist): for the tag.
  It creates or moves the binding, creates the `template` row for the bound key at its
  current code version if missing, and logs `tag.bound`.
- Discovery of a new tag: its "unassigned" label (unchanged from decision 0013).
- Every gateway claim: the promo boundaries below.

The pipeline defaults to `real_label_pipeline`; each writer takes an optional `pipeline`
so tests can inject one. A refresh that raises an unexpected error rolls the whole
write back (each writer runs in one savepoint).

**Scheduled promo starts and ends are checked on every claim.** A promo whose
`promo_from` or `promo_to` passes changes the label with no write at all. Instead of a
scheduler process or a "next change" column, `POST /jobs/claim` first calls
`refresh_promo_boundaries`, then hands out jobs, so the new label can go out in that
same claim. It refreshes the bound tags whose product's promo *flipped* in
`(tag.desired_hash_changed_at, now]`: it started (`changed_at < promo_from <= now <
promo_to`) or ended (`promo_from <= changed_at < promo_to <= now`). Consequences:

- A promo switches on or off within one claim interval of its time: about a minute
  with the agent's poll interval, longer while the gateway is offline or busy.
- A second claim renders nothing again: the refresh moves `desired_hash_changed_at` to
  the claim's `now` because the hash changed.
- A promo that both started and ended between two claims (the gateway was offline for
  all of it) leaves the label as it was and is not refreshed. Matching any boundary in
  the window would render such tags on every claim forever, since an unchanged hash
  does not move `desired_hash_changed_at`, and moving it without a change would revive
  dead jobs (decision 0007).
- A tag whose label cannot be built at the boundary (a refused promo, a renderer
  error) is retried on each claim until the promo flips back; each attempt is logged.
- Tags with no `desired_hash_changed_at` (no label was ever built) are skipped.
- The query scans the `price` rows with a started promo, joined to `binding` by its
  `product_id` index and to `tag` by key: cheap for a few thousand rows. There is no
  index on `price.promo_from`; if the catalogue grows far beyond that, a partial
  index on `price (promo_from) WHERE promo_cents IS NOT NULL` is the first step.
- Tags another transaction holds are skipped (`SKIP LOCKED`) and picked up on the
  next claim, so a claim never waits for a long import.

**Lock order across catalogue and queue.** Every transaction takes row locks in this
order, each step in id order:

1. `store` (imports only, serialising imports), `FOR NO KEY UPDATE`;
2. `product` rows, `FOR NO KEY UPDATE`;
3. `price` rows, `FOR NO KEY UPDATE`;
4. `tag` rows, `FOR NO KEY UPDATE`;
5. `update_job` rows, `FOR UPDATE`.

Catalogue writers take 1–3, then 4–5 through the refresh at the end. The queue,
discovery, gateway results, `bind_tag` and the claim take only 4–5 and never lock a
store, product or price row; they read those rows without locks, and their foreign-key
checks take `FOR KEY SHARE`, which `FOR NO KEY UPDATE` does not block. So no
transaction waits for a lower step while holding a higher one, and there is no cycle.
The claim takes its tag locks with `SKIP LOCKED`; it can only wait briefly for a job
row that another claim locked (which itself waits for nothing) or for a concurrent
insert of the same render hash. Tested with real connections in
`tests/integration/test_label_refresh_concurrency.py`.

**Tag locks during rendering.** `refresh_desired_hashes` renders while it holds the
tag locks (review note N5 on workstream C's PR). This is kept for v1: an import that
touches many bound products holds their tags until it commits, about one render per
tag. Meanwhile a gateway result for one of those tags waits, and claims skip them. At
a few thousand tags this is seconds at most.

**Tag types come from an admin tool, never from a migration.** Discovery creates a tag
only when its `(vendor, model)` matches a `tag_type` row. `python -m core.tools.tag_types
add --key ... --vendor ... --model ... --width ... --height ... --palette ...` (and
`list`) goes through `core.services.tag_types.add_tag_type`, which is idempotent (an
identical row is left alone), refuses a row that clashes on key or on vendor and model
(it never changes a type in place, because renders and tags refer to it), and logs
`tag_type.added`. Tests create the fake driver's type with the same service.

No migration seeds a tag type, not even the Gicisky one: whether the ordered tags are
three- or four-colour is still open (PLAN.md section 12), and a wrong palette in a
seeded row would render every label wrongly. The shop's type is added with the tool
during M4, once the tags have arrived.
