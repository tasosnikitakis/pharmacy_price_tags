# 0010 Secrets are required and have no defaults

**Decision.** `POSTGRES_PASSWORD`, `DATABASE_URL` (which embeds it), `SESSION_SECRET`
and `GATEWAY_TOKEN` have no default anywhere: not in Compose, not in the settings
classes, not in `.env.example`. A missing value stops the process. Compose refuses to
start (`${VAR:?}`), and the core and gateway refuse to start (required pydantic
fields). `SESSION_SECRET` and `GATEWAY_TOKEN` must be at least 32 characters.
Postgres publishes no port; only the core reaches it, over the Compose network.

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
