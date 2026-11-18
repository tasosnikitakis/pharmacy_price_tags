# ESL System: Build Plan for the Coding Agent

Version of 18 November 2026, updated the same day with Tasos's answers on job attempts and promo rules. This file is the source of truth for the build.

## Brief for the lead agent

You are the lead agent for this build. You own the contracts, the integration and the reviews; subagents implement one workstream each, in parallel, inside paths they own. The target is the local deployment decided in section 2, and the build is complete when milestones M0–M6 in section 10 are accepted.

**Step 1: do this yourself before starting any subagent (wave 0)**

Parallel work is only safe once the shared contracts exist. Deliver milestone M0 plus these, in one PR:

1. All tables from section 5 as SQLAlchemy models with the initial Alembic migration, so subagents rarely need migrations of their own.
2. `shared/`: request and response models for the gateway API in section 3.
3. `gateway/drivers/base.py`: the `TagDriver` interface and its result types from section 8.
4. `core/render/`: the `LabelData` type and the signature `render(label: LabelData, tag_type: TagType) -> Render`.
5. `core/domain/`: function signatures for pricing rules and the reconciler, with docstrings and failing placeholder tests.

After wave 0, changes to any of these files go through you. A subagent that needs a contract change asks; it does not edit.

**Step 2: workstreams and subagents**

Start each workstream with the `implementer` agent (`.claude/agents/implementer.md`) and a brief written by you. Use the `reviewer` agent (`.claude/agents/reviewer.md`) for every review.

| Subagent | Owns (paths) | Delivers | Depends on | Wave |
| --- | --- | --- | --- | --- |
| A. Catalogue and import | `core/importers/`, catalogue parts of `core/domain/`, their tests | M1 | Wave 0 | 1 |
| B. Renderer | `core/render/`, pricing rules in `core/domain/`, golden images | M2 | Wave 0 | 1 |
| C. Queue and gateway | Reconciler in `core/domain/`, gateway API router, `gateway/agent.py`, `gateway/drivers/fake.py` | M3 | Wave 0 | 1 |
| D. Protocol research | `docs/gicisky-protocol.md` only; no driver code yet | First half of M4 | Nothing | 1 |
| E. Admin UI | `core/app/` routers, templates, static files, login and roles | M5 | A, B and C merged | 2 |
| F. Gicisky driver | `gateway/drivers/gicisky.py`, `gateway/tools/` | Second half of M4 | D, C, and the physical tags | 3 |
| G. Store install | `docs/runbook.md`, systemd unit, HTTPS setup, backup and restore scripts | M6 | E, F, and the box | 3 |
| R. Reviewer | Nothing; read-only | An acceptance report per milestone | The PR under review | Every wave |

**Step 3: waves and gates**

- **Wave 1 (parallel, no hardware):** A, B, C and D. Gate: you run one end-to-end test with the fake driver: import a CSV, change a price, and confirm a new PNG appears for exactly the bound tags.
- **Wave 2:** E builds the pages of section 9 on top of the merged core. Gate: the phone binding flow works against the fake driver.
- **Wave 3 (needs hardware and Tasos):** F, then G. Gate: the M4 and M6 acceptance criteria on the real tags and the real box. Wave 3 runs in a local Claude Code session on the shop's Linux box, because cloud sessions have no Bluetooth.

Waves 0–2 run in cloud sessions and need nothing from Tasos except a sample export file. Wave 3 cannot start until the tags have arrived.

**What every subagent brief must contain**

- The goal in one sentence and the milestone it delivers.
- The sections of this document it must read, always including section 11.
- The paths it owns and the contracts it consumes but must not change.
- The acceptance criteria from section 10, copied in full.
- What to return: a PR, a list of which criteria were verified and how, and any open issues or assumptions.

**Review**

- Each milestone PR is checked by the reviewer subagent, started fresh with only this document and the PR, never by the subagent that wrote it.
- The reviewer runs the test suite, checks each acceptance criterion and the rules in section 11, and reports pass or fail per criterion.
- You merge only on a full pass, and you fix integration problems between workstreams yourself.

