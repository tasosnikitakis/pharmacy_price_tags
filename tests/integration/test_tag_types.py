"""The tag-type service and its admin tool, ``python -m core.tools.tag_types``."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.db.models import EventLog, Tag, TagTypeRow
from core.services.tag_types import TagTypeConflict, add_tag_type, list_tag_types
from core.services.tags import record_seen_tags
from core.tools import tag_types as tool
from shared.gateway_api import SeenTagReport
from tests.e2e.support import T0, FakePipeline
from tests.e2e.wave1_support import fake_tag_type

pytestmark = pytest.mark.db


def _events(db: Session) -> list[EventLog]:
    return list(
        db.scalars(select(EventLog).where(EventLog.kind == "tag_type.added").order_by(EventLog.id))
    )


def test_add_is_idempotent_and_logged_once(db: Session) -> None:
    tag_type = fake_tag_type()
    first = add_tag_type(db, tag_type, actor="admin@example.test", now=T0)
    again = add_tag_type(db, tag_type, actor="admin@example.test", now=T0 + timedelta(hours=1))
    assert first.created and not again.created
    assert first.row.id == again.row.id
    [event] = [e for e in _events(db) if e.payload["key"] == tag_type.key]
    assert event.actor == "admin@example.test" and event.at == T0
    assert event.payload == {"tag_type_id": first.row.id, **tag_type.model_dump(mode="json")}
    assert tag_type in list_tag_types(db)


def test_a_clashing_type_is_refused_and_nothing_changes(db: Session) -> None:
    tag_type = fake_tag_type()
    add_tag_type(db, tag_type, actor="test", now=T0)
    other_size = tag_type.model_copy(update={"height": 152})
    same_vendor_model = fake_tag_type().model_copy(
        update={"vendor": tag_type.vendor, "model": tag_type.model}
    )
    for clash in (other_size, same_vendor_model):
        with pytest.raises(TagTypeConflict):
            add_tag_type(db, clash, actor="test", now=T0)
    rows = db.scalars(
        select(TagTypeRow).where(
            (TagTypeRow.key == tag_type.key) | (TagTypeRow.model == tag_type.model)
        )
    ).all()
    assert [(r.key, r.height) for r in rows] == [(tag_type.key, 128)]


def test_discovery_creates_tags_of_an_added_type(db: Session) -> None:
    tag_type = fake_tag_type()
    row = add_tag_type(db, tag_type, actor="test", now=T0).row
    report = SeenTagReport(vendor_id="TT-1", vendor="fake", model=tag_type.model, seen_at=T0)
    assert record_seen_tags(db, [report], FakePipeline(), T0).created == 1
    tag = db.scalars(select(Tag).where(Tag.vendor_id == "TT-1")).one()
    assert tag.tag_type_id == row.id and tag.desired_hash is not None


def _argv(tag_type_key: str, model: str, palette: str = "BWR") -> list[str]:
    return [
        "add", "--key", tag_type_key, "--vendor", "fake", "--model", model,
        "--width", "296", "--height", "128", "--palette", palette,
    ]  # fmt: skip


def test_tool_adds_lists_and_refuses(db: Session) -> None:
    t = fake_tag_type()
    now = datetime(2026, 12, 1, 9, 0, tzinfo=T0.tzinfo)
    assert tool.run(db, _argv(t.key, t.model), now) == (0, f"added: {t.key}")
    assert tool.run(db, _argv(t.key, t.model), now) == (
        0,
        f"already present, unchanged: {t.key}",
    )
    code, output = tool.run(db, _argv(t.key, t.model, "BWRY"), now)
    assert code == 1 and output.startswith("not added:")
    code, output = tool.run(db, ["list"])
    assert code == 0 and f"{t.key}\tfake\t{t.model}\t296x128\tBWR" in output.splitlines()
    [event] = [e for e in _events(db) if e.payload["key"] == t.key]
    assert event.actor == tool.ACTOR


def test_tool_reports_an_invalid_type(db: Session) -> None:
    code, output = tool.run(db, _argv("k" * 65, "m"))
    assert code == 2 and output.startswith("invalid tag type: key")
