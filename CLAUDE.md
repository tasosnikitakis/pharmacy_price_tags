# CLAUDE.md

This repository builds a self-hosted electronic shelf label system for one pharmacy. The full specification is `docs/PLAN.md`. Read it before doing anything else; it is the source of truth.

## Your role

If you were started without a narrower brief, you are the **lead agent**. Follow "Brief for the lead agent" at the top of `docs/PLAN.md`:

1. Do wave 0 yourself (milestone M0 plus the shared contracts) in one PR.
2. Only then start subagents, one per workstream, using the `implementer` agent with a brief that follows "What every subagent brief must contain".
3. Have every milestone PR checked by the `reviewer` agent before merging.

If you were started by the lead agent with a brief, you are a subagent: do only that brief, inside the paths it gives you.

## Rules that always apply

Section 11 of `docs/PLAN.md` lists them in full. The most important:

- One milestone at a time, small PRs. Every PR passes `ruff`, `mypy` and `pytest`.
- Money is integer cents. Timestamps are UTC in the database, shown in `Europe/Athens`.
- `core/domain/` has no database or HTTP imports. The gateway has no business logic.
- Copy no code from non-commercially licensed projects (OpenEPaperLink in particular).
- Do not add anything listed as out of scope or as M7. Stop and ask instead.
- **This repository is public.** Never commit secrets, real customer data, or files from `data/private/`. Tests use synthetic fixtures or the trimmed sample in `tests/fixtures/`.

## Environment notes

- Cloud sessions: Postgres 16 is preinstalled but not running; the SessionStart hook in `.claude/settings.json` starts it and creates the `esl` database. Use `DATABASE_URL` from the environment.
- Cloud sessions have no Bluetooth and no tags. Everything up to and including M3 and M5 must work with the fake driver. M4 (Gicisky driver) and M6 (store install) run in a local session on the shop's Linux box.
- Development on Windows must keep working: no shell-only tooling in the Python code paths.
