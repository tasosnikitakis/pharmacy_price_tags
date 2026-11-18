# 0009 A promo price must be below the prior price

**Decision.** `validate_promo` refuses a promo whose price is not below the prior
price (`PromoRefusal.NOT_A_DISCOUNT`).

**Why.** A promo label shows the prior price struck through. A struck-through price
lower than or equal to the "discounted" price misleads the customer, and the EU rule on
announced price reductions assumes there is a reduction.

**Status.** Not stated in section 6; awaiting Tasos's confirmation together with the
other price-display rules in section 12. Removing it is one check and one test.
