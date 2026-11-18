"""Tag types: the ``tag_type`` rows discovery matches on ``(vendor, model)``.

No migration seeds a tag type: the real Gicisky variant is still unknown (PLAN.md
section 12), so the shop's types are added with ``python -m core.tools.tag_types``
once the tags have arrived, and tests add the fake driver's type with ``add_tag_type``
(docs/decisions/0014). Functions here never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.db.models import DEFAULT_STORE_ID, EventLog, TagTypeRow
from core.services.queue import tag_type_of
from shared.tag_types import TagType


class TagTypeConflict(ValueError):
    """A different tag type already uses this key, or this vendor and model."""


@dataclass(frozen=True, slots=True)
class TagTypeAdded:
    row: TagTypeRow
    #: ``False`` when an identical row already existed; nothing was written then.
    created: bool


def add_tag_type(
    session: Session,
    tag_type: TagType,
    *,
    actor: str,
    now: datetime,
    store_id: int = DEFAULT_STORE_ID,
) -> TagTypeAdded:
    """Add ``tag_type``, idempotently.

    An identical row (same key, vendor, model, size and palette) is returned as it is,
    with ``created=False`` and no event. A row that shares the key, or the vendor and
    model, but differs in anything else raises ``TagTypeConflict``: renders and tags
    already refer to it, so it is never changed in place. A new row is logged as
    ``tag_type.added``. Two identical adds at once both succeed: the one that loses the
    insert race finds the other's row and returns it with ``created=False``.
    """
    found = _find(session, tag_type, store_id)
    if found is not None:
        return found
    row = TagTypeRow(
        store_id=store_id,
        key=tag_type.key,
        vendor=tag_type.vendor,
        model=tag_type.model,
        width=tag_type.width,
        height=tag_type.height,
        palette=tag_type.palette.value,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        # A concurrent add committed the same key or vendor and model first.
        found = _find(session, tag_type, store_id)
        if found is not None:
            return found
        raise TagTypeConflict(f"tag type {tag_type.key!r} was added concurrently") from None
    session.add(
        EventLog(
            store_id=store_id,
            at=now,
            actor=actor,
            kind="tag_type.added",
            payload={"tag_type_id": row.id, **tag_type.model_dump(mode="json")},
        )
    )
    session.flush()
    return TagTypeAdded(row, created=True)


def _find(session: Session, tag_type: TagType, store_id: int) -> TagTypeAdded | None:
    """The identical existing row, ``None`` if there is none; raises
    ``TagTypeConflict`` for a clashing one."""
    existing = list(
        session.scalars(
            select(TagTypeRow)
            .where(
                TagTypeRow.store_id == store_id,
                or_(
                    TagTypeRow.key == tag_type.key,
                    and_(TagTypeRow.vendor == tag_type.vendor, TagTypeRow.model == tag_type.model),
                ),
            )
            .order_by(TagTypeRow.id)
        )
    )
    for row in existing:
        if tag_type_of(row) == tag_type:
            return TagTypeAdded(row, created=False)
    if existing:
        other = tag_type_of(existing[0])
        raise TagTypeConflict(
            f"tag type {other.key!r} ({other.vendor} {other.model}, {other.width}x"
            f"{other.height} {other.palette.value}) already uses this key or vendor and model"
        )
    return None


def list_tag_types(session: Session, store_id: int = DEFAULT_STORE_ID) -> list[TagType]:
    """Every tag type of the store, by key."""
    return [
        tag_type_of(row)
        for row in session.scalars(
            select(TagTypeRow).where(TagTypeRow.store_id == store_id).order_by(TagTypeRow.key)
        )
    ]
