# 0007 How job attempts and backoff are counted

**Decision (Tasos, 18 November 2026).** A job is `dead` after 6 attempts: 1 initial
push and 5 retries, so every backoff step in section 5 is used. `update_job.attempts`
counts pushes started; it is incremented on claim. After the failure of attempt `n`
(1 to 5) the job waits `BACKOFF[n-1]`, that is 1, 5, 15, 60 and 240 minutes; the sixth
failure makes it `dead` (`MAX_ATTEMPTS = 6`). A stale claim (no result within 5
minutes) counts as a used attempt.

**Why.** The first draft stopped at 5 attempts, which left the 240-minute step unused.
Counting on claim means a gateway that crashes mid-push cannot loop a job forever.

**Matching results to claims.** The gateway echoes the claim's `attempt` in its
result. A result whose `attempt` is not the job's current `attempts` (a late result
from a claim that timed out and was handed out again) is rejected with 409 and changes
nothing, so an old ack can never close a newer push and vice versa.

**One clock.** All times the reconciler compares are a single Python UTC `now` per
transaction, written explicitly by the caller; database `now()` defaults are only a
fallback for audit columns.

**Unknown outcome.** E-paper can update even when its confirmation is lost. After a
stale claim, or a `timeout` or `protocol` failure, the tag's `displayed_hash` is set to
NULL ("unknown"), so the next pass pushes the current label even if it equals what the
core last believed was shown. `not_found` and `rejected` keep `displayed_hash`.

**Admin retry.** `admin_retry` creates a fresh job for the tag's current
`desired_hash`; the dead job stays for the record.
