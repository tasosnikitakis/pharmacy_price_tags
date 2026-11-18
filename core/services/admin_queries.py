"""Read-only queries for the admin UI pages. Nothing here writes or locks.

Every write the pages make goes through the services that own it (catalogue,
promotions, tags, queue, users), in their lock order.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from core.db.models import (
    DEFAULT_STORE_ID,
    Binding,
    GatewayRow,
    ImportProfile,
    ImportRun,
    Price,
    PriceHistory,
    Product,
    RenderRow,
    Tag,
    TagTypeRow,
    Template,
    UpdateJob,
)
from core.services.catalogue import last_applied_import_at
from core.services.tags import promo_boundary_tags

SEARCH_LIMIT = 50


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- Products -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProductRow:
    product: Product
    price: Price | None
    tag_count: int


def search_products(
    session: Session, query: str, store_id: int = DEFAULT_STORE_ID, limit: int = SEARCH_LIMIT
) -> list[ProductRow]:
    """Products whose name contains ``query`` (ignoring case) or whose EAN starts with
    it; all products, by name, for an empty query. At most ``limit`` rows."""
    tags = (
        select(Binding.product_id, func.count().label("n")).group_by(Binding.product_id).subquery()
    )
    stmt = (
        select(Product, Price, func.coalesce(tags.c.n, 0))
        .outerjoin(Price, Price.product_id == Product.id)
        .outerjoin(tags, tags.c.product_id == Product.id)
        .where(Product.store_id == store_id)
    )
    query = query.strip()
    if query:
        pattern = _like_escape(query)
        stmt = stmt.where(
            or_(
                Product.name.ilike(f"%{pattern}%", escape="\\"),
                Product.ean.like(f"{pattern}%", escape="\\"),
            )
        )
    stmt = stmt.order_by(Product.name, Product.id).limit(limit)
    return [ProductRow(p, price, n) for p, price, n in session.execute(stmt).tuples()]


def get_product(
    session: Session, product_id: int, store_id: int = DEFAULT_STORE_ID
) -> tuple[Product, Price | None] | None:
    row = session.execute(
        select(Product, Price)
        .outerjoin(Price, Price.product_id == Product.id)
        .where(Product.id == product_id, Product.store_id == store_id)
    ).first()
    return (row[0], row[1]) if row is not None else None


def product_by_ean(
    session: Session, ean: str, store_id: int = DEFAULT_STORE_ID
) -> tuple[Product, Price | None] | None:
    """The product with this (already normalised) EAN and its price, in one query."""
    row = session.execute(
        select(Product, Price)
        .outerjoin(Price, Price.product_id == Product.id)
        .where(Product.store_id == store_id, Product.ean == ean)
    ).first()
    return (row[0], row[1]) if row is not None else None


def price_history_rows(session: Session, product_id: int, limit: int = 200) -> list[PriceHistory]:
    """The product's newest ``price_history`` rows first (in write order)."""
    return list(
        session.scalars(
            select(PriceHistory)
            .where(PriceHistory.product_id == product_id)
            .order_by(PriceHistory.id.desc())
            .limit(limit)
        )
    )


@dataclass(frozen=True, slots=True)
class BoundTag:
    tag_id: int
    short_code: str
    template: str


def product_tags(session: Session, product_id: int) -> list[BoundTag]:
    rows = session.execute(
        select(Tag.id, Tag.short_code, Template.key)
        .join(Binding, Binding.tag_id == Tag.id)
        .join(Template, Template.id == Binding.template_id)
        .where(Binding.product_id == product_id)
        .order_by(Tag.short_code)
    ).tuples()
    return [BoundTag(tag_id, code, key) for tag_id, code, key in rows]


def tag_type_rows(session: Session, store_id: int = DEFAULT_STORE_ID) -> list[TagTypeRow]:
    return list(
        session.scalars(
            select(TagTypeRow).where(TagTypeRow.store_id == store_id).order_by(TagTypeRow.key)
        )
    )


# --- Tags -----------------------------------------------------------------------------


