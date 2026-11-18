# 0009 A promo price must be a real discount

**Decision (Tasos, 18 November 2026).** A promo price must be strictly below both the
prior price (the lowest price of the 30 days before the promo takes effect, or the
admin-entered one) and the current regular price. Otherwise `validate_promo` refuses
it: `NOT_A_DISCOUNT` for the prior price, `NOT_BELOW_REGULAR` for the regular price,
checked in that order.

**Why.** A promo label shows the prior price struck through. A struck-through price at
or below the "discounted" price misleads the customer, and the EU rule on announced
price reductions assumes there is a reduction. The regular-price check matters when a
manual prior price is higher than today's regular price.

**Related rules (same date).**

- `promo_from` is a timestamp. A promo "from today" starts when it is saved, and a
  promo that would start in the past is refused (`STARTS_IN_PAST`). The 30-day window
  ends at the moment the promo takes effect, so a price drop earlier the same day counts.
- A manual prior price an admin enters is always accepted and logged
  (`promo.prior_set`), even when it equals the stored one.
- Setting `is_rx` on a product ends any active or scheduled promo and writes a history
  row for it (`apply_product_change`).
- When `unit_price_required` is set, the promo label also shows the unit price,
  computed from the promo price, under the same size rule as the unit-price template.

The price-display rules as a whole still await confirmation by an accountant, lawyer
or the pharmacists' association (PLAN.md section 12).
