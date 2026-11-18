# pharmacy_price_tags

Self-hosted electronic shelf label (ESL) system for a pharmacy: its own price catalogue, pixel-exact e-paper label rendering, and a hardware-neutral gateway that pushes prices to e-paper tags.

**Status:** planning complete, implementation not started. The build plan is in [`docs/PLAN.md`](docs/PLAN.md).

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

## For coding agents

Start with [`CLAUDE.md`](CLAUDE.md), then [`docs/PLAN.md`](docs/PLAN.md).