**When to stop and ask Tasos**

- A needs a real ERP export to finish M1. It builds against a synthetic Greek fixture first; M1 is accepted only once Tasos has added a trimmed real sample as `tests/fixtures/erp_sample.csv` and it imports correctly.
- Any blocking item in section 12.
- Before F starts: confirm the tags have arrived and which variant they are.
- Before G starts: confirm which box is used and where backups go.
- Anything that would add scope beyond section 1.

## 1. Purpose and scope

We are building a standalone electronic shelf label (ESL) system for one pharmacy: its own price catalogue, label rendering, and delivery of label images to e-paper tags. It has no ERP connection in v1; prices arrive by CSV import or manual edit.

**In scope for v1**

- Product catalogue with current price, promo price and an append-only price history.
- CSV/Excel-export import with a preview diff before applying.
- Label templates rendered to pixel-exact bitmaps for each tag type.
- Binding a tag to a product from a phone.
- An update queue that pushes images to tags, confirms delivery and retries.
- Monitoring: tag last seen, battery where the tag reports it, failed updates, stale catalogue.
- A hardware-neutral driver layer with two drivers: a fake driver (writes PNGs) and a Gicisky BLE driver.

**Out of scope for v1**

- Live ERP/POS integration, e-shop sync, multi-store and multi-tenant features.
- Cloud hosting (see section 2; the design keeps it possible).
- Custom tag firmware or custom hardware.
- LED pick-to-light, NFC, customer-facing QR flows.

**Hardware context**

- Proof of concept: 2 Gicisky 2.9" BLE tags (296×128, three or four colours), ordered from AliExpress, arriving in 1–3 weeks. No gateway; a Bluetooth radio within about 10 m talks to them directly.
- Later production candidates: Opticon EE tags with an EBS-50 base station, or Zkong tags with their gateway. Both are driven over a LAN API, which is why the driver layer must not assume BLE.

## 2. Deployment decision: local for v1, built to move to cloud

Decided on 18 November 2026: v1 runs locally, on one small Linux box inside the pharmacy. It is built as two separate processes so the core can later move to the cloud without a rewrite.

A purely cloud system is not possible: the tags are reached only by a radio that is physically in the shop (a Bluetooth adapter now, a vendor base station on the LAN later). The real choice is where the catalogue, renderer and queue live.

| Factor | All local (one box in the shop) | Cloud core + local agent |
| --- | --- | --- |
| Local hardware needed | Yes | Yes, still needed for the radio |
| Internet outage | Everything keeps working | Price changes wait until the link returns |
| Things to deploy and secure | One box, private network only | Two deployments, public login, TLS, tenant isolation |
| Access from outside the shop | Needs a private VPN such as Tailscale | Built in |
| Backups | Must be set up: nightly dump to off-site storage | Managed database backups |
| Several stores or selling it as a product | Poor fit | Required |
| Running cost | One-off box, roughly €100–200 | Hosting fees plus the same box |

**Why local wins for v1:** one store, a dataset of a few thousand rows, and shelf prices that should not depend on the internet connection. Cloud adds a second deployment and a public attack surface while removing nothing from the shop.

**When to move the core to the cloud:** a second store, or a decision to offer this to other pharmacies. That is milestone M7.

**Design constraints that keep the move cheap** (follow these from day one)

- Core and gateway are separate processes that talk only over HTTP with a bearer token. No shared files, no shared database access.
- The gateway always dials out to the core; the core never calls the gateway.
- The gateway fetches label images from the core by content hash over HTTP.
- Postgres from the start, all configuration through environment variables.
- Every table carries a `store_id`, fixed to one store in v1.

**The box:** any small Linux machine with Bluetooth 5, placed within about 10 m of the pilot shelf (Raspberry Pi 5 or a mini-PC). Development on a Windows or Linux laptop must also work.

## 3. Architecture

Two processes: a **core** that owns all data and decisions, and a **gateway** that owns the radio. Tags are driven by desired state: the core decides what each tag should show, the gateway makes it so and reports back.

