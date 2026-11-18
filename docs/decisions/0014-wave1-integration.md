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
  error) keeps matching, so it is retried on every claim. At a start that lasts until
  a label is built or the promo ends; at an end it lasts until a label is built, for
  example after a write fixes the product (that write's own refresh then moves
  `desired_hash_changed_at`). Only the first failure per tag is logged as a warning;
  repeats are logged at debug level until the tag's label is built again (an
  in-process set, so a restart logs one warning again).
- The check never stops a claim. It runs in a savepoint; if refreshing all matching
  tags at once fails, each is refreshed in its own savepoint, so one bad tag does not
  hold the others back. Any other failure is rolled back, logged with its exception
  type, and the claim hands out jobs as usual.
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
2. `product` rows: `FOR NO KEY UPDATE` by catalogue writers, `FOR SHARE` by `bind_tag`;
3. `price` rows, `FOR NO KEY UPDATE`;
4. `tag` rows, `FOR NO KEY UPDATE`;
5. `update_job` rows, `FOR UPDATE`.

Catalogue writers take 1–3, then 4–5 through the refresh at the end. `bind_tag` takes
2 (the product it binds to and, on a rebind, the one the tag leaves, in id order), then
4–5. The queue, discovery, gateway results and the claim take only 4–5 and never lock
a store, product or price row; they read those rows without locks, and their
foreign-key checks take `FOR KEY SHARE`, which `FOR NO KEY UPDATE` does not block. So
no transaction waits for a lower step while holding a higher one, and there is no
cycle. (A rebind re-reads the binding under the tag lock; if a concurrent rebind moved
the tag meanwhile, the product it now leaves is not locked, because that would be a
product lock after a tag lock. It needs none: a writer on that product locks the tag
and waits for the bind.)

**A bind waits for an open write on its product.** `FOR SHARE` conflicts with the
writers' `FOR NO KEY UPDATE`. Without it, a bind could render the last committed price
while an edit of that product was still open, and the edit's refresh, which only sees
committed bindings, would miss the new tag: the tag kept the old price until the next
write. Now a bind during an open price, product or import write on that product waits
for it to commit and renders the new price; a write that starts after a bind waits for
the bind, then refreshes the newly bound tag itself. Binds to the same product do not
wait for each other (`FOR SHARE` is shared); binds of the same tag are serialised by the
tag lock.
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
