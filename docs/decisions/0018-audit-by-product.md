# 0018 Audit by product barcode (M5)

**Decision (Tasos).** A bound tag shows its label, not its short code, so on the shelf
it is audited by the product's barcode. Decision 0017 left this open; this settles it.
Lookup by a hardware ID printed on the tag stays per driver and waits for M4.

**One field, decided by the input.** The audit page keeps one code field, empty and
focused at the top after each lookup. The input, with surrounding spaces trimmed, is
looked up in this order:

1. If it passes the EAN rules (`core.domain.catalogue.normalise_ean`, the same rules as
   import and bind: EAN-13 or EAN-8, spaces ignored, valid check digit, not all zeros;
   12-digit UPC-A refused) and a product of the store has that EAN, it is a **product
   lookup**. This wins even if a tag's `short_code` or `vendor_id` is the same string.
2. Otherwise it is the existing **tag lookup** (decision 0017): the `short_code` in any
   case, else the `vendor_id` ignoring case. So an EAN-shaped input that matches no
   product but matches a tag still shows that tag.
3. Nothing found: an input that passed the EAN rules says no product has that barcode
   (and no tag that code); any other input keeps the existing "no tag with code" message.

An input that looks like an EAN but fails the rules (a wrong check digit, 12 digits) is
not an EAN, so it is only a tag lookup. Generated short codes are four characters from the tag
alphabet, so they never pass the EAN rules.

**What a product lookup shows.** The product's name, EAN and regular price (and the
promo price while a promo runs), then every tag bound to it, by short code. For each:

- **Should show**: the desired label (`desired_hash`), enlarged, from the render store
  through the Tags page's `/tags/{id}/label/desired.png`, as the tag lookup already did,
  with the price text built from the current price.
- **Last confirmed**: `displayed_hash`. The same as desired says so; a different hash
  whose PNG is still in the render store is shown (`/tags/{id}/label/displayed.png`); a
  different hash with no PNG says the image is no longer kept; NULL says nothing has
  been confirmed. Today render rows are never deleted (update jobs reference them), so
  "no longer kept" only appears if a later pruning removes a render; the page does not
  assume they live forever.
- Its status, with the same statuses and Greek labels as decision 0017 and the Tags page.
- When the gateway last saw it (Athens time, Greek format), battery and signal.

A product with no bound tags says so and links to `/bind?ean=`. The tag lookup shows the
same per-tag block, now including "last confirmed".

**Queries.** A product lookup reads a fixed number of queries, whatever the number of
tags: the product with its price, its bound tags with type, template and newest job
(`admin_queries.product_tag_rows`, the same statement as `tag_rows`), its price history,
and which displayed hashes are in the render store (`admin_queries.stored_renders`). A
test checks the count is the same for one tag and for four. The preview text is built
once per tag type and template.

**Unchanged.** Read only, staff and admins, no new POST route. The camera scanner on
this page now also reads EAN-13 and EAN-8.