```
CSV import / manual edit
          |
          v
+--------------------- core (FastAPI) ----------------------+
|  catalogue + price history   ->  price rules              |
|  bindings (tag <-> product <-> template)                  |
|  renderer  ->  image store (by content hash)              |
|  reconciler: desired_hash != displayed_hash -> update job |
|  admin UI, phone binding page, monitoring                 |
+------------------------------+----------------------------+
                               ^  HTTP, bearer token, gateway dials out
                               |
+------------------------- gateway -------------------------+
|  polls for jobs, downloads image by hash                  |
|  driver interface: Fake | GiciskyBLE | (later) Opticon... |
|  reports job results and tags seen (RSSI, battery)        |
+------------------------------+----------------------------+
                               |  BLE now, LAN API later
                               v
                           e-paper tags
```

**Core responsibilities**

- Single source of truth for products, prices, bindings and tag state.
- Computes, for every bound tag, the label it should show and a `desired_hash` for it.
- Creates an update job whenever `desired_hash` differs from the tag's `displayed_hash`.
- Serves the admin UI and the gateway API.

**Gateway responsibilities**

- Claims jobs, pushes images through the configured driver, reports success or failure.
- Scans for tags and reports what it sees.
- Holds no business logic and no persistent data beyond a small cache of downloaded images.

**Gateway API (core side)**

| Endpoint | Purpose |
| --- | --- |
| `POST /gateway/v1/jobs/claim` | Returns up to N queued jobs for this gateway and marks them claimed |
| `GET /gateway/v1/renders/{hash}` | Returns the label image for a job |
| `POST /gateway/v1/jobs/{id}/result` | Reports `acked` or `failed` with an error code |
| `POST /gateway/v1/tags/seen` | Batch report of discovered tags: vendor id, model, RSSI, battery |
| `POST /gateway/v1/heartbeat` | Gateway liveness and version |

## 4. Tech stack and repository layout

One language, Python, for both processes: it has the most dependable cross-platform Bluetooth library and gives pixel-level control over rendering.

| Concern | Choice | Reason |
| --- | --- | --- |
| Language | Python 3.12, managed with `uv` | One language for core, renderer and gateway |
| Web framework | FastAPI | Typed request models, generated OpenAPI for the gateway API |
| Database | Postgres 16, SQLAlchemy 2, Alembic | Same engine locally and on Railway later |
| Rendering | Pillow | Exact control of pixels and palettes at 296×128 |
| Bluetooth | `bleak` | Works on Linux and Windows |
| Admin UI | Server-rendered Jinja2 templates with HTMX | No separate front-end build |
| Barcode scanning | Browser `BarcodeDetector` API with a `zxing-js` fallback | Runs on staff phones without an app |
| Quality | `pytest`, `ruff`, `mypy --strict` on core logic | Run in CI (GitHub Actions) on every PR |
| Packaging | Docker Compose for core and Postgres; gateway runs natively under systemd | Bluetooth inside containers is unreliable |

Repository root:

```
core/
  app/            FastAPI app, routers, Jinja templates, static files
  domain/         catalogue, pricing rules, bindings, reconciler (no I/O)
  render/         templates, palettes, fonts, hashing
  db/             SQLAlchemy models, Alembic migrations
  importers/      CSV import profiles and diff
gateway/
  agent.py        claim/push/report loop
  drivers/        base.py, fake.py, gicisky.py
  tools/          push_png.py and other manual hardware tools
shared/           gateway API models used by both sides
tests/            unit, golden images, end-to-end with the fake driver
  fixtures/       synthetic data and the trimmed ERP sample
docs/             PLAN.md, decisions/, gicisky-protocol.md, runbook.md
scripts/          session and maintenance scripts
data/private/     real pharmacy files; gitignored, never committed
.claude/          settings.json (SessionStart hook), agents/
docker-compose.yml
pyproject.toml
```

`core/domain/` must stay free of database and HTTP imports so the pricing rules and reconciler can be unit-tested directly.

