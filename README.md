# pharmacy_price_tags

Self-hosted electronic shelf label (ESL) system for a pharmacy: its own price catalogue, pixel-exact e-paper label rendering, and a hardware-neutral gateway that pushes prices to e-paper tags.

**Status:** wave 0 (M0 skeleton and shared contracts) in progress. The build plan is in [`docs/PLAN.md`](docs/PLAN.md).

## What it does

- Keeps a product catalogue with current prices, promotions and a full price history.
- Imports price lists exported from the pharmacy's ERP as CSV, showing a diff before applying.
- Renders shelf labels for each tag's exact resolution and colours (black/white/red/yellow).
- Binds tags to products from a phone by scanning barcodes.
- Queues label updates, confirms delivery and retries failures.
- Applies Greek and EU price-display rules, including unit prices and the 30-day prior price on promotions.

## How it runs

Two processes on one small Linux box inside the pharmacy:

- **core**: FastAPI web app with Postgres. Catalogue, rendering, update queue, admin UI.
- **gateway**: talks to the tags through a pluggable driver (fake driver for development, Gicisky Bluetooth tags for the proof of concept, LAN base stations later).

## Stack

Python 3.12 (`uv`), FastAPI, Postgres 16, SQLAlchemy 2 and Alembic, Pillow, `bleak`, Jinja2 with HTMX.

## Development

```bash
uv sync --all-extras
cp .env.example .env
uv run python -m core.gen_secrets >> .env   # required secrets; see docs/decisions/0010
# for running outside Compose, also set DATABASE_URL to your own Postgres 16
uv run alembic upgrade head
uv run pytest
uv run ruff check . && uv run mypy
docker compose up --build     # core + Postgres; GET http://localhost:8000/health
```

## For coding agents

Start with [`CLAUDE.md`](CLAUDE.md), then [`docs/PLAN.md`](docs/PLAN.md).
