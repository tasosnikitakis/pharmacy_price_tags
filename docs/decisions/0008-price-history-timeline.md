# 0008 How price history is written and read

**Decision.** Every `price_history` row means "from `valid_from` on, the shelf price is
`effective_cents`". Rows are applied in id order, and each row overrides the timeline
from its `valid_from` onward. Rows may be dated in the future.

Every write to a `price` row, from an import, a manual edit or a promo edit, appends the
rows returned by `core.domain.pricing.history_rows_for_change`:

1. a row at *now* with the price in effect now (`promo` kind if a promo is active);
2. if the promo starts later, a `promo` row at `promo_from`;
3. if the promo ends later, a `regular` row at `promo_to`.

**Why.** The 30-day prior price (section 6) needs to know exactly which price was on
the shelf when. Writing the promo's end together with its start means the history
never says a promo lasted longer than it did. The override rule means a cancelled or
changed promo needs no deletion, which the append-only trigger forbids anyway.

**Consequences.** The lowest prior price counts both regular and earlier promo prices
that were in effect at any moment of `[promo_from - 30 days, promo_from)`. `promo_to`
is exclusive; labels show its previous day as the last day of the promo.