## 5. Data model

Money is stored as integer cents, timestamps as UTC, and every table carries `store_id`.

| Table | Key fields | Notes |
| --- | --- | --- |
| `store` | id, name, timezone | One row in v1 (`Europe/Athens`) |
| `user` | id, email, password_hash, role | Roles: `admin`, `staff` |
| `product` | id, ean (unique per store), name, brand, pack_qty_milli, pack_unit, unit_price_required, is_rx, active | `pack_unit` in g, ml or pieces; `pack_qty_milli` in thousandths of it (4.8 g = 4800) |
| `price` | product_id, regular_cents, promo_cents, promo_from, promo_to, updated_at | Current state, one row per product |
| `price_history` | id, product_id, effective_cents, kind, valid_from, source | Append-only; `source` is `import`, `manual` or `promo` |
| `tag_type` | id, vendor, model, width, height, palette | Example: gicisky, 2.9", 296×128, `BWR` |
| `tag` | id, vendor_id (unique), tag_type_id, short_code, last_seen_at, rssi, battery, displayed_hash, desired_hash | `vendor_id` is the BLE address or vendor serial |
| `template` | id, key, version, tag_type_id | Code-defined; the row records what exists |
| `binding` | tag_id (unique), product_id, template_id, bound_at, bound_by | One product per tag; a product may have several tags |
| `render` | hash (PK), tag_type_id, png, created_at | Content-addressed image store |
| `update_job` | id, tag_id, render_hash, state, attempts, next_attempt_at, error, gateway_id | See state machine below |
| `import_run` | id, profile, filename, started_at, applied_at, counts | One row per CSV import |
| `gateway` | id, name, token_hash, last_heartbeat_at, version | One row in v1 |
| `event_log` | id, at, actor, kind, payload | Audit trail for price edits, bindings, imports |

**Update job states**

```
queued -> claimed -> acked
             |
             +-> failed -> queued   (retry with backoff: 1, 5, 15, 60, 240 min)
                      |
                      +-> dead      (after 6 attempts: 1 initial + 5 retries; shown in monitoring)
queued | failed -> cancelled        (replaced by a newer desired_hash)
```

- A job is `acked` only when the driver confirms the tag accepted the full image. The core then sets `tag.displayed_hash` to that job's `render_hash` (not to the tag's current `desired_hash`), so a newer label is queued on the next pass.
- A claimed job with no result after 5 minutes returns to `queued`; the lost push counts as an attempt. Each result carries the attempt it reports on, and a late result for an earlier claim is rejected.
- If a newer `desired_hash` appears while a job is queued or failed, the old job is `cancelled` and replaced, so a tag never receives an outdated price. A `claimed` job is never cancelled, because the gateway may be pushing it; the newer label waits for its result.
- When a push may have landed but was not confirmed (stale claim, timeout or protocol error), `tag.displayed_hash` becomes unknown and the current label is pushed again.
- An admin can retry a `dead` job from the Tags page; this creates a fresh job.
- Decisions 0007 and 0004 record the details.

## 6. Catalogue, import and price rules

The catalogue is the only thing tags render from. CSV import and manual edits are interchangeable feeders into it, and a live ERP adapter can be added later as a third.

**CSV import**

1. Upload a file and pick an import profile (saved column mapping: EAN, name, price, optional brand, pack size, unit).
2. Parse with tolerance for Greek exports: UTF-8 and Windows-1253 encodings, `;` or `,` delimiters, decimal comma.
3. Show a preview diff, matched on EAN: new products, price changes (old → new), unchanged, rows rejected with a reason, and catalogue products missing from the file.
4. Apply in one transaction. Write `price_history` rows for every change. Never delete products; missing ones are only listed.
5. The reconciler then queues updates for affected tags only.

**Manual edit**

- Editing a price writes `price_history` with source `manual` and marks the product as manually changed since the last import.
- The next import preview flags these products so the difference from the ERP is visible.

**Staleness**

- Monitoring warns when the last applied import is older than a configurable number of days (default 7).