class TagStatus(StrEnum):
    """Displayed versus desired label, as the Tags page, the Dashboard, the binding
    flow and the audit page show it. Every tag has exactly one status."""

    UP_TO_DATE = "up_to_date"
    PENDING = "pending"
    FAILED = "failed"
    DEAD = "dead"
    NO_LABEL = "no_label"


def tag_status(desired: str | None, displayed: str | None, last_job_state: str | None) -> TagStatus:
    """The tag's one status:

    - ``no_label``: no label could be built yet (``desired_hash`` is NULL);
    - ``up_to_date``: it shows its desired label (``displayed_hash = desired_hash``);
    - otherwise the state of its newest job: ``dead``, ``failed`` (waiting for a
      retry), else ``pending`` (queued or claimed; or, for the moment between a
      change and the reconcile in the same transaction, no active job yet).
    """
    if desired is None:
        return TagStatus.NO_LABEL
    if desired == displayed:
        return TagStatus.UP_TO_DATE
    if last_job_state == "dead":
        return TagStatus.DEAD
    if last_job_state == "failed":
        return TagStatus.FAILED
    return TagStatus.PENDING


@dataclass(frozen=True, slots=True)
class TagRow:
    tag: Tag
    tag_type: TagTypeRow
    product: Product | None
    template: str | None
    last_job: UpdateJob | None

    @property
    def status(self) -> TagStatus:
        state = self.last_job.state if self.last_job is not None else None
        return tag_status(self.tag.desired_hash, self.tag.displayed_hash, state)

    @property
    def dead_job_id(self) -> int | None:
        job = self.last_job
        return job.id if job is not None and job.state == "dead" else None


def _tag_rows_select(store_id: int) -> Select[tuple[Tag, TagTypeRow, Product, str, UpdateJob]]:
    """Tags with their type, bound product, template key and newest job, by short code:
    one statement, so a page of tags never needs a query per tag."""
    last_job = (
        select(UpdateJob.tag_id, func.max(UpdateJob.id).label("job_id"))
        .group_by(UpdateJob.tag_id)
        .subquery()
    )
    return (
        select(Tag, TagTypeRow, Product, Template.key, UpdateJob)
        .join(TagTypeRow, TagTypeRow.id == Tag.tag_type_id)
        .outerjoin(Binding, Binding.tag_id == Tag.id)
        .outerjoin(Product, Product.id == Binding.product_id)
        .outerjoin(Template, Template.id == Binding.template_id)
        .outerjoin(last_job, last_job.c.tag_id == Tag.id)
        .outerjoin(UpdateJob, UpdateJob.id == last_job.c.job_id)
        .where(Tag.store_id == store_id)
        .order_by(Tag.short_code)
    )


def _to_tag_rows(
    session: Session, stmt: Select[tuple[Tag, TagTypeRow, Product, str, UpdateJob]]
) -> list[TagRow]:
    return [
        TagRow(tag, tag_type, product, key, job)
        for tag, tag_type, product, key, job in session.execute(stmt).tuples()
    ]


def tag_rows(
    session: Session, store_id: int = DEFAULT_STORE_ID, tag_ids: Sequence[int] | None = None
) -> list[TagRow]:
    """Every tag (or the given ones) with its type, bound product and newest job, by
    short code."""
    stmt = _tag_rows_select(store_id)
    if tag_ids is not None:
        stmt = stmt.where(Tag.id.in_(tag_ids))
    return _to_tag_rows(session, stmt)


def product_tag_rows(
    session: Session, product_id: int, store_id: int = DEFAULT_STORE_ID
) -> list[TagRow]:
    """Every tag bound to ``product_id``, as ``tag_rows`` gives them (by short code), in
    one query whatever the number of tags (the audit page, decision 0018)."""
    return _to_tag_rows(session, _tag_rows_select(store_id).where(Binding.product_id == product_id))


def tag_row(session: Session, tag_id: int, store_id: int = DEFAULT_STORE_ID) -> TagRow | None:
    rows = tag_rows(session, store_id, [tag_id])
    return rows[0] if rows else None


