# 0007 How job attempts and backoff are counted

**Decision.** `update_job.attempts` counts pushes started; it is incremented on claim.
After a failure with `attempts < 5` the job waits `1, 5, 15, 60` minutes for attempts
1 to 4, and the fifth failure makes it `dead`. The 240-minute step in section 5 is
kept in `BACKOFF` but not reached with 5 attempts. A stale claim (no result within
5 minutes) counts as a used attempt.

**Why.** Section 5 and M3 both say "dead after 5 attempts". Counting on claim means a
gateway that crashes mid-push cannot loop a job forever.

**Matching results to claims.** The gateway echoes the claim's `attempt` in its
result. A result whose `attempt` is not the job's current `attempts` (a late result
from a claim that timed out and was handed out again) is rejected with 409 and changes
nothing, so an old ack can never close a newer push and vice versa.

**One clock.** All times the reconciler compares are a single Python UTC `now` per
transaction, written explicitly by the caller; database `now()` defaults are only a
fallback for audit columns.

**Admin retry.** `admin_retry` creates a fresh job for the tag's current
`desired_hash`; the dead job stays for the record.

**Open.** If Tasos wants all five delays used, `MAX_ATTEMPTS` becomes 6; one constant.
