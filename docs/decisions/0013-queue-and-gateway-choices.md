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

**Short codes.** Four characters from `ACDEFHJKMNPRTUVWXY34679` (23 characters, about
280,000 codes), which leaves out look-alikes: 0/O/Q, 1/I/L, 2/Z, 5/S, 8/B and G/6.

**Robustness.** The core starts while Postgres is down: `/health` reports 503, and the
gateway row is provisioned on the first gateway request instead. In the agent, heartbeat,
discovery and job processing each run in their own guarded step with their own timer,
so a failing step never stops the others.

**Fake driver.** It writes `./fake_tags/<vendor_id>.png`, replacing characters outside
`[A-Za-z0-9._-]` with `_`, because a BLE address with colons is not a valid Windows file
name. It validates PNGs with the standard library, so the gateway still needs no Pillow.

**Wiring.** Imports, price edits, product edits and bindings call
`refresh_desired_hashes`, and each gateway claim re-checks promo starts and ends; see
decision 0014.
