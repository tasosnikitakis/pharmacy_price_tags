"""Tags page (section 9): list, statuses, label images, retry and unbind."""

from __future__ import annotations

import io

import pytest
from PIL import Image
from sqlalchemy import select

from core.app.i18n import t
from core.db.models import Binding, EventLog, Tag, UpdateJob
from core.domain.pricing import unassigned_label_data
from core.render import label_hash
from core.services.admin_queries import TagStatus, tag_status
from core.services.queue import tag_type_of
from core.services.tags import unbind_tag
from tests.integration.test_ui_helpers import (
    T0,
    UI,
    add_product,
    add_tag,
    add_tag_type_row,
    suffix,
    ui_fixture,  # noqa: F401  (the "ui" fixture)
)

pytestmark = pytest.mark.db


def _jobs(ui: UI, tag_id: int) -> list[UpdateJob]:
    jobs = list(
        ui.db.scalars(select(UpdateJob).where(UpdateJob.tag_id == tag_id).order_by(UpdateJob.id))
    )
    for job in jobs:
        ui.db.refresh(job)
    return jobs


def _kill_job(ui: UI, tag: Tag) -> UpdateJob:
    """Make the tag's queued job dead, as after six failed pushes (test data only)."""
    job = _jobs(ui, tag.id)[-1]
    job.state, job.attempts, job.error_code = "dead", 6, "timeout"
    ui.db.commit()
    return job


def test_status_rules() -> None:
    assert tag_status("a" * 64, "a" * 64, "dead") is TagStatus.UP_TO_DATE
    assert tag_status("a" * 64, None, "queued") is TagStatus.PENDING
    assert tag_status("a" * 64, "b" * 64, "claimed") is TagStatus.PENDING
    assert tag_status("a" * 64, None, "failed") is TagStatus.FAILED
    assert tag_status("a" * 64, None, "dead") is TagStatus.DEAD
    assert tag_status(None, None, None) is TagStatus.PENDING


def test_list_shows_product_seen_rssi_battery_and_status(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    tag_type = add_tag_type_row(ui.db)
    bound = add_tag(ui.db, tag_type, product)
    bound.last_seen_at, bound.rssi, bound.battery_pct = T0, -67, 81
    unbound = add_tag(ui.db, tag_type)
    unbound.displayed_hash = unbound.desired_hash
    ui.db.commit()
    ui.as_staff()
    page = ui.client.get("/tags").text
    row = page.split(f">{bound.short_code}<", 1)[1].split("</tr>", 1)[0]
    assert product.name in row
    assert "01/12/2026 10:00" in row  # Athens time
    assert t("tags.rssi", value=-67) in row and t("tags.battery_pct", value=81) in row
    assert t("tags.status.pending") in row
    row = page.split(f">{unbound.short_code}<", 1)[1].split("</tr>", 1)[0]
    assert t("tags.unbound") in row and t("tags.status.up_to_date") in row
    filtered = ui.client.get("/tags", params={"status": "up_to_date"}).text
    assert unbound.short_code in filtered and bound.short_code not in filtered


def test_detail_shows_enlarged_labels(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    tag = add_tag(ui.db, add_tag_type_row(ui.db), product)
    ui.as_staff()
    page = ui.client.get(f"/tags/{tag.id}").text
    assert f"/tags/{tag.id}/label/desired.png?v={tag.desired_hash}" in page
    assert t("tags.detail.displayed_unknown") in page
    r = ui.client.get(f"/tags/{tag.id}/label/desired.png")
    with Image.open(io.BytesIO(r.content)) as image:
        assert image.size == (888, 384)
    assert ui.client.get(f"/tags/{tag.id}/label/displayed.png").status_code == 404
    # Staff see no admin actions.
    assert f"/tags/{tag.id}/unbind" not in page


def test_admin_retries_dead_job(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    tag = add_tag(ui.db, add_tag_type_row(ui.db), product)
    dead = _kill_job(ui, tag)
    token = ui.as_admin()
    page = ui.client.get(f"/tags/{tag.id}").text
    assert t("tags.status.dead") in page
    assert t("push_error.timeout") in page
    action = f"/tags/{tag.id}/jobs/{dead.id}/retry"
    assert action in page
    page = ui.client.post(action, data={"csrf_token": token}).text
    assert t("tags.retried") in page
    jobs = _jobs(ui, tag.id)
    assert jobs[-1].id != dead.id and jobs[-1].state == "queued"
    assert ui.db.scalars(select(EventLog).where(EventLog.kind == "job.retry")).first()
    # A second retry of the same job has nothing to do.
    page = ui.client.post(action, data={"csrf_token": token}).text
    assert t("error.job.not_retryable") in page


def test_admin_unbinds_tag(ui: UI) -> None:
    product = add_product(ui.db, f"Κρέμα {suffix()}", 1290)
    tag_type = add_tag_type_row(ui.db)
    tag = add_tag(ui.db, tag_type, product)
    token = ui.as_admin()
    page = ui.client.post(f"/tags/{tag.id}/unbind", data={"csrf_token": token}).text
    assert t("tags.unbound_done") in page
    assert ui.db.scalars(select(Binding).where(Binding.tag_id == tag.id)).first() is None
    ui.db.refresh(tag)
    unassigned = unassigned_label_data(tag.short_code, tag_type.model)
    assert tag.desired_hash == label_hash(unassigned, tag_type_of(tag_type))
    assert _jobs(ui, tag.id)[-1].render_hash == tag.desired_hash
    event = ui.db.scalars(select(EventLog).where(EventLog.kind == "tag.unbound")).all()[-1]
    assert event.payload == {"tag_id": tag.id, "previous_product_id": product.id}
    assert event.actor == ui.admin.email
    page = ui.client.post(f"/tags/{tag.id}/unbind", data={"csrf_token": token}).text
    assert t("error.tag.not_bound") in page


def test_unbind_service_on_unbound_tag_writes_nothing(ui: UI) -> None:
    tag = add_tag(ui.db, add_tag_type_row(ui.db))
    assert unbind_tag(ui.db, tag.id, actor="test", now=T0) is None


def test_unknown_tag_is_404(ui: UI) -> None:
    token = ui.as_admin()
    assert ui.client.get("/tags/999999999").status_code == 404
    assert ui.client.post("/tags/999999999/unbind", data={"csrf_token": token}).status_code == 404