def find_tag(session: Session, code: str, store_id: int = DEFAULT_STORE_ID) -> Tag | None:
    """The tag whose ``short_code`` is ``code`` in any case (spaces ignored), or else
    whose ``vendor_id`` is ``code`` ignoring case (a code scanned from the tag's own
    sticker). ``None`` when neither matches."""
    cleaned = "".join(code.split())
    if not cleaned or len(cleaned) > 64:
        return None
    tag = session.scalars(
        select(Tag).where(Tag.store_id == store_id, Tag.short_code == cleaned.upper())
    ).one_or_none()
    if tag is not None:
        return tag
    return session.scalars(
        select(Tag)
        .where(Tag.store_id == store_id, func.lower(Tag.vendor_id) == cleaned.lower())
        .order_by(Tag.id)
        .limit(1)
    ).one_or_none()


@dataclass(frozen=True, slots=True)
class TagProgress:
    """What the binding page polls: the tag's hashes and its newest job."""

    desired_hash: str | None
    displayed_hash: str | None
    job: UpdateJob | None

    @property
    def status(self) -> TagStatus:
        state = self.job.state if self.job is not None else None
        return tag_status(self.desired_hash, self.displayed_hash, state)


def tag_progress(
    session: Session, tag_id: int, store_id: int = DEFAULT_STORE_ID
) -> TagProgress | None:
    """Two primary-key-ordered lookups, cheap enough to poll every few seconds."""
    row = session.execute(
        select(Tag.desired_hash, Tag.displayed_hash).where(
            Tag.id == tag_id, Tag.store_id == store_id
        )
    ).first()
    if row is None:
        return None
    job = session.scalars(
        select(UpdateJob)
        .where(UpdateJob.tag_id == tag_id)
        .order_by(UpdateJob.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    ).first()
    return TagProgress(row[0], row[1], job)


def tag_jobs(session: Session, tag_id: int, limit: int = 10) -> list[UpdateJob]:
    return list(
        session.scalars(
            select(UpdateJob)
            .where(UpdateJob.tag_id == tag_id)
            .order_by(UpdateJob.id.desc())
            .limit(limit)
        )
    )


def stored_renders(session: Session, digests: Iterable[str | None]) -> set[str]:
    """Which of ``digests`` have a PNG in the render store, in one query."""
    wanted = {d for d in digests if d}
    if not wanted:
        return set()
    return set(session.scalars(select(RenderRow.hash).where(RenderRow.hash.in_(wanted))))


# --- Imports, gateway -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImportRunRow:
    run: ImportRun
    profile_name: str | None


def import_runs(
    session: Session, store_id: int = DEFAULT_STORE_ID, limit: int = 20
) -> list[ImportRunRow]:
    rows = session.execute(
        select(ImportRun, ImportProfile.name)
        .outerjoin(ImportProfile, ImportProfile.id == ImportRun.profile_id)
        .where(ImportRun.store_id == store_id)
        .order_by(ImportRun.id.desc())
        .limit(limit)
    ).tuples()
    return [ImportRunRow(run, name) for run, name in rows]


def gateways(session: Session, store_id: int = DEFAULT_STORE_ID) -> list[GatewayRow]:
    return list(
        session.scalars(
            select(GatewayRow).where(GatewayRow.store_id == store_id).order_by(GatewayRow.name)
        )
    )


# --- Dashboard ------------------------------------------------------------------------


class FailingReason(StrEnum):
    """Why a tag's label keeps failing to build (decision 0014), from what the database
    records; no column says so directly."""

    #: ``desired_hash`` is NULL: no label could ever be built for this tag.
    NO_LABEL = "no_label"
    #: Its product's promo started or ended, but the tag's label still predates that
    #: moment: every gateway claim retries it (``promo_boundary_tags``), and so far it
    #: could not be built, or the promo is refused.
    PROMO_BOUNDARY = "promo_boundary"


@dataclass(frozen=True, slots=True)
class FailingLabel:
    row: TagRow
    reason: FailingReason
    #: The promo start or end the label is behind; ``None`` for ``no_label``.
    since: datetime | None


@dataclass(frozen=True, slots=True)
class DashboardCounts:
    """Tag counts. ``up_to_date + pending + failed + no_label == total`` (each tag has
    one ``TagStatus``; ``failed`` includes ``dead``); ``bound + unbound == total``;
    ``unseen`` and ``low_battery`` cut across all of them."""

    total: int
    bound: int
    unbound: int
    up_to_date: int
    pending: int
    failed: int
    dead: int
    no_label: int
    unseen: int
    low_battery: int


@dataclass(frozen=True, slots=True)
class Dashboard:
    counts: DashboardCounts
    last_import_at: datetime | None
    gateways: list[GatewayRow]
    failed: list[TagRow]
    failing_labels: list[FailingLabel]
    unseen: list[TagRow]
    low_battery: list[TagRow]


def is_unseen(tag: Tag, now: datetime, unseen_after: timedelta) -> bool:
    """Not seen by the gateway for longer than ``unseen_after``, or never."""
    return tag.last_seen_at is None or tag.last_seen_at < now - unseen_after


def is_low_battery(tag: Tag, low_pct: int, low_mv: int) -> bool:
    """Below ``low_pct`` percent when the tag reports a percentage, else below
    ``low_mv`` millivolts when it reports a voltage. Unknown is not low."""
    if tag.battery_pct is not None:
        return tag.battery_pct < low_pct
    return tag.battery_mv is not None and tag.battery_mv < low_mv


def dashboard(
    session: Session,
    now: datetime,
    *,
    unseen_after: timedelta,
    low_battery_pct: int,
    low_battery_mv: int,
    failing_grace: timedelta,
    store_id: int = DEFAULT_STORE_ID,
) -> Dashboard:
    """Everything the Dashboard shows, read without locks.

    ``failing_grace``: a tag behind a promo start or end is listed only once the
    boundary is older than this, so the minute until the next gateway claim refreshes
    it is not reported as a failure.
    """
    rows = tag_rows(session, store_id)
    statuses = [row.status for row in rows]
    unseen = [row for row in rows if is_unseen(row.tag, now, unseen_after)]
    low = [row for row in rows if is_low_battery(row.tag, low_battery_pct, low_battery_mv)]
    counts = DashboardCounts(
        total=len(rows),
        bound=sum(1 for row in rows if row.product is not None),
        unbound=sum(1 for row in rows if row.product is None),
        up_to_date=statuses.count(TagStatus.UP_TO_DATE),
        pending=statuses.count(TagStatus.PENDING),
        failed=statuses.count(TagStatus.FAILED) + statuses.count(TagStatus.DEAD),
        dead=statuses.count(TagStatus.DEAD),
        no_label=statuses.count(TagStatus.NO_LABEL),
        unseen=len(unseen),
        low_battery=len(low),
    )
    return Dashboard(
        counts=counts,
        last_import_at=last_applied_import_at(session, store_id),
        gateways=gateways(session, store_id),
        failed=[row for row in rows if row.status in (TagStatus.FAILED, TagStatus.DEAD)],
        failing_labels=_failing_labels(session, rows, now, failing_grace, store_id),
        unseen=unseen,
        low_battery=low,
    )


def _failing_labels(
    session: Session,
    rows: Sequence[TagRow],
    now: datetime,
    grace: timedelta,
    store_id: int,
) -> list[FailingLabel]:
    by_id = {row.tag.id: row for row in rows}
    failing = [
        FailingLabel(row, FailingReason.NO_LABEL, None)
        for row in rows
        if row.tag.desired_hash is None
    ]
    behind = promo_boundary_tags(session, now, store_id)
    if behind:
        prices = session.execute(
            select(Binding.tag_id, Price.promo_from, Price.promo_to)
            .join(Price, Price.product_id == Binding.product_id)
            .where(Binding.tag_id.in_(behind))
        ).tuples()
        for tag_id, start, end in prices:
            row = by_id.get(tag_id)
            changed = row.tag.desired_hash_changed_at if row is not None else None
            if row is None or changed is None or start is None or end is None:
                continue
            # The flip the label is behind: the start if the label predates it, else the end.
            since = start if start > changed else end
            if since <= now - grace:
                failing.append(FailingLabel(row, FailingReason.PROMO_BOUNDARY, since))
    failing.sort(key=lambda f: f.row.tag.short_code)
    return failing
