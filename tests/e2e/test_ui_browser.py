"""Browser test of the admin UI with Playwright and Chromium.

An admin logs in, edits a price on the Products page, and the label preview changes.
The core runs in a background uvicorn server on the test's database session (rolled
back afterwards). The browser is driven by Node's ``playwright`` package (preinstalled
in cloud sessions, with Chromium); the test is skipped where it is missing.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.app.uploads import UploadStore
from core.db.models import Price
from core.db.session import get_session
from tests.integration.test_ui_helpers import (
    PASSWORD,
    Clock,
    add_product,
    add_tag_type_row,
    add_user,
    build_ui_app,
    suffix,
)

pytestmark = pytest.mark.db

SCRIPT = r"""
const { chromium } = require('playwright');
const [base, email, password, productId] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch();
  const result = {};
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    await page.goto(`${base}/login`);
    await page.fill('#email', email);
    await page.fill('#password', password);
    await page.click('button[type=submit]');
    await page.waitForURL(`${base}/products`);
    await page.goto(`${base}/products/${productId}`);
    const img = page.locator('#label-preview');
    await img.waitFor();
    result.before = await img.getAttribute('src');
    result.beforeLoaded = await img.evaluate((i) => i.complete && i.naturalWidth);
    // Phone width: the page must not scroll sideways.
    result.overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    await page.fill('#regular_price', '15,40');
    await page.click('form[action$="/price"] button[type=submit]');
    await page.waitForFunction(
      (old) => {
        const i = document.querySelector('#label-preview');
        return i && i.getAttribute('src') !== old && i.complete && i.naturalWidth > 0;
      },
      result.before,
    );
    result.after = await img.getAttribute('src');
    result.afterLoaded = await img.evaluate((i) => i.naturalWidth);
    result.flash = await page.locator('.flash-success').first().innerText();
    result.url = page.url();
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(result));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _node_with_playwright() -> tuple[str, dict[str, str]] | None:
    node, npm = shutil.which("node"), shutil.which("npm")
    if node is None or npm is None:
        return None
    root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, check=False)
    env = {**os.environ, "NODE_PATH": root.stdout.strip()}
    probe = subprocess.run(
        [node, "-e", "require('playwright')"], env=env, capture_output=True, check=False
    )
    return (node, env) if probe.returncode == 0 else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def server(db: Session, tmp_path: Path) -> Iterator[str]:
    app = build_ui_app(db, Clock(), UploadStore(tmp_path / "uploads"))
    lock = threading.Lock()  # one request at a time on the shared test session

    def _session() -> Iterator[Session]:
        with lock:
            yield db

    app.dependency_overrides[get_session] = _session
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


def test_admin_edits_price_and_preview_changes(db: Session, server: str, tmp_path: Path) -> None:
    found = _node_with_playwright()
    if found is None:
        pytest.skip("Node with the playwright package is not available")
    node, env = found
    admin = add_user(db, "admin")
    product = add_product(db, f"Κρέμα χεριών {suffix()}", 1290)
    add_tag_type_row(db)
    script = tmp_path / "ui_browser.js"
    script.write_text(SCRIPT, encoding="utf-8")
    run = subprocess.run(
        [node, str(script), server, admin.email, PASSWORD, str(product.id)],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout.strip().splitlines()[-1])
    assert result["beforeLoaded"] == 296 * 3
    assert result["overflow"] <= 0, "the product page scrolls sideways at phone width"
    assert result["afterLoaded"] == 296 * 3
    assert result["before"] != result["after"]  # a new label hash in the URL
    assert "15,40 €" in result["flash"]
    price = db.scalars(select(Price).where(Price.product_id == product.id)).one()
    db.refresh(price)
    assert price.regular_cents == 1540
