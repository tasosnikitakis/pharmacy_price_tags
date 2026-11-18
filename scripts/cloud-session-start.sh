#!/bin/bash
# Runs at the start of every Claude Code session (see .claude/settings.json).
# Does nothing outside Claude Code cloud sessions.
# In the cloud: starts the preinstalled Postgres 16, creates the "esl" role and
# database if missing, and installs Python dependencies once pyproject.toml exists.

if [ "$CLAUDE_CODE_REMOTE" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR" || exit 0

# Postgres
# The 'esl' password below is for throwaway Claude Code cloud dev sessions only: the
# database lives in an ephemeral container and is never reachable from outside it.
# Real installs generate their own secrets (docs/decisions/0010).
service postgresql start >/dev/null 2>&1 || true

for _ in $(seq 1 20); do
  su postgres -c "pg_isready -q" >/dev/null 2>&1 && break
  sleep 0.5
done

if ! su postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='esl'\"" 2>/dev/null | grep -q 1; then
  su postgres -c "psql -c \"CREATE ROLE esl LOGIN PASSWORD 'esl' CREATEDB\"" >/dev/null 2>&1 || true
fi

if ! su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='esl'\"" 2>/dev/null | grep -q 1; then
  su postgres -c "createdb -O esl esl" >/dev/null 2>&1 || true
fi

# Throwaway app secrets for this session only (the app refuses to start without them).
if [ -n "$CLAUDE_ENV_FILE" ] && ! grep -q SESSION_SECRET "$CLAUDE_ENV_FILE" 2>/dev/null; then
  {
    echo "export SESSION_SECRET=$(openssl rand -hex 32)"
    echo "export GATEWAY_TOKEN=$(openssl rand -hex 32)"
  } >> "$CLAUDE_ENV_FILE"
fi

# Python dependencies, once the project has been scaffolded
if [ -f pyproject.toml ]; then
  uv sync --all-extras >/dev/null 2>&1 || true
fi

exit 0