**Price display rules** (from research on Greek and EU price-display law; Tasos to confirm, see section 12)

| Rule | Behaviour in the system |
| --- | --- |
| Price shown with VAT, two decimals and the euro sign | Catalogue prices are VAT-inclusive; format as `12,90 €` |
| Unit price where required | If `unit_price_required`, compute price per kg, litre or piece from the pack size (`pack_qty_milli`, thousandths of `pack_unit`) and use a unit-price template. Promo labels for such products also show the unit price, computed from the promo price |
| Unit price at least 70% of the selling price's character size, both bold | Enforced by the template; covered by a test that measures rendered glyph heights |
| A discount must show the lowest price of the previous 30 days | Promo templates show a prior price: the lowest price in effect in the 30 days before the promo takes effect (`promo_from`), up to that moment, so a drop earlier the same day counts |
| A promo must be a real reduction | The promo price must be strictly below both the prior price and the current regular price; otherwise the promo is refused (decision 0009) |
| Promo start | `promo_from` is a timestamp, not a date. A promo "from today" starts when it is saved; a `promo_from` in the past is refused |
| Less than 30 days of history | A promo cannot be activated until an admin enters the prior price by hand. An entered prior price is always accepted and logged, even if unchanged. A stored one is cleared when the promo changes or ends, unless the admin enters one in the same edit |
| Prescription medicines must not be promoted | Products with `is_rx` cannot be given a promo price or a promo template. Setting `is_rx` ends any active or scheduled promo and writes a history row for it |

A promo label is never rendered without a valid prior price. Every write to a price, and every product edit that affects one, goes through `core.domain.pricing` (`apply_price_change`, `apply_product_change`), which applies these rules and writes the price history; decision 0008 describes how history is read. These checks live in `core/domain/` and have unit tests.

## 7. Rendering and label templates

Labels are drawn directly with Pillow at the tag's native resolution and palette, so what the preview shows is exactly what the tag receives.

**Pipeline**

1. Build a `LabelData` object from product, price and rules (name, brand, formatted price, unit price, prior price, promo flag).
2. Compute `hash = sha256(template key + template version + tag_type + LabelData)`. If a `render` row exists, reuse it.
3. Otherwise draw onto an indexed image whose palette is exactly the tag's colours (`BW`, `BWR` or `BWRY`).
4. Store the PNG in `render`. Driver-specific packing into the tag's wire format happens in the driver, not here.

**Rendering rules**

- No anti-aliasing and no dithering for text and prices: every pixel is one palette colour.
- Bundle open-licence fonts with full Greek coverage (for example Noto Sans or DejaVu Sans, regular and bold) in the repo, with their licence files. Do not rely on system fonts.
- Product names fit by rule: up to two lines, shrink through a fixed list of sizes, then truncate with an ellipsis. Never overflow into the price area.
- Red (and yellow where available) is used only for promo elements.
- An unbound tag gets an "unassigned" label showing its `short_code` in large type, so staff can identify it during binding.

**Templates for v1** (each for 296×128; code-defined classes with a version number)

| Key | Content |
| --- | --- |
| `standard` | Name, brand, price |
| `unit_price` | Name, brand, price, unit price at the required relative size |
| `promo` | Name, promo price in red, prior price struck through, validity end date; unit price (from the promo price, same size rule) when required |
| `unassigned` | Short code and tag model |

**Tests**

- Golden-image tests for each template with Greek fixtures, including accented capitals, long names and prices from 0,50 € to 999,99 €.
- A test that every output pixel is in the palette.
- A test that the same `LabelData` always yields the same hash and bytes.

The admin UI shows the rendered PNG enlarged with nearest-neighbour scaling as the preview.

## 8. Tag drivers

Every tag brand sits behind one interface, and the whole system is built and tested against a fake driver before any hardware arrives.

**Interface** (`gateway/drivers/base.py`)

