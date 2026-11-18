"""Catalogue writes: import preview and apply, manual price edit, product edit.

Every write to a ``price`` row goes through ``core.domain.pricing.apply_price_change``
(product edits through ``apply_product_change``) and stores exactly what it returns:
the new ``price`` row, its ``price_history`` rows and its ``event_log`` rows. Each
public write function runs in one savepoint inside the caller's transaction, locks the
``price`` rows it reads (``SELECT ... FOR UPDATE``) and takes ``now`` once, so the
whole write uses one clock (docs/decisions/0007, 0008). The caller commits.

Products are never deleted: products missing from an import are only listed.

Every write that changes a price row or a product field then refreshes the labels of
the product's bound tags (``core.services.tags.refresh_desired_hashes``) in the same
savepoint and with the same ``now``, so a change and the update jobs it causes commit
together (docs/decisions/0014). Lock order across catalogue and queue, the same in every
transaction: ``store`` (imports only), then ``product``, then ``price`` rows, then
``tag`` rows, then ``update_job`` rows. The queue and gateway code takes only the last
two, in the same order, and never locks a store, product or price row (it reads them
without locks; foreign-key checks take ``FOR KEY SHARE``, which the catalogue's ``FOR NO
KEY UPDATE`` does not block). So there is no cycle between a catalogue writer and the
queue. An import holds its tag locks while it renders every affected label (one render
per bound tag, before commit); a gateway result for one of those tags waits for the
import, and a claim skips them (``SKIP LOCKED``). That is accepted for v1.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import Settings, get_settings
from core.db.models import (
    DEFAULT_STORE_ID,
    EventLog,
    ImportProfile,
    ImportRun,
    Price,
    PriceHistory,
    Product,
    Store,
)
from core.domain import pricing
from core.domain.catalogue import (
    BRAND_MAX_LEN,
    NAME_MAX_LEN,
    PRICE_CENTS_MAX,
    CatalogueEntry,
    DiffEntry,
    ImportDiff,
    JumpThresholds,
    PriceJump,
    build_diff,
    normalise_text,
)
from core.domain.pricing import PriceChange, PriceEvent
from core.domain.types import (
    PackUnit,
    PriceKind,
    PricePoint,
    PriceSource,
    PriceState,
    ProductFacts,
)
from core.importers.profile import ImportSettings, ParsedImport, parse_import
from core.services.tags import LabelPipeline, real_label_pipeline, refresh_desired_hashes

#: ``event_log.actor`` for writes no person made.
SYSTEM_ACTOR = "system"
#: Column lengths of ``import_profile.name`` and ``import_run.filename``.
PROFILE_NAME_MAX_LEN = 100
FILENAME_MAX_LEN = 255


class ImportConfirmationRequired(ValueError):
    """Applying needs ``confirm_price_jumps=True``: too many prices jump at once,
    which suggests a wrong column or the wrong file. Nothing was written."""

    def __init__(self, jumps: list[PriceJump], price_changes: int, thresholds: JumpThresholds):
        super().__init__(
            f"{len(jumps)} of {price_changes} price changes are over "
            f"{thresholds.warn_percent}%, more than {thresholds.banner_percent}% of them: "
            "check the price column and the file, then confirm the price jumps to apply"
        )
        self.jumps = jumps
        self.price_changes = price_changes
        self.thresholds = thresholds


def jump_thresholds(settings: Settings | None = None) -> JumpThresholds:
    """The price-jump thresholds configured in settings."""
    settings = settings or get_settings()
    return JumpThresholds(settings.price_jump_warn_percent, settings.price_jump_banner_percent)


class NotFound(LookupError):
    """The product or profile does not exist in this store."""


# --- Conversions ----------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(UTC)


def _check_now(now: datetime | None) -> datetime:
    if now is None:
        return utc_now()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def product_facts(product: Product) -> ProductFacts:
    return ProductFacts(
        product_id=product.id,
        ean=product.ean,
        name=product.name,
        brand=product.brand,
        pack_qty_milli=product.pack_qty_milli,
        pack_unit=PackUnit(product.pack_unit) if product.pack_unit else None,
        unit_price_required=product.unit_price_required,
        is_rx=product.is_rx,
    )


def price_state(price: Price) -> PriceState:
    return PriceState(
        regular_cents=price.regular_cents,
        promo_cents=price.promo_cents,
        promo_from=price.promo_from,
        promo_to=price.promo_to,
        promo_prior_cents=price.promo_prior_cents,
    )


def _jsonable(value: Any) -> Any:
    """Make an event payload storable as JSONB (datetimes as ISO 8601 UTC)."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


