"""Content-addressed render store (PLAN.md section 7, steps 2 and 4).

A label is rendered at most once: its hash is computed from the inputs first, and an
existing ``render`` row with that hash is reused. Functions here never commit; they
run in the caller's transaction.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from core.db.models import DEFAULT_STORE_ID, RenderRow, TagTypeRow
from core.render import LabelData, Render, label_hash, render
from shared.tag_types import TagType


def tag_type_id(session: Session, tag_type: TagType, store_id: int = DEFAULT_STORE_ID) -> int:
    """The ``tag_type`` row id for ``tag_type.key``. Raises ``LookupError`` if missing."""
    found = session.scalar(
        select(TagTypeRow.id).where(TagTypeRow.store_id == store_id, TagTypeRow.key == tag_type.key)
    )
    if found is None:
        raise LookupError(f"no tag_type row with key {tag_type.key!r}")
    return found


def save_render(
    session: Session, rendered: Render, tag_type_row_id: int, store_id: int = DEFAULT_STORE_ID
) -> None:
    """Insert ``rendered`` unless a row with its hash exists. Safe against a concurrent
    insert of the same hash: the same hash always means the same image."""
    session.execute(
        insert(RenderRow)
        .values(
            hash=rendered.hash, store_id=store_id, tag_type_id=tag_type_row_id, png=rendered.png
        )
        .on_conflict_do_nothing(index_elements=[RenderRow.hash])
    )


def ensure_render(
    session: Session, label: LabelData, tag_type: TagType, store_id: int = DEFAULT_STORE_ID
) -> str:
    """The hash of ``label`` on ``tag_type``, rendering and storing it only if no
    ``render`` row has that hash yet. Raises what ``render`` raises (``ValueError``)
    and ``LookupError`` for an unknown tag type."""
    digest = label_hash(label, tag_type)
    if session.get(RenderRow, digest) is not None:
        return digest
    row_id = tag_type_id(session, tag_type, store_id)
    save_render(session, render(label, tag_type), row_id, store_id)
    return digest


def load_png(session: Session, digest: str) -> bytes | None:
    """The stored PNG for ``digest``, or ``None``."""
    return session.scalar(select(RenderRow.png).where(RenderRow.hash == digest))