```python
class TagDriver(Protocol):
    vendor: str

    async def discover(self, timeout_s: float) -> list[SeenTag]:
        """Tags currently reachable: vendor_id, model, rssi, battery if known."""

    async def push_image(self, vendor_id: str, tag_type: TagType, png: bytes) -> PushResult:
        """Send a palette PNG. Return ok only when the tag confirmed the full image."""

    async def status(self, vendor_id: str) -> TagStatus | None:
        """Optional: battery and reachability for one tag."""
```

`PushResult` carries `ok`, an error code (`not_found`, `timeout`, `rejected`, `protocol`) and the duration.

**Fake driver**

- Reads a list of pretend tags from a config file and writes each pushed image to `./fake_tags/<vendor_id>.png`.
- Can be told to fail a percentage of pushes or to delay, so retries and monitoring can be tested.
- Used by all end-to-end tests and by milestones M0–M3 and M5.

**Gicisky BLE driver**

The vendor publishes no protocol documentation; the protocol is known from community reverse-engineering. Treat everything below as to be verified on the real tags.

1. Research first. Read the public implementations (the Home Assistant Gicisky integration, atc1441's Gicisky work, OpenEPaperLink's BLE support) and write `docs/gicisky-protocol.md`: how tags advertise, how model, resolution and colours are identified, the write and notify characteristics, the image packing, the chunking and the completion signal.
2. Licences. Record each source's licence in that document. OpenEPaperLink is CC BY-NC-SA (non-commercial): use it only to understand the protocol and copy no code from it. Write the driver from the protocol notes.
3. Implement with `bleak`: scan for advertisements, connect to one tag at a time, convert the palette PNG to the tag's packed format, write in chunks, wait for the tag's confirmation, disconnect.
4. Reliability: a 60-second overall timeout per push, one connection at a time per Bluetooth adapter, and a clean disconnect on every error path.
5. Provide `gateway/tools/push_png.py <address> <file.png>` for manual testing with a real tag.

**Known unknowns to resolve with hardware**

- Which model variant was delivered and whether it needs mirrored or rotated image data.
- Whether battery level is exposed in the advertisement.
- Real update time per tag and usable range in the shop.

## 9. Admin UI and phone binding

A server-rendered web app, usable on a desktop for catalogue work and on a phone for work at the shelf.

| Page | What it does |
| --- | --- |
| Dashboard | Counts of tags up to date, pending, failed and unseen for 24 h; last import age; gateway status |
| Products | Search by name or EAN; edit price and promo; label preview; price history |
| Import | Upload, column mapping, preview diff, apply; list of past imports |
| Tags | List with product, last seen, RSSI, battery, displayed vs desired; retry and unbind actions |
| Bind (phone) | The binding flow below |
| Audit | Walk the shelf: scan a tag code, see what it should show |
| Settings | Users, gateway token, staleness threshold |

**Binding flow on the phone**