# --- Reading --------------------------------------------------------------------------


def load_history(session: Session, product_ids: Iterable[int]) -> dict[int, list[PricePoint]]:
    """``price_history`` of the given products as ``PricePoint`` lists, in id order."""
    ids = sorted(set(product_ids))
    result: dict[int, list[PricePoint]] = {pid: [] for pid in ids}
    if not ids:
        return result
    rows = session.scalars(
        select(PriceHistory).where(PriceHistory.product_id.in_(ids)).order_by(PriceHistory.id)
    )
    for row in rows:
        result[row.product_id].append(
            PricePoint(
                seq=row.id,
                effective_cents=row.effective_cents,
                kind=PriceKind(row.kind),
                valid_from=row.valid_from,
                source=PriceSource(row.source),
            )
        )
    return result


def catalogue_entries(session: Session, store_id: int = DEFAULT_STORE_ID) -> list[CatalogueEntry]:
    """Every product of the store with its regular price, for the import diff."""
    rows = session.execute(
        select(Product, Price.regular_cents)
        .outerjoin(Price, Price.product_id == Product.id)
        .where(Product.store_id == store_id)
        .order_by(Product.id)
    )
    return [
        CatalogueEntry(
            product_id=product.id,
            ean=product.ean,
            name=product.name,
            brand=product.brand,
            pack_qty_milli=product.pack_qty_milli,
            pack_unit=PackUnit(product.pack_unit) if product.pack_unit else None,
            regular_cents=regular_cents,
            manually_changed_at=product.manually_changed_at,
            active=product.active,
        )
        for product, regular_cents in rows
    ]


def last_applied_import_at(session: Session, store_id: int = DEFAULT_STORE_ID) -> datetime | None:
    """When the last import was applied, for the staleness warning (section 6)."""
    return session.scalar(
        select(ImportRun.applied_at)
        .where(ImportRun.store_id == store_id, ImportRun.applied_at.is_not(None))
        .order_by(ImportRun.applied_at.desc())
        .limit(1)
    )


# --- Profiles -------------------------------------------------------------------------


def save_profile(
    session: Session,
    name: str,
    settings: ImportSettings,
    store_id: int = DEFAULT_STORE_ID,
) -> ImportProfile:
    """Create the profile ``name``, or replace its mapping and settings."""
    name = name.strip()
    if not name:
        raise ValueError("profile name is empty")
    if len(name) > PROFILE_NAME_MAX_LEN:
        raise ValueError(f"profile name is longer than {PROFILE_NAME_MAX_LEN} characters")
    profile = get_profile(session, name, store_id)
    if profile is None:
        profile = ImportProfile(store_id=store_id, name=name)
        session.add(profile)
    profile.mapping = settings.mapping_json()
    profile.encoding = settings.encoding
    profile.delimiter = settings.delimiter
    session.flush()
    return profile


def get_profile(
    session: Session, name: str, store_id: int = DEFAULT_STORE_ID
) -> ImportProfile | None:
    return session.scalar(
        select(ImportProfile).where(
            ImportProfile.store_id == store_id, ImportProfile.name == name.strip()
        )
    )


def list_profiles(session: Session, store_id: int = DEFAULT_STORE_ID) -> list[ImportProfile]:
    return list(
        session.scalars(
            select(ImportProfile)
            .where(ImportProfile.store_id == store_id)
            .order_by(ImportProfile.name)
        )
    )


