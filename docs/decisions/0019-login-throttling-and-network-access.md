# 0019 Login throttling and network access

**Decision (Tasos).** After repeated failed logins the admin UI adds a growing delay,
tracked per account and per client IP. There is no permanent lockout, and each
throttle event is logged. Separately, for M6: the app must not be reachable from any
customer or guest Wi-Fi.

**Throttling** (`core/app/throttle.py`, used by `POST /login` in
`core/app/routers/ui_auth.py`).
- Two keys. The account key is the normalised email as typed (`normalise_email`),
  whether or not the account exists, so the throttle does not reveal which emails
  exist. The IP key is `request.client.host`. A request is refused if either key is
  throttled.
- For each key, the first `free` failures cost nothing. Each further failure sets the
  next allowed attempt to `now + min(base * 2**(failures - free - 1), max)`, so the
  first delay is `base` and each further failure doubles it. With the defaults the
  delays after the 6th, 7th, ... failure on one email are 2, 4, 8 ... 512 s, then the
  cap of 900 s (15 minutes) from the 15th failure on. Refused attempts are not failures: the password is not checked.
- Failures are forgotten `window` seconds after the last one, but never before a
  running delay ends. A successful login clears the account key, not the IP key. The
  delay is capped and the key always unlocks: no permanent lockout.
- Refuse, don't sleep. While throttled, `POST /login` does not call `authenticate`, so
  no password is hashed. It re-renders the login page with 429, a `Retry-After` header
  (whole seconds, rounded up) and a Greek message giving the wait in seconds, or in
  minutes rounded up from one minute on. CSRF and `UiGuard` run first, as before. The
  attempt that sets a delay still gets the normal 401; the next one gets 429.
- Settings (`core/config.py`, in `.env.example`), all positive:

  | Setting | Default |
  | --- | --- |
  | `LOGIN_THROTTLE_ACCOUNT_FREE` | 5 |
  | `LOGIN_THROTTLE_IP_FREE` | 20 |
  | `LOGIN_THROTTLE_BASE_SECONDS` | 2 |
  | `LOGIN_THROTTLE_MAX_SECONDS` | 900 |
  | `LOGIN_THROTTLE_WINDOW_SECONDS` | 3600 |

  `MAX` must be at least `BASE`, and `IP_FREE` must be strictly greater than
  `ACCOUNT_FREE` (equal values are refused at startup). The IP allowance is the larger
  one because all staff phones in the shop probably share one address
  behind the router's NAT: one person's typos must not lock the whole shop out.
- State is in memory, one `LoginThrottle` per app (`app.state.login_throttle`, the
  `ThrottleDep` dependency), behind one lock, with the request's `NowDep` as its clock.
  The core runs as a single uvicorn process (Dockerfile), so this is enough. A restart
  clears the counters, and more than one worker would need shared state (a table or
  Redis), which v1 does not have. Each key space keeps at most 10,000 keys; when full,
  expired keys go first, then the key whose last failure is oldest. Spraying many
  emails from one address to evict a throttled key trips the IP key first.
- Check then count is not one atomic step: concurrent attempts that all pass the check
  are all counted afterwards, so a burst can get a few more tries than `free`. That is
  acceptable for a delay, which is not a hard limit.

**Logging.** Each event goes to the app log at WARNING and to `event_log` with kind
`auth.throttled`, actor `system` (the request has no logged-in user, and `system` is
one of the documented actors), `at` the request's UTC time, append-only as everywhere
else. The payload is `event` (`throttled` or `refused`), `key` (`account` or `ip`),
`failures`, `delay_seconds` (the new delay, or the time left on a refusal), `ip`, and
`account` as the first 12 hex characters of sha256 of the normalised email. Never the
raw email, never the password. Events logged:
- every time a key becomes throttled, that is every failure that sets a delay;
- the first refusal of each throttle period per key. Further refusals in the same
  period are refused but not logged again, so a flood of requests cannot flood
  `event_log`: at most two rows per key per delay.

The account hash is unsalted, so an admin can check whether a known email was
targeted; it hides the email from a casual reader of the log, not from someone who
guesses it.

**For the M6 runbook.**
- **Network rule (Tasos): the app must not be reachable from any customer or guest
  Wi-Fi. Access is only from the staff network or over Tailscale.** This, not login
  throttling, is the main protection of the admin UI; throttling only slows guessing
  by someone who can already reach the login page. The runbook must include a check,
  made from a device on the guest Wi-Fi, that the app (its HTTPS address and the core
  port) is unreachable.
- Behind the HTTPS proxy, `request.client.host` is the proxy's address, so every
  request would share one IP key. Run uvicorn with `--proxy-headers` and
  `--forwarded-allow-ips` set to the proxy's address only, so the client address (and
  the scheme, decision 0016) comes from the proxy's headers and cannot be forged by a
  client. The Dockerfile is not changed yet.
- A restart of the core clears the throttle counters.
- Someone who knows a staff email and can reach the login page can keep that account
  throttled by sending one wrong password each time its delay ends (every 15 minutes
  once capped). Each key still unlocks, so this is not a lockout, and the network rule
  above is what keeps such a person out. The runbook's remedy for a staff member who is
  locked out this way: find the source in `event_log` (`auth.throttled`, its `ip`),
  deal with it, and restart the core, which clears the counters.
- If the database is down while a throttle event is written, that request gets a 500
  instead of 429 and the period's `event_log` row is lost; the WARNING line in the app
  log is written first, so the event is still recorded there.