1. Scan the product's EAN with the camera. Unknown EAN: offer to create the product.
2. Enter or scan the tag's `short_code`, which an unbound tag shows on its own screen.
3. Choose a template (default from the product's flags), see the preview, confirm.
4. The tag updates; the page shows pending, then done or failed.

**Constraints**

- Phone cameras only work in the browser over HTTPS. The runbook must cover serving the app over HTTPS on the private network (for example Tailscale's HTTPS certificates, or Caddy with a local certificate).
- Fallback without a camera: a USB barcode scanner on a desktop, which types into the same EAN field.
- Session login with two roles: `admin` (everything) and `staff` (bind, audit, view).
- UI language is an open question (section 12); keep all strings in one translation file from the start.

## 10. Milestones

Milestones M0–M3 and M5 need no hardware and can start today; only M4 and M6 wait for the tags and the box.

| # | Milestone | Deliverables | Done when | Needs tags |
| --- | --- | --- | --- | --- |
| M0 | Skeleton | Repo layout, `pyproject.toml`, Docker Compose (core + Postgres), first migration, GitHub Actions CI with ruff, mypy and pytest, health endpoint | `docker compose up` serves `/health`; CI is green | No |
| M1 | Catalogue and import | Product and price models, price history, CSV import with profiles and preview diff, manual price edit, event log | A Greek export (Windows-1253, decimal comma) imports correctly, first the synthetic fixture and then `tests/fixtures/erp_sample.csv`; re-import shows only real changes; all changes appear in history | No |
| M2 | Renderer | Palette handling, bundled fonts, four templates, content hashing, render store, pricing rules in `core/domain/` | Golden-image tests pass; palette test passes; promo without prior price is refused | No |
| M3 | Tags, bindings, queue | Tag and binding models, reconciler, update-job state machine, gateway API, gateway agent with fake driver | End to end with the fake driver: a price change produces a new PNG for exactly the bound tags; injected failures retry and end in `dead` after 6 attempts | No |
| M4 | Gicisky driver | `docs/gicisky-protocol.md`, BLE driver, `push_png.py` tool, discovery reporting | A price edit in the UI appears on a real tag within 2 minutes; 50 consecutive pushes succeed at 3 m | Yes |
| M5 | Admin UI and binding | All pages in section 9, login and roles, phone binding and audit flows | A staff user can bind a product to a tag from a phone unaided; dashboard numbers match the database | No (fake driver) |
| M6 | Store install | Runbook, systemd unit for the gateway, HTTPS on the private network, nightly Postgres dump to off-site storage, restore tested | A fresh box is installed from the runbook alone; a restore from backup is demonstrated; staleness and failed-job warnings show up | Yes |
| M7 | Later, not part of this build | Move core to Railway, real multi-store, second driver (Opticon EBS-50 or Zkong), ERP adapter | Decided separately after the pilot | n/a |

Suggested order while waiting for hardware: M0, M1, M2, M3, M5, then M4 when the tags arrive, then M6.

## 11. Working rules for the coding agents

- Work one milestone at a time, in small pull requests; each PR states which acceptance criteria it advances.
- Every PR passes ruff, mypy and pytest. New domain logic comes with unit tests; new templates come with golden images.
- Do not start M4 driver code before `docs/gicisky-protocol.md` exists and lists its sources and their licences.
- Copy no code from non-commercially licensed projects (OpenEPaperLink in particular).
- Keep `core/domain/` free of I/O. Keep the gateway free of business logic.
- Money is integer cents; never floats. Timestamps are UTC in the database and shown in `Europe/Athens`.
- Secrets and connection strings come from environment variables; nothing sensitive in the repo.
- **The repository is public.** Never commit real pharmacy data, credentials or anything under `data/private/`. The only real data allowed is the trimmed sample Tasos adds himself as `tests/fixtures/erp_sample.csv`.
- Tests must run in a cloud session: Postgres is started by the SessionStart hook; no test may need Bluetooth, except tests marked `hardware`, which are skipped by default.
- Record each non-obvious decision as a short note in `docs/decisions/`.
- Do not add scope from section 1's out-of-scope list or from M7. If a milestone seems to need it, stop and ask.
- If a question in section 12 blocks progress, stop and ask instead of assuming.

## 12. Open questions for Tasos

- [ ] Can the pharmacy's ERP export a price list with EAN, name and retail price to a file? A trimmed real sample (20–50 rows, only EAN, name, price and pack columns) is needed as `tests/fixtures/erp_sample.csv` to accept M1.
- [ ] Which colour variant of the Gicisky tags was ordered (three or four colours)?
- [ ] UI language: Greek only, or Greek with English?
- [ ] Which box will run it in the shop (Raspberry Pi 5 or mini-PC), and where will it sit relative to the pilot shelf?
- [ ] Where should nightly backups go (for example a Cloudflare R2 bucket)?
- [ ] Who uses it besides you, and is `admin` plus `staff` enough?
- [ ] Have the price-display rules in section 6 confirmed by your accountant, a lawyer or the Φαρμακευτικός Σύλλογος, including which product categories are exempt from unit pricing, how to treat non-prescription medicines, and whether promo labels must also show the unit price (v1 shows it, computed from the promo price, whenever `unit_price_required` is set).
- [ ] Which licence for the public repository (for example MIT, Apache-2.0, or none yet)?
