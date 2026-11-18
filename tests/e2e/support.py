"""Helpers for the gateway API and end-to-end tests: seed data, a clock, a fake renderer.

The fake label pipeline stands in for workstream B's renderer: it turns a tag's inputs
into a deterministic palette PNG whose hash depends on what the label would show.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import FastAPI
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.app.main import create_app
from core.app.routers.gateway import get_label_pipeline, get_now
from core.db.models import Binding, Price, Product, TagTypeRow, Template
from core.db.session import get_session
from core.render.label import TEMPLATE_VERSIONS, Render, TemplateKey
from core.services.gateway_auth import ensure_gateway
from core.services.tags import LabelOutcome, LabelPipeline, LabelRequest
from shared.tag_types import PALETTE_COLOURS, Palette

T0 = datetime(2026, 12, 1, 8, 0, tzinfo=UTC)


def token() -> str:
    return os.environ["GATEWAY_TOKEN"]


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {token()}"}


@dataclass
class Clock:
    now: datetime = T0


@dataclass
class FakePipeline:
    """A ``LabelPipeline`` that draws the label's content hash as pixels."""

    calls: list[int] = field(default_factory=list)

    def __call__(self, request: LabelRequest, now: datetime) -> LabelOutcome:
        self.calls.append(request.tag_id)
        if request.bound is None:
            content: dict[str, object] = {"t": "unassigned", "code": request.short_code}
        else:
            price = request.bound.price
            content = {
                "t": request.bound.template.value,
                "name": request.bound.product.name,
                "cents": price.regular_cents if price else None,
            }
        content["type"] = request.tag_type.key
        digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        tt = request.tag_type
        return LabelOutcome(
            Render(
                digest, tt.key, tt.width, tt.height, _png(digest, tt.palette, tt.width, tt.height)
            )
        )


def _png(digest: str, palette: Palette, width: int, height: int) -> bytes:
    colours = PALETTE_COLOURS[palette]
    img = Image.new("P", (width, height), 0)
    img.putpalette([c for rgb in colours for c in rgb])
    bits = bin(int(digest, 16))[2:].zfill(256)
    for i, bit in enumerate(bits):
        if bit == "1":
            img.putpixel((i % width, (i // width) * 2), 1)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


@dataclass
class World:
    """Seeded rows for one test: a fake tag type, its standard template, two products."""

    tag_type: TagTypeRow
    template: Template
    products: list[Product]

    @property
    def model(self) -> str:
        return self.tag_type.model


def seed(db: Session) -> World:
    suffix = uuid.uuid4().hex[:8]
    tag_type = TagTypeRow(
        key=f"fake-2.9-bwr-{suffix}", vendor="fake", model=f"2.9-bwr-{suffix}",
        width=296, height=128, palette="BWR",
    )  # fmt: skip
    db.add(tag_type)
    db.flush()
    template = Template(
        key=TemplateKey.STANDARD.value,
        version=TEMPLATE_VERSIONS[TemplateKey.STANDARD],
        tag_type_id=tag_type.id,
    )
    products = [Product(ean=f"52{suffix}{i:03d}"[:13], name=f"Κρέμα χεριών {i}") for i in range(2)]
    db.add_all([template, *products])
    db.flush()
    db.add_all(Price(product_id=p.id, regular_cents=450 + 100 * i) for i, p in enumerate(products))
    db.flush()
    ensure_gateway(db, token())
    return World(tag_type, template, products)


def bind(db: Session, world: World, tag_id: int, product: Product, now: datetime) -> None:
    db.add(
        Binding(tag_id=tag_id, product_id=product.id, template_id=world.template.id, bound_at=now)
    )
    db.flush()


def set_price(db: Session, product: Product, cents: int) -> None:
    price = db.scalars(select(Price).where(Price.product_id == product.id)).one()
    price.regular_cents = cents
    db.flush()


def build_app(db: Session, clock: Clock, pipeline: LabelPipeline) -> FastAPI:
    app = create_app()

    def _session() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_now] = lambda: clock.now
    app.dependency_overrides[get_label_pipeline] = lambda: pipeline
    return app
