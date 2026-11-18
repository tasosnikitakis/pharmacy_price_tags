"""M5's first criterion, "A staff user can bind a product to a tag from a phone
unaided", as far as a machine can show it (a person on a real phone checks the rest).

Everything runs for real except the radio and the camera: the core app in a uvicorn
server, the gateway agent with the fake driver polling it over HTTP, the pricing rules
and the renderer. Chromium (Node's Playwright) emulates a phone 390 px wide with touch.
The test only taps and types, as a person would:

1. log in as staff;
2. type the product's EAN (the camera cannot run here; the typed field is the
   documented fallback, the same one a USB scanner types into);
3. type the short code the unbound tag shows on its screen (read from the fake tag's
   PNG's source, the tag's row);
4. keep the default template and confirm;
5. wait until the page says done.

Then the fake driver's PNG for the tag must be exactly the expected label, and no page
of the flow may scroll sideways. The failure path: the fake driver fails every push and
the page shows "failed" with the Greek reason.

The server shares the test's rolled-back session; one lock serialises every request
and the test's own database reads.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest
import uvicorn
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.app.i18n import t
from core.app.uploads import UploadStore
from core.db.models import Binding, Price, Product, Tag
from core.db.session import get_session
from core.domain.pricing import build_label_data, unassigned_label_data
from core.render import render
from core.render.label import TemplateKey
from core.services.catalogue import history_of, price_state, product_facts
from core.services.gateway_auth import ensure_gateway
from core.services.queue import tag_type_of
from gateway.agent import Agent, AgentTimings, CoreClient, RenderCache
from gateway.drivers.base import PushError
from gateway.drivers.fake import FakeDriver, FakeTag
from shared.tag_types import TagType
from tests.e2e.support import auth, token
from tests.e2e.test_ui_browser import _free_port, require_playwright
from tests.integration.test_ui_helpers import (
    PASSWORD,
    T0,
    Clock,
    add_product,
    add_tag_type_row,
    add_user,
    build_ui_app,
    suffix,
)

pytestmark = pytest.mark.db

R = TypeVar("R")

SCRIPT = r"""
const { chromium, devices } = require('playwright');
const [base, email, password, ean, code, expected] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch();
  const result = { overflow: {}, smallTargets: {} };
  try {
    const context = await browser.newContext({
      ...devices['Pixel 7'], viewport: { width: 390, height: 844 }, locale: 'el-GR',
    });
    const page = await context.newPage();
    const check = async (step) => {
      result.overflow[step] = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      // Touch targets in the flow's own content (not the top menu): at least 44 px high.
      result.smallTargets[step] = await page.evaluate(() =>
        [...document.querySelectorAll([
          'main button', 'main label.choice', 'main a.button-link',
          'main input:not([type=hidden]):not([type=radio]):not([type=checkbox])',
        ].join(','))]
          .filter((e) => e.offsetParent !== null && e.getBoundingClientRect().height < 44)
          .map((e) => e.id || e.name || e.textContent.trim()),
      );
    };
    await page.goto(`${base}/login`);
    await check('login');
    await page.fill('#email', email);
    await page.fill('#password', password);
    await page.tap('button[type=submit]');
    await page.waitForURL(`${base}/`);
    await check('dashboard');

    await page.tap('nav a[href="/bind"]');
    await page.locator('#ean').waitFor();
    await check('ean');
    await page.fill('#ean', ean);
    await page.tap('#ean-form button[type=submit]');

    await page.locator('#code').waitFor();
    result.product = await page.locator('#product-h').innerText();
    await check('code');
    await page.fill('#code', code.toLowerCase());
    await page.tap('#code-form button[type=submit]');

    await page.locator('#confirm').waitFor();
    await page.waitForFunction(() => {
      const i = document.querySelector('#label-preview');
      return i && i.complete && i.naturalWidth > 0;
    });
    result.previewWidth = await page.locator('#label-preview').evaluate((i) => i.naturalWidth);
    result.template = await page.locator('input[name=template]:checked').getAttribute('value');
    await check('confirm');
    await page.tap('#confirm');

    const box = page.locator('#bind-progress');
    await box.waitFor();
    result.firstState = await box.getAttribute('data-state');
    result.url = page.url();
    result.flash = await page.locator('.flash-success').first().innerText();
    await check('progress');
    await page.waitForSelector(`#bind-progress[data-state="${expected}"]`, { timeout: 30000 });
    result.finalText = await page.locator('#bind-progress').innerText();
    await check('final');
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(result));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@dataclass(frozen=True)
class TagState:
    id: int
    short_code: str
    desired_hash: str | None
    displayed_hash: str | None


