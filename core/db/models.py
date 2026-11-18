"""SQLAlchemy models for every table in PLAN.md section 5, plus ``import_profile``.

Contract: changes go through the lead agent, and every change comes with an Alembic
migration. Conventions:

- Money is integer cents (``*_cents``), never floats.
- Timestamps are ``timestamptz`` and always written in UTC.
- Every table except ``store`` carries ``store_id``; v1 has exactly one store (id 1).
- Enumerations are text columns with a CHECK constraint (docs/decisions/0003).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DEFAULT_STORE_ID = 1

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

ROLES = ("admin", "staff")
PACK_UNITS = ("g", "ml", "pieces")
PRICE_KINDS = ("regular", "promo")
PRICE_SOURCES = ("import", "manual", "promo")
PALETTES = ("BW", "BWR", "BWRY")
JOB_STATES = ("queued", "claimed", "acked", "failed", "dead", "cancelled")
ACTIVE_JOB_STATES = ("queued", "claimed", "failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012  (SQLAlchemy reads this class attribute)
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }


def _now() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


def _store_fk() -> Mapped[int]:
    return mapped_column(
        ForeignKey("store.id"), server_default=text(str(DEFAULT_STORE_ID)), index=True
    )


class Store(Base):
    __tablename__ = "store"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), server_default="Europe/Athens")
    created_at: Mapped[datetime] = _now()


class AppUser(Base):
    """``user`` in the plan; renamed because ``user`` is reserved in Postgres."""

    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("store_id", "email"),
        CheckConstraint(_in("role", ROLES), name="role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = _store_fk()
    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = _now()


class Product(Base):
    __tablename__ = "product"
    __table_args__ = (
        UniqueConstraint("store_id", "ean"),
        CheckConstraint(f"pack_unit IS NULL OR {_in('pack_unit', PACK_UNITS)}", name="pack_unit"),
        CheckConstraint("pack_qty IS NULL OR pack_qty > 0", name="pack_qty_positive"),
        CheckConstraint(
            "NOT unit_price_required OR (pack_qty IS NOT NULL AND pack_unit IS NOT NULL)",
            name="unit_price_needs_pack",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = _store_fk()
    ean: Mapped[str] = mapped_column(String(14))
    name: Mapped[str] = mapped_column(String(300))
    brand: Mapped[str | None] = mapped_column(String(200))
    pack_qty: Mapped[int | None] = mapped_column(Integer)  # in pack_unit
    pack_unit: Mapped[str | None] = mapped_column(String(8))
    unit_price_required: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_rx: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Set by a manual price edit, cleared by the next applied import (section 6).
    manually_changed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Price(Base):
    """Current price state, one row per product."""

    __tablename__ = "price"
    __table_args__ = (
        CheckConstraint("regular_cents >= 0", name="regular_nonneg"),
        CheckConstraint("promo_cents IS NULL OR promo_cents >= 0", name="promo_nonneg"),
        CheckConstraint("promo_prior_cents IS NULL OR promo_prior_cents >= 0", name="prior_nonneg"),
        CheckConstraint(
            "(promo_cents IS NULL AND promo_from IS NULL AND promo_to IS NULL)"
            " OR (promo_cents IS NOT NULL AND promo_from IS NOT NULL"
            " AND promo_to IS NOT NULL AND promo_to > promo_from)",
            name="promo_complete",
        ),
        CheckConstraint(
            "promo_prior_cents IS NULL OR promo_cents IS NOT NULL", name="prior_needs_promo"
        ),
    )

    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), primary_key=True)
    store_id: Mapped[int] = _store_fk()
    regular_cents: Mapped[int] = mapped_column(Integer)
    promo_cents: Mapped[int | None] = mapped_column(Integer)
    promo_from: Mapped[datetime | None]
    promo_to: Mapped[datetime | None]
    # Prior price entered by an admin for this promo when history is under 30 days.
    # Cleared whenever the promo ends or changes; setting it writes an event_log row.
    promo_prior_cents: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class PriceHistory(Base):
    """Append-only; a database trigger rejects UPDATE and DELETE."""

    __tablename__ = "price_history"
    __table_args__ = (
        CheckConstraint("effective_cents >= 0", name="effective_nonneg"),
        CheckConstraint(_in("kind", PRICE_KINDS), name="kind"),
        CheckConstraint(_in("source", PRICE_SOURCES), name="source"),
        Index("ix_price_history_product_valid_from", "product_id", "valid_from"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = _store_fk()
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"))
    effective_cents: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    valid_from: Mapped[datetime]
    source: Mapped[str] = mapped_column(String(16))
    import_run_id: Mapped[int | None] = mapped_column(ForeignKey("import_run.id"))
    created_at: Mapped[datetime] = _now()


class TagTypeRow(Base):
    __tablename__ = "tag_type"
    __table_args__ = (
        UniqueConstraint("store_id", "key"),
        CheckConstraint(_in("palette", PALETTES), name="palette"),
        CheckConstraint("width > 0 AND height > 0", name="size_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = _store_fk()
    key: Mapped[str] = mapped_column(String(64))  # e.g. "gicisky-2.9-bwr"
    vendor: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    palette: Mapped[str] = mapped_column(String(8))


class Tag(Base):
    __tablename__ = "tag"
    __table_args__ = (
        UniqueConstraint("vendor_id"),
        UniqueConstraint("store_id", "short_code"),
        CheckConstraint("battery_pct IS NULL OR battery_pct BETWEEN 0 AND 100", name="battery_pct"),
        CheckConstraint("length(displayed_hash) = 64", name="displayed_hash_len"),
        CheckConstraint("length(desired_hash) = 64", name="desired_hash_len"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = _store_fk()
    vendor_id: Mapped[str] = mapped_column(String(64))  # BLE address or vendor serial
    tag_type_id: Mapped[int] = mapped_column(ForeignKey("tag_type.id"))
    short_code: Mapped[str] = mapped_column(String(16))
    last_seen_at: Mapped[datetime | None]
    rssi: Mapped[int | None] = mapped_column(Integer)
    battery_mv: Mapped[int | None] = mapped_column(Integer)
    battery_pct: Mapped[int | None] = mapped_column(Integer)
    displayed_hash: Mapped[str | None] = mapped_column(String(64))
    desired_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now()


class Template(Base):
    """Templates are code-defined; a row records which key/version exists per tag type."""

    __tablename__ = "template"
    __table_args__ = (UniqueConstraint("store_id", "key", "version", "tag_type_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = _store_fk()
    key: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    tag_type_id: Mapped[int] = mapped_column(ForeignKey("tag_type.id"))


class Binding(Base):
    """One product per tag; a product may have several tags."""

    __tablename__ = "binding"
    __table_args__ = (UniqueConstraint("tag_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = _store_fk()
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), index=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("template.id"))
    bound_at: Mapped[datetime] = _now()
    bound_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))


class RenderRow(Base):
    """Content-addressed image store."""

    __tablename__ = "render"
    __table_args__ = (CheckConstraint("length(hash) = 64", name="hash_len"),)

    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    store_id: Mapped[int] = _store_fk()
    tag_type_id: Mapped[int] = mapped_column(ForeignKey("tag_type.id"))
    png: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = _now()


class UpdateJob(Base):
    """See ``core.domain.reconciler`` for the state machine."""

    __tablename__ = "update_job"
    __table_args__ = (
        CheckConstraint(_in("state", JOB_STATES), name="state"),
        CheckConstraint("attempts >= 0", name="attempts_nonneg"),
        # At most one active job per tag (queued, claimed or failed).
        Index(
            "uq_update_job_active_per_tag",
            "tag_id",
            unique=True,
            postgresql_where=text(f"state IN ({', '.join(repr(s) for s in ACTIVE_JOB_STATES)})"),
        ),
        Index("ix_update_job_state_next_attempt_at", "state", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = _store_fk()
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id"))
    render_hash: Mapped[str] = mapped_column(ForeignKey("render.hash"))
    state: Mapped[str] = mapped_column(String(16), server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None]
    claimed_at: Mapped[datetime | None]
    error_code: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    gateway_id: Mapped[int | None] = mapped_column(ForeignKey("gateway.id"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class ImportProfile(Base):
    """Saved column mapping for CSV imports (section 6, step 1). Not in section 5's table."""

    __tablename__ = "import_profile"
    __table_args__ = (UniqueConstraint("store_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = _store_fk()
    name: Mapped[str] = mapped_column(String(100))
    # e.g. {"ean": "Barcode", "name": "Περιγραφή", "price": "Λιανική", ...}
    mapping: Mapped[dict[str, Any]]
    encoding: Mapped[str | None] = mapped_column(String(32))  # None: detect
    delimiter: Mapped[str | None] = mapped_column(String(1))  # None: detect
    created_at: Mapped[datetime] = _now()


class ImportRun(Base):
    __tablename__ = "import_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = _store_fk()
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("import_profile.id"))
    filename: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime] = _now()
    applied_at: Mapped[datetime | None]
    # {"new": n, "changed": n, "unchanged": n, "rejected": n, "missing": n}
    counts: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))


class GatewayRow(Base):
    __tablename__ = "gateway"
    __table_args__ = (UniqueConstraint("store_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = _store_fk()
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(255))
    last_heartbeat_at: Mapped[datetime | None]
    version: Mapped[str | None] = mapped_column(String(64))
    driver: Mapped[str | None] = mapped_column(String(32))


class EventLog(Base):
    """Audit trail. Append-only; a database trigger rejects UPDATE and DELETE."""

    __tablename__ = "event_log"
    __table_args__ = (Index("ix_event_log_kind_at", "kind", "at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = _store_fk()
    at: Mapped[datetime] = _now()
    actor: Mapped[str] = mapped_column(String(320))  # user email, "gateway:<name>" or "system"
    kind: Mapped[str] = mapped_column(String(64))  # e.g. "price.manual", "promo.prior_set"
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))


#: Tables whose rows may never be updated or deleted.
APPEND_ONLY_TABLES = ("price_history", "event_log")