def profile_settings(profile: ImportProfile) -> ImportSettings:
    """The validated settings of a stored profile (raises ``ValueError`` if invalid)."""
    return ImportSettings.create(profile.mapping, profile.encoding, profile.delimiter)


# --- Storing one price write ----------------------------------------------------------


def _write_events(
    session: Session,
    events: Iterable[PriceEvent],
    actor: str,
    now: datetime,
    store_id: int,
) -> None:
    for event in events:
        _log(session, event.kind, event.payload, actor, now, store_id)


def _log(
    session: Session,
    kind: str,
    payload: Mapping[str, object],
    actor: str,
    now: datetime,
    store_id: int,
) -> None:
    stored = _jsonable(payload)
    session.add(EventLog(store_id=store_id, at=now, actor=actor, kind=kind, payload=stored))


def _store_change(
    session: Session,
    product: Product,
    price: Price | None,
    change: PriceChange,
    *,
    actor: str,
    now: datetime,
    import_run_id: int | None = None,
) -> tuple[Price, list[PricePoint]]:
    """Store exactly what a pricing call returned; return the price row and the new
    history as ``PricePoint`` values (with their ids), for a follow-up call."""
    state = change.state
    if price is None:
        price = Price(product_id=product.id, store_id=product.store_id)
        session.add(price)
    price.regular_cents = state.regular_cents
    price.promo_cents = state.promo_cents
    price.promo_from = state.promo_from
    price.promo_to = state.promo_to
    price.promo_prior_cents = state.promo_prior_cents
    price.updated_at = now
    rows = [
        PriceHistory(
            store_id=product.store_id,
            product_id=product.id,
            effective_cents=point.effective_cents,
            kind=PriceKind(point.kind).value,
            valid_from=point.valid_from,
            source=PriceSource(point.source).value,
            import_run_id=import_run_id,
            created_at=now,
        )
        for point in change.history_rows
    ]
    session.add_all(rows)
    _write_events(session, change.events, actor, now, product.store_id)
    session.flush()
    points = [
        PricePoint(r.id, r.effective_cents, PriceKind(r.kind), r.valid_from, PriceSource(r.source))
        for r in rows
    ]
    return price, points


