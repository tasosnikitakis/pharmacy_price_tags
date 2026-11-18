"""Helpers and fixtures for the admin UI tests (imported by the ``test_ui_*`` modules).

The app runs on the test's own database session (rolled back afterwards), with a fixed
clock and the real label pipeline. Data is synthetic, with random suffixes so tests
never collide with rows already in the development database.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from core.app.main import create_app
from core.app.routers.gateway import get_label_pipeline, get_now
from core.app.uploads import UploadStore, get_upload_store
from core.db.models import AppUser, Product, Tag, TagTypeRow
from core.db.session import get_session
from core.domain.catalogue import ean_check_digit
from core.render.label import TemplateKey
from core.services.catalogue import edit_price
from core.services.tag_types import add_tag_type
from core.services.tags import bind_tag, real_label_pipeline, refresh_desired_hashes
from core.services.users import create_user
from shared.tag_types import Palette, TagType

#: 10:00 in Athens on a Tuesday.
T0 = datetime(2026, 12, 1, 8, 0, tzinfo=UTC)
PASSWORD = "correct horse battery"
_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


@dataclass
class Clock:
    now: datetime = T0


def build_ui_app(db: Session, clock: Clock, store: UploadStore) -> FastAPI:
    app = create_app()

    def _session() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_now] = lambda: clock.now
    app.dependency_overrides[get_label_pipeline] = lambda: real_label_pipeline
    app.dependency_overrides[get_upload_store] = lambda: store
    return app


def suffix() -> str:
    return uuid.uuid4().hex[:8]


def synthetic_ean() -> str:
    """A valid EAN-13 with an in-store prefix (29), so never a real product's."""
    body = f"29{uuid.uuid4().int % 10**10:010d}"
    return body + str(ean_check_digit(body))


def add_user(db: Session, role: str, *, active: bool = True) -> AppUser:
    user = create_user(db, f"{role}-{suffix()}@example.test", PASSWORD, role, actor="test", now=T0)
    user.active = active
    db.commit()
    return user


def csrf_of(html: str) -> str:
    found = _CSRF.search(html)
    assert found is not None, "page has no CSRF token"
    return found.group(1)


def login(client: TestClient, email: str, password: str = PASSWORD) -> str:
    """Log in and return the session's CSRF token."""
    page = client.get("/login")
    token = csrf_of(page.text)
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": token, "next": "/products"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return csrf_of(client.get("/products").text)


def add_product(
    db: Session,
    name: str,
    cents: int,
    *,
    since: datetime = T0 - timedelta(days=40),
    **fields: object,
) -> Product:
    """A product whose price has been ``cents`` since ``since`` (default: long enough
    for a 30-day prior price)."""
    product = Product(ean=synthetic_ean(), name=name, **fields)
    db.add(product)
    db.flush()
    edit_price(db, product.id, cents, actor="test", now=since)
    product.manually_changed_at = None
    db.commit()
    return product


def add_tag_type_row(db: Session, palette: Palette = Palette.BWR) -> TagTypeRow:
    s = suffix()
    tag_type = TagType(
        key=f"fake-2.9-{palette.value.lower()}-{s}", vendor="fake", model=f"2.9-{s}",
        width=296, height=128, palette=palette,
    )  # fmt: skip
    row = add_tag_type(db, tag_type, actor="test", now=T0).row
    db.commit()
    return row


def add_tag(db: Session, tag_type: TagTypeRow, product: Product | None = None) -> Tag:
    s = suffix()
    tag = Tag(vendor_id=f"fake-{s}", tag_type_id=tag_type.id, short_code=s[:6].upper())
    db.add(tag)
    db.flush()
    if product is not None:
        bind_tag(db, tag.id, product.id, actor="test", now=T0, template=TemplateKey.STANDARD)
    else:
        refresh_desired_hashes(db, real_label_pipeline, T0, tag_ids=[tag.id])
    db.commit()
    db.refresh(tag)
    return tag


@dataclass
class UI:
    client: TestClient
    db: Session
    clock: Clock
    admin: AppUser
    staff: AppUser
    store: UploadStore

    def as_admin(self) -> str:
        return login(self.client, self.admin.email)

    def as_staff(self) -> str:
        return login(self.client, self.staff.email)


@pytest.fixture(name="ui")
def ui_fixture(db: Session, tmp_path: Path) -> Iterator[UI]:
    clock = Clock()
    store = UploadStore(tmp_path / "uploads")
    admin = add_user(db, "admin")
    staff = add_user(db, "staff")
    # Not entered as a context manager: the lifespan would provision the gateway row
    # through its own (real) database session.
    client = TestClient(build_ui_app(db, clock, store))
    yield UI(client, db, clock, admin, staff, store)
