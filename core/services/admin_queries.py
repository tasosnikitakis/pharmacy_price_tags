"""Read-only queries for the admin UI pages. Nothing here writes or locks.

Every write the pages make goes through the services that own it (catalogue,
promotions, tags, queue, users), in their lock order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import func, or_, select
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
    Tag,
    TagTypeRow,
    Template,
    UpdateJob,
)

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
    """Displayed versus desired label, as the Tags page shows it."""

    UP_TO_DATE = "up_to_date"
    PENDING = "pending"
    FAILED = "failed"
    DEAD = "dead"


def tag_status(desired: str | None, displayed: str | None, last_job_state: str | None) -> TagStatus:
    """``up_to_date`` when the tag shows its desired label; otherwise the state of its
    newest job: ``dead``, ``failed`` (waiting for a retry), else ``pending``."""
    if desired is not None and desired == displayed:
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


def tag_rows(
    session: Session, store_id: int = DEFAULT_STORE_ID, tag_ids: Sequence[int] | None = None
) -> list[TagRow]:
    """Every tag (or the given ones) with its type, bound product and newest job, by
    short code."""
    last_job = (
        select(UpdateJob.tag_id, func.max(UpdateJob.id).label("job_id"))
        .group_by(UpdateJob.tag_id)
        .subquery()
    )
    stmt = (
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
    if tag_ids is not None:
        stmt = stmt.where(Tag.id.in_(tag_ids))
    return [
        TagRow(tag, tag_type, product, key, job)
        for tag, tag_type, product, key, job in session.execute(stmt).tuples()
    ]


def tag_row(session: Session, tag_id: int, store_id: int = DEFAULT_STORE_ID) -> TagRow | None:
    rows = tag_rows(session, store_id, [tag_id])
    return rows[0] if rows else None


def tag_jobs(session: Session, tag_id: int, limit: int = 10) -> list[UpdateJob]:
    return list(
        session.scalars(
            select(UpdateJob)
            .where(UpdateJob.tag_id == tag_id)
            .order_by(UpdateJob.id.desc())
            .limit(limit)
        )
    )


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