def _lock_price(session: Session, product_id: int, store_id: int) -> Price | None:
    return session.scalar(
        select(Price)
        .where(Price.product_id == product_id, Price.store_id == store_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )


def _lock_product(session: Session, product_id: int, store_id: int) -> Product:
    product = session.scalar(
        select(Product)
        .where(Product.id == product_id, Product.store_id == store_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if product is None:
        raise NotFound(f"product {product_id} not found")
    return product


def _lock_products(
    session: Session, store_id: int, product_ids: Sequence[int] | None = None
) -> list[Product]:
    """Lock the store's products (or the given ones) in id order."""
    query = select(Product).where(Product.store_id == store_id)
    if product_ids is not None:
        query = query.where(Product.id.in_(product_ids))
    return list(
        session.scalars(
            query.order_by(Product.id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    )


def _lock_prices(
    session: Session, store_id: int, product_ids: Sequence[int] | None = None
) -> list[Price]:
    """Lock the store's price rows (or the given products') in product id order."""
    query = select(Price).where(Price.store_id == store_id)
    if product_ids is not None:
        query = query.where(Price.product_id.in_(product_ids))
    return list(
        session.scalars(
            query.order_by(Price.product_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    )


def _refresh_labels(
    session: Session,
    pipeline: LabelPipeline | None,
    now: datetime,
    product_ids: Iterable[int | None],
) -> None:
    """Refresh the labels of the tags bound to ``product_ids`` and queue their updates.

    Runs last in the writer's savepoint, after the product and price locks, so the tag
    and job locks it takes come after them (see the module docstring)."""
    ids = sorted({pid for pid in product_ids if pid is not None})
    if ids:
        refresh_desired_hashes(session, pipeline or real_label_pipeline, now, product_ids=ids)


# --- Import ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImportPreview:
    """What applying a file would do; nothing is written."""

    filename: str
    profile_name: str
    encoding: str
    delimiter: str
    diff: ImportDiff


@dataclass(frozen=True, slots=True)
class ImportResult:
    run: ImportRun
    diff: ImportDiff
    # Events from the pricing rules, e.g. ``promo.refused`` when the new regular price
    # makes a running promo unlawful; shown to the admin after applying.
    price_events: list[PriceEvent]


def _check_filename(filename: str) -> None:
    if len(filename) > FILENAME_MAX_LEN:
        raise ValueError(f"file name is longer than {FILENAME_MAX_LEN} characters")


def _parse(data: bytes, profile: ImportProfile) -> ParsedImport:
    return parse_import(data, profile_settings(profile))


def preview_import(
    session: Session,
    data: bytes,
    filename: str,
    profile: ImportProfile,
    thresholds: JumpThresholds | None = None,
) -> ImportPreview:
    """Parse ``data`` with ``profile`` and diff it against the catalogue (section 6,
    step 3). The diff carries the price jumps and the ``suspicious`` flag, with
    ``thresholds`` or, by default, those from settings. Raises ``ImportFormatError``
    if the file as a whole cannot be read."""
    _check_filename(filename)
    thresholds = thresholds or jump_thresholds()
    parsed = _parse(data, profile)
    catalogue = catalogue_entries(session, profile.store_id)
    diff = build_diff(parsed.rows, parsed.rejected, catalogue, thresholds)
    return ImportPreview(filename, profile.name, parsed.encoding, parsed.delimiter, diff)


def apply_import(
    session: Session,
    data: bytes,
    filename: str,
    profile: ImportProfile,
    *,
    actor: str,
    created_by: int | None = None,
    now: datetime | None = None,
    confirm_price_jumps: bool = False,
    thresholds: JumpThresholds | None = None,
    pipeline: LabelPipeline | None = None,
) -> ImportResult:
    """Apply a file in one transaction (section 6, step 4).

    The diff is rebuilt here after locking the store, its products and its ``price``
    rows, so it reflects the catalogue at the moment of writing. If it is
    ``suspicious`` (too many price jumps, see ``build_diff``) this raises
    ``ImportConfirmationRequired`` and writes nothing, unless ``confirm_price_jumps``
    is true. Jumping rows are never rejected one by one. New products are created; changed
    prices go through ``apply_price_change`` with source ``import`` and the stored
    promo passed through unchanged; changed product fields go through
    ``apply_product_change`` and are logged per product as ``product.edited`` with the
    import run id. Unchanged rows write nothing, so re-importing a file
    adds no history. The manual-change flag is cleared on every product in the file.
    Records an ``import_run`` with counts and an ``import.applied`` event. Then the
    tags bound to every changed product get their labels refreshed with ``pipeline``
    (``real_label_pipeline`` by default); new products have no tags yet.
    """
    _check_filename(filename)
    now = _check_now(now)
    store_id = profile.store_id
    thresholds = thresholds or jump_thresholds()
    parsed = _parse(data, profile)
    with session.begin_nested():
        # Lock order, the same in every writer: store (imports only), then products,
        # then prices, all FOR NO KEY UPDATE (key_share=True). The store row serialises
        # imports, so two imports cannot both create the same new EAN; NO KEY UPDATE
        # does not conflict with the FOR KEY SHARE that every insert referencing the
        # store (history, events) takes, so an edit that already holds a product lock
        # can finish its inserts while this import waits for that product, instead of
        # deadlocking. Edits lock their product before its price, so a product without
        # a price row cannot get one behind the import's back (and vice versa): whoever
        # comes second waits, then reads the committed row. The label refresh at the
        # end takes tag rows, then update_job rows, after all of these (module docstring).
        session.execute(
            select(Store.id).where(Store.id == store_id).with_for_update(key_share=True)
        )
        products = {p.id: p for p in _lock_products(session, store_id)}
        prices = {price.product_id: price for price in _lock_prices(session, store_id)}
        catalogue = catalogue_entries(session, store_id)
        diff = build_diff(parsed.rows, parsed.rejected, catalogue, thresholds)
        if diff.suspicious and not confirm_price_jumps:
            price_changes = sum(
                1 for e in diff.changed if e.price_changed and e.old_regular_cents is not None
            )
            raise ImportConfirmationRequired(diff.price_jumps, price_changes, thresholds)
        matched = [*diff.changed, *diff.unchanged]
        # Under READ COMMITTED a product committed while this import waited for a lock
        # is visible to the diff but was not in the locked set; lock those products and
        # their price rows too, so an existing price row is updated, never duplicated.
        late = [e.product_id for e in matched if e.product_id and e.product_id not in products]
        if late:
            products.update((p.id, p) for p in _lock_products(session, store_id, late))
            prices.update((r.product_id, r) for r in _lock_prices(session, store_id, late))
        run = ImportRun(
            store_id=store_id,
            profile_id=profile.id,
            filename=filename,
            started_at=now,
            created_by=created_by,
        )
        session.add(run)
        session.flush()

        price_events: list[PriceEvent] = []
        for entry in diff.new:
            price_events += _import_new(session, entry, run, store_id, actor, now)
        history = load_history(session, [e.product_id for e in diff.changed if e.product_id])
        for entry in diff.changed:
            assert entry.product_id is not None
            product = products[entry.product_id]
            price_events += _import_changed(
                session,
                entry,
                product,
                prices.get(product.id),
                history[product.id],
                run,
                actor,
                now,
            )
        for entry in matched:
            assert entry.product_id is not None
            products[entry.product_id].manually_changed_at = None

        counts: dict[str, int] = {
            **diff.counts(),
            "promo_refused": sum(1 for e in price_events if e.kind == "promo.refused"),
        }
        run.counts = counts
        run.applied_at = now
        _log(
            session,
            "import.applied",
            {
                "import_run_id": run.id,
                "profile": profile.name,
                "filename": filename,
                "encoding": parsed.encoding,
                "delimiter": parsed.delimiter,
                "counts": counts,
                "price_jumps": len(diff.price_jumps),
                "price_jumps_suspicious": diff.suspicious,
                "price_jumps_confirmed": diff.suspicious and confirm_price_jumps,
                "jump_thresholds": {
                    "warn_percent": thresholds.warn_percent,
                    "banner_percent": thresholds.banner_percent,
                },
            },
            actor,
            now,
            store_id,
        )
        session.flush()
        _refresh_labels(session, pipeline, now, (e.product_id for e in diff.changed))
    return ImportResult(run, diff, price_events)


def _import_new(
    session: Session,
    entry: DiffEntry,
    run: ImportRun,
    store_id: int,
    actor: str,
    now: datetime,
) -> list[PriceEvent]:
    row = entry.row
    product = Product(
        store_id=store_id,
        ean=row.ean,
        name=row.name,
        brand=row.brand,
        pack_qty_milli=row.pack_qty_milli,
        pack_unit=row.pack_unit.value if row.pack_unit else None,
        unit_price_required=False,
        is_rx=False,
        active=True,
        created_at=now,
        updated_at=now,
    )
    session.add(product)
    session.flush()
    change = pricing.apply_price_change(
        product_facts(product),
        None,
        PriceState(regular_cents=row.regular_cents),
        [],
        now,
        PriceSource.IMPORT,
    )
    _store_change(session, product, None, change, actor=actor, now=now, import_run_id=run.id)
    return list(change.events)


def _import_changed(
    session: Session,
    entry: DiffEntry,
    product: Product,
    price: Price | None,
    history: list[PricePoint],
    run: ImportRun,
    actor: str,
    now: datetime,
) -> list[PriceEvent]:
    events: list[PriceEvent] = []
    row = entry.row
    if entry.field_changes:
        old_facts = product_facts(product)
        for field_change in entry.field_changes:
            new = field_change.new
            setattr(product, field_change.field, new.value if isinstance(new, PackUnit) else new)
        product.updated_at = now
        product_change = pricing.apply_product_change(
            old_facts,
            product_facts(product),
            price_state(price) if price is not None else None,
            history,
            now,
        )
        if product_change is not None:
            price, points = _store_change(
                session, product, price, product_change, actor=actor, now=now,
                import_run_id=run.id,
            )  # fmt: skip
            history = [*history, *points]
            events += product_change.events
        _log(
            session,
            "product.edited",
            {
                "product_id": product.id,
                "ean": product.ean,
                "changes": {c.field: [c.old, c.new] for c in entry.field_changes},
                "import_run_id": run.id,
            },
            actor,
            now,
            product.store_id,
        )
    if price is None or price.regular_cents != row.regular_cents:
        old = price_state(price) if price is not None else None
        # Pass the stored promo through unchanged; leaving it out would end it.
        requested = (
            dataclasses.replace(old, regular_cents=row.regular_cents)
            if old is not None
            else PriceState(regular_cents=row.regular_cents)
        )
        change = pricing.apply_price_change(
            product_facts(product), old, requested, history, now, PriceSource.IMPORT
        )
        _store_change(session, product, price, change, actor=actor, now=now, import_run_id=run.id)
        events += change.events
    return events


# --- Manual edits ---------------------------------------------------------------------


def edit_price(
    session: Session,
    product_id: int,
    regular_cents: int,
    *,
    actor: str,
    now: datetime | None = None,
    store_id: int = DEFAULT_STORE_ID,
    pipeline: LabelPipeline | None = None,
) -> PriceChange | None:
    """Change a product's regular price by hand (section 6, "Manual edit").

    Goes through ``apply_price_change`` with source ``manual``, keeping the stored promo
    unchanged, and marks the product as manually changed since the last import. Logs
    ``price.manual`` with the old and new price, then refreshes the labels of the
    product's bound tags with ``pipeline``. Returns ``None`` and writes nothing when
    the price is already ``regular_cents``.
    """
    if isinstance(regular_cents, bool) or not isinstance(regular_cents, int):
        raise TypeError("regular_cents must be an integer number of cents")
    if regular_cents < 0:
        raise ValueError("regular_cents must not be negative")
    if regular_cents == 0:
        raise ValueError("zero price: a shelf price must be above zero")
    if regular_cents > PRICE_CENTS_MAX:
        raise ValueError(f"regular_cents must be at most {PRICE_CENTS_MAX}")
    now = _check_now(now)
    with session.begin_nested():
        # Product before price, as in every writer (see apply_import).
        product = _lock_product(session, product_id, store_id)
        price = _lock_price(session, product_id, store_id)
        old = price_state(price) if price is not None else None
        if old is not None and old.regular_cents == regular_cents:
            return None
        requested = (
            dataclasses.replace(old, regular_cents=regular_cents)
            if old is not None
            else PriceState(regular_cents=regular_cents)
        )
        history = load_history(session, [product_id])[product_id]
        change = pricing.apply_price_change(
            product_facts(product), old, requested, history, now, PriceSource.MANUAL
        )
        _store_change(session, product, price, change, actor=actor, now=now)
        product.manually_changed_at = now
        _log(
            session,
            "price.manual",
            {
                "product_id": product.id,
                "ean": product.ean,
                "old_regular_cents": old.regular_cents if old is not None else None,
                "new_regular_cents": regular_cents,
            },
            actor,
            now,
            store_id,
        )
        session.flush()
        _refresh_labels(session, pipeline, now, [product.id])
    return change


@dataclass(frozen=True, slots=True)
class ProductFields:
    """The editable fields of a product, as a whole. Build one from the current row
    with ``ProductFields.of(product)`` and change fields with ``dataclasses.replace``."""

    name: str
    brand: str | None
    pack_qty_milli: int | None
    pack_unit: PackUnit | None
    unit_price_required: bool
    is_rx: bool
    active: bool

    @classmethod
    def of(cls, product: Product) -> ProductFields:
        return cls(
            name=product.name,
            brand=product.brand,
            pack_qty_milli=product.pack_qty_milli,
            pack_unit=PackUnit(product.pack_unit) if product.pack_unit else None,
            unit_price_required=product.unit_price_required,
            is_rx=product.is_rx,
            active=product.active,
        )

    def normalised(self) -> ProductFields:
        """Name and brand cleaned like an import cell; an empty brand becomes ``None``."""
        brand = normalise_text(self.brand) if self.brand is not None else None
        return dataclasses.replace(self, name=normalise_text(self.name), brand=brand or None)

    def check(self) -> None:
        """Raise ``ValueError`` for values the database would refuse."""
        if not self.name.strip():
            raise ValueError("name is empty")
        if len(self.name) > NAME_MAX_LEN:
            raise ValueError(f"name is longer than {NAME_MAX_LEN} characters")
        if self.brand is not None and len(self.brand) > BRAND_MAX_LEN:
            raise ValueError(f"brand is longer than {BRAND_MAX_LEN} characters")
        if self.pack_qty_milli is not None and self.pack_qty_milli <= 0:
            raise ValueError("pack size must be positive")
        if self.unit_price_required and (self.pack_qty_milli is None or self.pack_unit is None):
            raise ValueError("a unit price needs a pack size and unit")


def _changed_fields(old: ProductFields, new: ProductFields) -> dict[str, list[object]]:
    return {
        f.name: [getattr(old, f.name), getattr(new, f.name)]
        for f in dataclasses.fields(ProductFields)
        if getattr(old, f.name) != getattr(new, f.name)
    }


def edit_product(
    session: Session,
    product_id: int,
    fields: ProductFields,
    *,
    actor: str,
    now: datetime | None = None,
    store_id: int = DEFAULT_STORE_ID,
    pipeline: LabelPipeline | None = None,
) -> PriceChange | None:
    """Edit a product's fields through the shared write path (Tasos, decision 4).

    Calls ``apply_product_change`` in the same transaction as the product update and
    stores its result; setting ``is_rx`` thereby ends any active or scheduled promo
    and writes a history row. Logs ``product.edited`` with each changed field's old and
    new value. Name and brand are normalised as in an import (``normalise_text``; an
    empty brand is stored as ``None``) before comparing, so an edit that changes
    nothing, or only whitespace, writes nothing. Any other edit refreshes the labels of
    the product's bound tags with ``pipeline`` (a field that no label shows simply
    leaves their hashes unchanged). Returns the price change, or ``None`` if the price
    is unaffected.
    """
    fields = fields.normalised()
    fields.check()
    now = _check_now(now)
    with session.begin_nested():
        # Product before price, as in every writer (see apply_import).
        product = _lock_product(session, product_id, store_id)
        price = _lock_price(session, product_id, store_id)
        old_fields = ProductFields.of(product)
        changed = _changed_fields(old_fields, fields)
        if not changed:
            return None
        old_facts = product_facts(product)
        product.name = fields.name
        product.brand = fields.brand
        product.pack_qty_milli = fields.pack_qty_milli
        product.pack_unit = fields.pack_unit.value if fields.pack_unit else None
        product.unit_price_required = fields.unit_price_required
        product.is_rx = fields.is_rx
        product.active = fields.active
        product.updated_at = now
        history = load_history(session, [product_id])[product_id]
        change = pricing.apply_product_change(
            old_facts,
            product_facts(product),
            price_state(price) if price is not None else None,
            history,
            now,
        )
        if change is not None:
            _store_change(session, product, price, change, actor=actor, now=now)
        _log(
            session,
            "product.edited",
            {"product_id": product.id, "ean": product.ean, "changes": changed},
            actor,
            now,
            store_id,
        )
        session.flush()
        _refresh_labels(session, pipeline, now, [product.id])
    return change


def history_of(session: Session, product_id: int) -> Sequence[PricePoint]:
    """A product's price history in id order (for the product page and tests)."""
    return load_history(session, [product_id])[product_id]