@dataclass(frozen=True)
class ProductState:
    id: int
    ean: str
    name: str


@dataclass
class Shop:
    """A running core with a fake-driver gateway agent polling it."""

    base: str
    db: Session
    lock: threading.Lock
    driver: FakeDriver
    tag_type: TagType
    vendor_id: str

    def read(self, query: Callable[[Session], R]) -> R:
        """Run ``query`` on the shared session while no request is using it."""
        with self.lock:
            self.db.expire_all()
            return query(self.db)

    def find_tag(self) -> TagState | None:
        """The tag discovery created, as plain values read under the lock (an ORM object
        would reload expired attributes later, outside the lock)."""

        def query(db: Session) -> TagState | None:
            tag = db.scalars(select(Tag).where(Tag.vendor_id == self.vendor_id)).one_or_none()
            if tag is None:
                return None
            return TagState(tag.id, tag.short_code, tag.desired_hash, tag.displayed_hash)

        return self.read(query)

    def tag(self) -> TagState:
        tag = self.find_tag()
        assert tag is not None, "discovery has not created the tag"
        return tag

    def wait_for(self, what: str, condition: Callable[[], bool], timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline, f"timed out waiting for {what}"
            time.sleep(0.1)


def _start_server(db: Session, tmp_path: Path, lock: threading.Lock) -> Iterator[str]:
    app = build_ui_app(db, Clock(), UploadStore(tmp_path / "uploads"))

    def _session() -> Iterator[Session]:
        with lock:
            yield db

    app.dependency_overrides[get_session] = _session
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _start_agent(base: str, driver: FakeDriver, cache: Path) -> Iterator[None]:
    """The real gateway agent loop in its own thread and event loop, over real HTTP."""
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    timings = AgentTimings(
        poll_interval_s=0.3, discovery_interval_s=3600, heartbeat_interval_s=30,
        error_backoff_s=0.5, report_retry_delay_s=0.1,
    )  # fmt: skip

    async def main() -> None:
        async with httpx.AsyncClient(base_url=base, headers=auth(), timeout=10) as http:
            agent = Agent(CoreClient(http), driver, RenderCache(cache), timings=timings)
            await agent.run(stop)

    thread = threading.Thread(target=loop.run_until_complete, args=(main(),), daemon=True)
    thread.start()
    try:
        yield
    finally:
        loop.call_soon_threadsafe(stop.set)
        thread.join(timeout=10)
        loop.close()


@pytest.fixture
def shop_factory(db: Session, tmp_path: Path) -> Iterator[Callable[[float], Shop]]:
    started: list[Iterator[object]] = []

    def start(fail_rate: float) -> Shop:
        lock = threading.Lock()
        tag_type = add_tag_type_row(db)
        ensure_gateway(db, token())
        db.commit()
        vendor_id = f"FAKE-{suffix()}"
        driver = FakeDriver(
            [FakeTag(vendor_id, tag_type.model, rssi=-58, battery_pct=90)],
            output_dir=tmp_path / "fake_tags", fail_rate=fail_rate,
            fail_with=PushError.TIMEOUT, seed=1,
        )  # fmt: skip
        server = _start_server(db, tmp_path, lock)
        base = next(server)
        agent = _start_agent(base, driver, tmp_path / "gateway_cache")
        next(agent)
        started.extend([agent, server])  # stopped in this order: agent first
        return Shop(base, db, lock, driver, tag_type_of(tag_type), vendor_id)

    yield start
    for running in started:
        with pytest.raises(StopIteration):
            next(running)


def _run(
    shop: Shop, email: str, ean: str, code: str, expected: str, tmp_path: Path
) -> dict[str, Any]:
    node, env = require_playwright()
    script = tmp_path / "ui_phone.js"
    script.write_text(SCRIPT, encoding="utf-8")
    run = subprocess.run(
        [node, str(script), shop.base, email, PASSWORD, ean, code, expected],
        env=env, capture_output=True, text=True, timeout=180, check=False,
    )  # fmt: skip
    assert run.returncode == 0, run.stderr
    result: dict[str, Any] = json.loads(run.stdout.strip().splitlines()[-1])
    return result


def _assert_phone_friendly(result: dict[str, Any]) -> None:
    assert result["overflow"], "no step was measured"
    for step, overflow in result["overflow"].items():
        assert overflow <= 0, f"the {step} page scrolls sideways at 390 px"
    for step, small in result["smallTargets"].items():
        assert small == [], f"touch targets under 44 px on the {step} page: {small}"


def _seed_product(shop: Shop) -> ProductState:
    with shop.lock:
        product = add_product(shop.db, f"Κρέμα χεριών {suffix()}", 1290)
        return ProductState(product.id, product.ean, product.name)


def test_staff_binds_from_a_phone(db: Session, shop_factory: Callable[[float], Shop],
                                  tmp_path: Path) -> None:  # fmt: skip
    require_playwright()
    email = add_user(db, "staff").email  # before the server shares the session
    shop = shop_factory(0.0)
    product = _seed_product(shop)

    # Discovery creates the tag; it is pushed its "unassigned" label with its code.
    shop.wait_for(
        "the unassigned label",
        lambda: (found := shop.find_tag()) is not None and found.displayed_hash is not None,
    )
    tag = shop.tag()
    assert tag.displayed_hash == tag.desired_hash
    shown = render(unassigned_label_data(tag.short_code, shop.tag_type.model), shop.tag_type)
    assert shop.driver.image_path(shop.vendor_id).read_bytes() == shown.png

    result = _run(shop, email, product.ean, tag.short_code, "up_to_date", tmp_path)

    _assert_phone_friendly(result)
    assert result["product"] == product.name
    assert result["template"] == "standard"  # the default for a plain product
    assert result["previewWidth"] == 296 * 3
    assert result["url"].endswith(f"/bind/tags/{tag.id}")
    assert t("bind.bound", code=tag.short_code, name=product.name) in result["flash"]
    assert result["firstState"] in ("pending", "up_to_date")
    assert t("bind.progress.up_to_date") in result["finalText"]

    # The tag now shows exactly the product's label.
    def expected_png(db: Session) -> bytes:
        row = db.get_one(Product, product.id)
        price = db.get_one(Price, product.id)
        label = build_label_data(
            product_facts(row), price_state(price), history_of(db, product.id),
            TemplateKey.STANDARD, T0,
        )  # fmt: skip
        return render(label, shop.tag_type).png

    expected = shop.read(expected_png)
    tag = shop.tag()
    assert tag.displayed_hash == tag.desired_hash
    assert shop.driver.image_path(shop.vendor_id).read_bytes() == expected
    assert shop.driver.pushes[-1] == (shop.vendor_id, True)


def test_failed_push_shows_the_greek_reason(db: Session, shop_factory: Callable[[float], Shop],
                                            tmp_path: Path) -> None:  # fmt: skip
    require_playwright()
    email = add_user(db, "staff").email  # before the server shares the session
    shop = shop_factory(1.0)  # every push times out
    product = _seed_product(shop)
    shop.wait_for("the tag", lambda: shop.find_tag() is not None)
    tag = shop.tag()

    result = _run(shop, email, product.ean, tag.short_code, "failed", tmp_path)

    _assert_phone_friendly(result)
    assert t("bind.progress.failed") in result["finalText"]
    assert t("bind.progress.reason", reason=t("push_error.timeout")) in result["finalText"]
    assert not shop.driver.image_path(shop.vendor_id).exists()
    # The bind itself was saved; only the push failed.
    bound = shop.read(
        lambda db: db.scalars(select(Binding.product_id).where(Binding.tag_id == tag.id)).one()
    )
    assert bound == product.id
