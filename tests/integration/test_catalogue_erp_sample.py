"""M1 acceptance on the trimmed real ERP sample (PLAN.md section 10, row M1).

``tests/fixtures/erp_sample.csv`` is added by Tasos (section 12). Its column names are
not known in advance, so it comes with ``erp_sample.profile.json``::

    {"mapping": {"ean": "...", "name": "...", "price": "...", "pack_qty": "...",
                 "pack_unit": "..."},
     "encoding": null, "delimiter": null}

Until the sample exists these tests are skipped.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.db.models import PriceHistory
from core.importers import ImportSettings, parse_import
from core.services import catalogue as svc

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE = FIXTURES / "erp_sample.csv"
SAMPLE_PROFILE = FIXTURES / "erp_sample.profile.json"
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)

needs_sample = pytest.mark.skipif(
    not SAMPLE.exists(),
    reason="tests/fixtures/erp_sample.csv is not in the repository yet (Tasos adds it, "
    "PLAN.md section 12); M1's real-sample criterion cannot be checked until then",
)


def _settings() -> ImportSettings:
    if not SAMPLE_PROFILE.exists():
        pytest.fail(f"{SAMPLE.name} needs {SAMPLE_PROFILE.name} with its column mapping")
    stored = json.loads(SAMPLE_PROFILE.read_bytes().decode("utf-8"))
    return ImportSettings.create(stored["mapping"], stored.get("encoding"), stored.get("delimiter"))


@needs_sample
def test_erp_sample_parses() -> None:
    parsed = parse_import(SAMPLE.read_bytes(), _settings())
    assert len(parsed.rows) >= 20, [r.reason for r in parsed.rejected]
    assert not parsed.rejected, [(r.line, r.reason) for r in parsed.rejected]


@needs_sample
@pytest.mark.db
def test_erp_sample_imports_and_reimports_unchanged(db: Session) -> None:
    profile = svc.save_profile(db, "erp-sample", _settings())
    data = SAMPLE.read_bytes()
    first = svc.apply_import(db, data, SAMPLE.name, profile, actor="system", now=NOW)
    assert first.run.counts["new"] >= 20
    history = db.scalar(select(func.count()).select_from(PriceHistory))
    assert history == first.run.counts["new"]
    again = svc.apply_import(
        db, data, SAMPLE.name, profile, actor="system", now=NOW + timedelta(days=1)
    )
    assert again.run.counts["new"] == again.run.counts["changed"] == 0
    assert db.scalar(select(func.count()).select_from(PriceHistory)) == history
