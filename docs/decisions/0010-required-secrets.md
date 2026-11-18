# 0010 Secrets are required and have no defaults

**Decision.** `POSTGRES_PASSWORD`, `DATABASE_URL` (when it embeds one), `SESSION_SECRET`
and `GATEWAY_TOKEN` have no default anywhere: not in Compose, not in the settings
classes, not in `.env.example`. A missing value stops the process. Compose refuses to
start, and the core and gateway refuse to start (required pydantic fields).
Compose passes the three secrets as Compose secrets: files under `/run/secrets`, read
by Postgres (`POSTGRES_PASSWORD_FILE`) and by the core settings (`secrets_dir`), so
they never appear in `docker inspect` or the container environment. The core's
`DATABASE_URL` carries no password; the core adds it from the `database_password`
file. Environment variables still work outside Compose and take precedence. `SESSION_SECRET` and `GATEWAY_TOKEN` must be at least 32 characters; an empty database password is rejected.
Postgres publishes no port; only the core reaches it, over the Compose network.

**Outside Docker** (cloud sessions, the CI pytest job, Windows development) there is
no secret file: a `DATABASE_URL` with the password in it is used exactly as given.
With a secret file, the core reads it itself (`core.config.read_secret_file`) and
removes only trailing newlines, exactly as the Postgres image does, so both always see
the same password. A secret with any other leading or trailing whitespace (spaces,
tabs, a `\r` from a Windows editor) is refused at startup, from a file or from the
environment, because it is almost certainly a mistake and the two sides could disagree.
SQLAlchemy URL-escapes the password when it renders the URL, so `@`, `/`, `:` and `%`
are safe. Giving the password both ways is an error.

**Why.** The repository is public. Any default would be a published password that a
forgotten `.env` would silently put into production.

**Exceptions.** CI uses fresh random values per run, and a password-less Postgres
service container that only that job can reach. `scripts/cloud-session-start.sh`
keeps the `esl` password for throwaway cloud dev sessions only.

**For the M6 runbook.** On the shop box, before the first `docker compose up`:

```bash
cp .env.example .env
python3 -m core.gen_secrets >> .env   # or: openssl rand -base64 36, once per secret
chmod 600 .env
```

Put the same `GATEWAY_TOKEN` in the gateway's systemd environment file, also with mode
600. Keep a copy of `.env` with the off-site backups: a restore needs the same
`POSTGRES_PASSWORD`, and rotating `GATEWAY_TOKEN` means updating both sides.
