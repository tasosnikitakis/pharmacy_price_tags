# 0008 How price history is written and read

**Decision.** Every `price_history` row means "from `valid_from` on, the shelf price is
`effective_cents`". Rows are applied in id order, and each row overrides the timeline
from its `valid_from` onward. Rows may be dated in the future.

Every write to a `price` row, from an import, a manual edit or a promo edit, goes
through `core.domain.pricing.apply_price_change`. It clears a manual prior price when
the promo changes, re-validates a promo that has not started yet, and returns the new
state, the events, and the history rows from `history_rows_for_change`:

1. a row at *now* with the price in effect now (`promo` kind if a promo is active);
2. if the promo starts later, a `promo` row at `promo_from`;
3. if the promo ends later, a `regular` row at `promo_to`.

**Why.** The 30-day prior price (section 6) needs to know exactly which price was on
the shelf when. Writing the promo's end together with its start means the history
never says a promo lasted longer than it did. The override rule means a cancelled or
changed promo needs no deletion, which the append-only trigger forbids anyway.

**Concurrency.** Writers lock the `price` row (`SELECT ... FOR UPDATE`) before reading
it, then update it and insert the history rows in the same transaction, so history id
order always matches the order of writes.

**A promo refused later.** A promo accepted when entered can become unlawful before it
starts, for example when an import lowers the price inside its 30-day window below the
promo price. `apply_price_change` then removes the promo and records `promo.refused`,
so history never claims a promo price that was not on the shelf. As a safety net,
`effective_template` renders the regular label with `regular_cents` if a refused promo
is ever found at render time. A tag never shows a promo without a valid prior price.

**Scheduled changes.** `next_label_change` gives the next `promo_from` or `promo_to`;
the reconciler re-computes the affected tags' labels at that moment.

**Consequences.** The lowest prior price counts both regular and earlier promo prices
that were in effect at any moment of `[promo_from - 30 days, promo_from)`. `promo_to`
is exclusive; labels show its previous day as the last day of the promo.
