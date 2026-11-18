"""Dashboard (PLAN.md section 9), M5's second criterion: "dashboard numbers match the
database".

Tags are seeded in every state: up to date (bound and unbound), queued and claimed
jobs, a failed and a dead job, a bound tag whose label cannot be built, tags unseen for
over 24 hours or never, low batteries reported as a percentage and as millivolts, a
promo start the label is behind, and an old import. Every number on the page is then
compared with a count written here in plain SQL, not through ``admin_queries``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from core.app.i18n import t
from core.app.routers.ui_dashboard import LOW_BATTERY_MV, LOW_BATTERY_PCT
from core.config import get_settings
from core.db.models import GatewayRow, ImportRun, Price, Product, Tag, UpdateJob
from core.services.gateway_auth import ensure_gateway
from core.services.tags import bind_tag
from tests.e2e.support import token
from tests.integration.test_ui_helpers import (
    T0,
    UI,
    add_product,
    add_tag,
    add_tag_type_row,
    suffix,
    synthetic_ean,
    ui_fixture,  # noqa: F401  (the "ui" fixture)
)

pytestmark = pytest.mark.db

COUNTS_SQL = text(
    """
    WITH last_job AS (
        SELECT DISTINCT ON (tag_id) tag_id, state
        FROM update_job ORDER BY tag_id, id DESC
    ), t AS (
        SELECT tag.*, b.id AS binding_id, lj.state AS job_state,
               tag.desired_hash IS NOT NULL
                 AND tag.displayed_hash IS NOT DISTINCT FROM tag.desired_hash AS shows_desired
        FROM tag
        LEFT JOIN binding b ON b.tag_id = tag.id
        LEFT JOIN last_job lj ON lj.tag_id = tag.id
        WHERE tag.store_id = 1
    )
    SELECT
      count(*) AS total,
      count(*) FILTER (WHERE binding_id IS NOT NULL) AS bound,
      count(*) FILTER (WHERE binding_id IS NULL) AS unbound,
      count(*) FILTER (WHERE shows_desired) AS up_to_date,
      count(*) FILTER (WHERE desired_hash IS NULL) AS no_label,
      count(*) FILTER (WHERE desired_hash IS NOT NULL AND NOT shows_desired
                         AND job_state IN ('failed', 'dead')) AS failed,
      count(*) FILTER (WHERE desired_hash IS NOT NULL AND NOT shows_desired
                         AND job_state = 'dead') AS dead,
      count(*) FILTER (WHERE desired_hash IS NOT NULL AND NOT shows_desired
                         AND job_state IS DISTINCT FROM 'failed'
                         AND job_state IS DISTINCT FROM 'dead') AS pending,
      count(*) FILTER (WHERE desired_hash IS NOT NULL AND NOT shows_desired
                         AND job_state IN ('queued', 'claimed')) AS queued_or_claimed,
      count(*) FILTER (WHERE last_seen_at IS NULL
                         OR last_seen_at < CAST(:now AS timestamptz) - interval '24 hours')
        AS unseen,
      count(*) FILTER (WHERE (battery_pct IS NOT NULL AND battery_pct < :pct)
                         OR (battery_pct IS NULL AND battery_mv < :mv)) AS low_battery
    FROM t
    """
)


def sql_counts(db: Session, now: datetime) -> dict[str, int]:
    row = db.execute(COUNTS_SQL, {"now": now, "pct": LOW_BATTERY_PCT, "mv": LOW_BATTERY_MV})
    return dict(row.mappings().one())


def page_counts(html: str) -> dict[str, int]:
    found = dict(re.findall(r'id="count-([a-z_]+)">(\d+)<', html))
    return {k: int(v) for k, v in found.items()}


def _last_job(db: Session, tag: Tag) -> UpdateJob:
    job = db.scalars(
        select(UpdateJob).where(UpdateJob.tag_id == tag.id).order_by(UpdateJob.id.desc())
    ).first()
    assert job is not None
    return job


def _seed(ui: UI) -> dict[str, Any]:
    db = ui.db
    tag_type = add_tag_type_row(db)
    product = add_product(db, f"Κρέμα {suffix()}", 1290)
    seen = {"last_seen_at": T0 - timedelta(minutes=3)}

    def tag(bound: bool = True, **fields: object) -> Tag:
        new = add_tag(db, tag_type, product if bound else None)
        for name, value in {**seen, **fields}.items():
            setattr(new, name, value)
        db.commit()
        return new

    up_bound = tag(battery_pct=80)
    up_bound.displayed_hash = up_bound.desired_hash
    up_unbound = tag(bound=False, battery_mv=2900)
    up_unbound.displayed_hash = up_unbound.desired_hash
    queued = tag()  # a fresh tag's job is queued
    claimed = tag()
    _last_job(db, claimed).state = "claimed"
    failed = tag(battery_pct=LOW_BATTERY_PCT - 1)
    job = _last_job(db, failed)
    job.state, job.attempts, job.error_code = "failed", 1, "timeout"
    dead = tag(battery_pct=None, battery_mv=LOW_BATTERY_MV - 200)
    job = _last_job(db, dead)
    job.state, job.attempts, job.error_code = "dead", 6, "not_found"
    unseen_old = tag(last_seen_at=T0 - timedelta(hours=25))
    unseen_never = tag(bound=False, last_seen_at=None)
    seen_23h = tag(last_seen_at=T0 - timedelta(hours=23))

    # A bound product without a price: its label can never be built.
    no_price = Product(ean=synthetic_ean(), name=f"Χωρίς τιμή {suffix()}")
    db.add(no_price)
    db.flush()
    no_label = Tag(vendor_id=f"fake-{suffix()}", tag_type_id=tag_type.id,
                   short_code=suffix()[:6].upper(), last_seen_at=T0)  # fmt: skip
    db.add(no_label)
    db.flush()
    bind_tag(db, no_label.id, no_price.id, actor="test", now=T0)

    # A promo started half an hour ago, but the bound label was never refreshed for it
    # (as when every claim fails to build it); and one that started a minute ago, which
    # the next claim refreshes (inside the grace period, so not listed).
    behind = []
    for minutes in (30, 1):
        promo_product = add_product(db, f"Προσφορά {suffix()}", 1500)
        promo_tag = tag()
        promo_tag.desired_hash_changed_at = T0 - timedelta(hours=1)
        price = db.get_one(Price, promo_product.id)
        bind_tag(db, promo_tag.id, promo_product.id, actor="test", now=T0 - timedelta(hours=1))
        promo_tag.desired_hash_changed_at = T0 - timedelta(hours=1)
        price.promo_cents = 990
        price.promo_from = T0 - timedelta(minutes=minutes)
        price.promo_to = T0 + timedelta(days=3)
        behind.append(promo_tag)

    db.add(ImportRun(filename="old.csv", started_at=T0 - timedelta(days=10),
                     applied_at=T0 - timedelta(days=10)))  # fmt: skip
    db.commit()
    return {
        "up_bound": up_bound, "up_unbound": up_unbound, "queued": queued, "claimed": claimed,
        "failed": failed, "dead": dead, "unseen_old": unseen_old, "unseen_never": unseen_never,
        "seen_23h": seen_23h, "no_label": no_label, "behind": behind,
    }  # fmt: skip


def test_dashboard_numbers_match_the_database(ui: UI) -> None:
    tags = _seed(ui)
    ui.as_staff()
    before = sql_counts(ui.db, T0)
    page = ui.client.get("/").text
    shown = page_counts(page)
    expected = {k: before[k] for k in shown}
    assert shown == expected
    assert set(shown) == {"up_to_date", "pending", "failed", "dead", "no_label", "unseen",
                          "low_battery"}  # fmt: skip
    total = t("dashboard.counts.total", total=before["total"], bound=before["bound"],
              unbound=before["unbound"])  # fmt: skip
    assert total in page

    # The categories add up, each tag in exactly one; failed includes dead.
    assert (
        before["up_to_date"] + before["pending"] + before["failed"] + before["no_label"]
        == (before["total"])
    )
    assert before["bound"] + before["unbound"] == before["total"]
    # Our seeds are in the counts (other committed rows in the database may add to them).
    assert before["dead"] >= 1 and before["failed"] >= 2 and before["no_label"] >= 1
    assert before["unseen"] >= 2 and before["low_battery"] >= 2
    # Every pending tag of ours has a queued or claimed job (the brief's definition).
    assert before["pending"] == before["queued_or_claimed"]

    # The lists name the right tags.
    failed_list = page.split('id="failed-list"', 1)[1].split("</ul>", 1)[0]
    assert tags["failed"].short_code in failed_list and tags["dead"].short_code in failed_list
    assert t("push_error.timeout") in failed_list and t("push_error.not_found") in failed_list
    assert tags["queued"].short_code not in failed_list
    failing = page.split('id="failing-list"', 1)[1].split("</ul>", 1)[0]
    assert tags["no_label"].short_code in failing
    assert tags["behind"][0].short_code in failing
    assert t("dashboard.failing.promo_boundary", when="01/12/2026 09:30") in failing
    assert tags["behind"][1].short_code not in failing  # within the grace period
    unseen = page.split('id="unseen-list"', 1)[1].split("</ul>", 1)[0]
    assert tags["unseen_old"].short_code in unseen and tags["unseen_never"].short_code in unseen
    assert tags["seen_23h"].short_code not in unseen
    battery = page.split('id="battery-list"', 1)[1].split("</ul>", 1)[0]
    assert tags["failed"].short_code in battery and tags["dead"].short_code in battery
    assert tags["up_unbound"].short_code not in battery  # 2900 mV is fine

    # A change in the database changes the numbers the same way.
    tags["queued"].displayed_hash = tags["queued"].desired_hash
    ui.db.commit()
    after = sql_counts(ui.db, T0)
    assert after["up_to_date"] == before["up_to_date"] + 1
    assert after["pending"] == before["pending"] - 1
    assert page_counts(ui.client.get("/").text) == {k: after[k] for k in shown}


def test_stale_import_warning(ui: UI) -> None:
    ui.db.add(ImportRun(filename="old.csv", started_at=T0 - timedelta(days=10),
                        applied_at=T0 - timedelta(days=10)))  # fmt: skip
    ui.db.commit()
    newest = ui.db.scalar(text("SELECT max(applied_at) FROM import_run WHERE store_id = 1"))
    assert newest == T0 - timedelta(days=10), "a newer import is committed in this database"
    ui.as_staff()
    days = get_settings().stale_import_days
    page = ui.client.get("/").text
    assert 'id="import-warning"' in page
    assert t("dashboard.import.stale", days=days, age=t("age.days", n=10)) in page
    assert t("dashboard.import.last", when="21/11/2026 10:00", age=t("age.days", n=10)) in page

    # Exactly at the threshold is not stale yet; just past it is.
    ui.db.add(ImportRun(filename="new.csv", started_at=T0 - timedelta(days=days),
                        applied_at=T0 - timedelta(days=days)))  # fmt: skip
    ui.db.commit()
    assert 'id="import-warning"' not in ui.client.get("/").text
    ui.clock.now = T0 + timedelta(seconds=1)
    assert 'id="import-warning"' in ui.client.get("/").text
    # An import run that was only previewed (never applied) does not count.
    ui.db.add(ImportRun(filename="preview.csv", started_at=T0))
    ui.db.commit()
    assert 'id="import-warning"' in ui.client.get("/").text


def test_gateway_warning(ui: UI) -> None:
    ensure_gateway(ui.db, token())
    ui.db.flush()
    rows = list(ui.db.scalars(select(GatewayRow)))
    assert rows
    ui.as_staff()

    def heartbeat(when: datetime | None) -> str:
        ui.db.execute(update(GatewayRow).values(last_heartbeat_at=when))
        ui.db.commit()
        ui.db.expire_all()
        page: str = ui.client.get("/").text
        return page

    page = heartbeat(T0 - timedelta(minutes=1))
    assert 'id="gateway-warning"' not in page
    assert t("dashboard.gateway.last", when="01/12/2026 09:59") in page
    assert t("dashboard.gateway.online") in page
    page = heartbeat(T0 - timedelta(minutes=6))
    assert 'id="gateway-warning"' in page and t("dashboard.gateway.stale") in page
    assert t("dashboard.gateway.offline") in page
    page = heartbeat(None)
    assert 'id="gateway-warning"' in page and t("dashboard.gateway.never") in page


def test_dashboard_is_the_start_page_for_both_roles(ui: UI) -> None:
    assert ui.client.get("/", follow_redirects=False).status_code == 303  # login first
    ui.as_staff()
    page = ui.client.get("/")
    assert page.status_code == 200 and t("dashboard.title") in page.text
    assert 'href="/bind"' in page.text and 'href="/audit"' in page.text
    ui.as_admin()
    assert ui.client.get("/").status_code == 200
