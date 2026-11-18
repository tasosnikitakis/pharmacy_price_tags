# 0002 The `user` table is named `app_user`

**Decision.** PLAN.md section 5's `user` table is `app_user` (model `AppUser`).

**Why.** `user` is a reserved word in Postgres; every query would need quoting, and an
unquoted `SELECT * FROM user` silently returns the current role instead of the table.
