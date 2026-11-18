# 0013 Queue and gateway implementation choices

Workstream C's choices while implementing M3 (sections 3, 5 and 8).

**Gateway token.** The core stores only `scrypt$n$r$p$salt$hash` of `GATEWAY_TOKEN` in
`gateway.token_hash` and compares in constant time. On startup the core creates or
updates the single v1 gateway row from `GATEWAY_TOKEN`. A successful check is cached in
process, so scrypt does not run on every request. A wrong or missing token gets 401, and
the token is never logged.

**Claiming and locks.** Claims use `SELECT … FOR UPDATE SKIP LOCKED`, so two gateways
(or two requests) never get the same job and a claim never waits. Retries that are due
and stale claims are processed in the same transaction before claiming. Locks are
always taken tags first, then jobs, so a reconcile and a claim cannot deadlock; a
reconcile waits for a claim's lock and never cancels the claimed job.

**Retries keep their history.** A job re-queued by `due_retries` keeps its last error
for monitoring and is held by no gateway; the next claim clears the error. A finished
job (acked, failed or dead) keeps the id of the gateway that pushed it.

**Unbound tags.** A tag with no binding gets the `unassigned` label as its
`desired_hash`, so a newly discovered tag is queued at once to show its short code.

**Short codes.** Four characters from an alphabet without look-alikes (no 0/O, 1/I/L).

**Fake driver.** It writes `./fake_tags/<vendor_id>.png`, replacing characters outside
`[A-Za-z0-9._-]` with `_`, because a BLE address with colons is not a valid Windows file
name. It validates PNGs with the standard library, so the gateway still needs no Pillow.

**Not yet wired (integration).** `core.services.tags.refresh_desired_hashes` must be
called after imports, price edits and binding changes, and re-run at the next scheduled
promo start or end (`next_label_change`). The lead wires these when A and C are merged.
