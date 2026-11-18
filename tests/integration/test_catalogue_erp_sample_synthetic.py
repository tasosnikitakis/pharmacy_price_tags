"""Applying ``tests/fixtures/erp_sample_synthetic.csv`` to the database, then
re-importing it: the second import must show and write no changes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.db.models import PriceHistory, Product
from core.services import catalogue as svc
from tests.unit.test_importers_erp_sample_synthetic import SAMPLE, sample_settings

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


def _count(db: Session, model: type[Product] | type[PriceHistory]) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def test_erp_sample_synthetic_imports_then_reimports_unchanged(db: Session) -> None:
    profile = svc.save_profile(db, "erp-sample-synthetic", sample_settings())
    data = SAMPLE.read_bytes()

    first = svc.apply_import(db, data, SAMPLE.name, profile, actor="system", now=NOW)
    assert first.run.counts["new"] == 29
    assert first.run.counts["rejected"] == 8
    assert _count(db, Product) == 29
    history = _count(db, PriceHistory)
    assert history == 29

    preview = svc.preview_import(db, data, SAMPLE.name, profile).diff
    assert (len(preview.new), len(preview.changed), len(preview.unchanged)) == (0, 0, 29)
    assert preview.missing == []

    again = svc.apply_import(
        db, data, SAMPLE.name, profile, actor="system", now=NOW + timedelta(days=1)
    )
    assert (again.run.counts["new"], again.run.counts["changed"]) == (0, 0)
    assert again.run.counts["unchanged"] == 29
    assert _count(db, PriceHistory) == history
