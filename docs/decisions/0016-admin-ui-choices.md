# 0016 Admin UI choices (M5, part 1)

Workstream E's choices for the server-rendered admin UI (PLAN.md section 9).

**Sessions and login.**
- Passwords: `hashlib.scrypt` with a per-user salt, compared in constant time. An
  unknown email checks a dummy hash, so timing does not reveal which emails exist.
- Signed cookie sessions (`SESSION_SECRET`), cookie `esl_session`, `HttpOnly`,
  `SameSite=Lax`, 12 hours. `Secure` is added per request when the request arrived
  over HTTPS, so the M6 TLS proxy must pass the scheme (uvicorn `--proxy-headers` and
  `--forwarded-allow-ips`).
- The session holds only the user id, a fingerprint of the password hash, the CSRF
  token, flash messages and an upload token. The user row is read on every request:
  deactivating a user or resetting their password ends their sessions at once.
- CSRF is checked on every POST (form field or `X-CSRF-Token` header for HTMX).
- `staff` can view; `admin` can also write and open Settings. The last active admin
  and one's own account cannot be deactivated. Login redirects only to local paths.
- Logout clears the session cookie but cannot revoke a copy of it: sessions are
  stateless signed cookies, valid for 12 hours from last use, unless the user's
  password is reset or the user is deactivated. The M6 runbook says so.
- A request guard (`UiGuard`) runs before any UI request body is read: a
  state-changing request without a session CSRF token, or (except `POST /login`)
  without a logged-in user, gets 403; a body over the upload limit plus 1 MB gets 413,
  checked on `Content-Length` and while streaming. Every UI response carries
  `X-Frame-Options: DENY` and `frame-ancestors 'none'`. The gateway API and `/health`
  are left alone.

**Writes.** Every page writes through the services. Promo edits go through
`core/services/promotions.py` (`edit_promo`, `end_promo`), which follows the
catalogue's lock order and `apply_price_change`. A running promo's start is fixed in the
form (only its price and last day can change, decision 0009); new and scheduled promos
accept dates from today up to five years ahead. The stored manual prior price is
pre-filled only when editing a running promo; otherwise the field is empty with the
previous value as a hint, so "cleared on change" stays the default. A promo edit's
history row has source `manual`, but it does not set `product.manually_changed_at`: that flag compares the
regular price with the ERP, which a promo does not change. The promo end date typed in
the UI is inclusive (`promo_to` is 00:00 Athens the next day). "Prior price saved" is
shown only when `promo.prior_set` was logged. `unbind_tag` locks the product, then the
tag, and refreshes the tag to its unassigned label. Like `bind_tag`, it reads the
current binding before taking the tag lock, so a concurrent rebind can leave the new
product unlocked; decision 0014 explains why that is safe.

**Uploads.** An uploaded import file lives in the system temp directory under a random
token kept in the session, never in the repository. It is deleted on apply or cancel;
files older than one hour are removed on the next upload.

**Text.** All UI text is in `core/app/i18n/el.json` (decision 0015). Importer reasons,
which have no machine codes yet, are mapped to Greek by pattern; a test feeds every
reason the importer can produce through the mapping, and an unknown reason falls back to
a generic Greek message with the English original. Giving `RejectedRow` and
`ImportFormatError` machine codes is a planned follow-up.

**Vendored front end.** htmx 2.0.11 (0BSD), with its licence and SHA-256 in
`core/app/static/vendor/README.md`; no CDN, because the shop box may be offline.
