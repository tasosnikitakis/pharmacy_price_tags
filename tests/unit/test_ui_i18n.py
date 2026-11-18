"""Decision 0015: every UI string is in ``core/app/i18n/el.json``, keyed by English ids.

- Every key used in templates and ``core/app/`` code exists, and ``el.json`` has no
  unused keys. Keys built at run time are listed in ``all_runtime_keys()``.
- Every error code the UI raises has a message, and templates contain no text of
  their own.
"""

from __future__ import annotations

import json
import re
import string
from html.parser import HTMLParser
from pathlib import Path

import pytest

from core.app import i18n
from core.app.i18n import ERROR_CODES, all_runtime_keys, catalogue, error_message, t
from core.services.users import USER_ERROR_CODES

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "core" / "app"
TEMPLATES = APP / "templates"
EL_JSON = APP / "i18n" / "el.json"

# t("key") or t("key", ...) and N_("key"): a complete literal key, not a prefix.
_KEY_CALL = re.compile(r"""\b(?:t|N_)\(\s*(["'])([A-Za-z0-9_.]+)\1\s*[,)]""")
_CODE_CALLS = (
    re.compile(r"""\berror_message\(\s*(["'])([a-z0-9_.]+)\1"""),
    re.compile(r"""\bflash_error\(\s*\w+,\s*(["'])([a-z0-9_.]+)\1"""),
    re.compile(r"""\bFormError\(\s*(["'])([a-z0-9_.]+)\1"""),
    re.compile(r"""\bUserError\(\s*(["'])([a-z0-9_.]+)\1"""),
    re.compile(r"""\b_error_page\(\s*\w+,\s*(["'])([a-z0-9_.]+)\1"""),
    re.compile(r"""["']error["']:\s*(["'])([a-z0-9_.]+)\1"""),
)


def _sources() -> list[Path]:
    return [*TEMPLATES.rglob("*.html"), *APP.rglob("*.py")]


def used_keys() -> set[str]:
    keys: set[str] = set()
    for path in _sources():
        keys.update(m.group(2) for m in _KEY_CALL.finditer(path.read_text(encoding="utf-8")))
    return keys


def test_el_json_is_flat_strings_without_duplicates() -> None:
    pairs = json.loads(EL_JSON.read_text(encoding="utf-8"), object_pairs_hook=lambda p: p)
    names = [k for k, _ in pairs]
    assert len(names) == len(set(names)), "duplicate keys in el.json"
    assert all(isinstance(v, str) and v.strip() for _, v in pairs)


def test_language_is_greek() -> None:
    assert i18n.LANGUAGE == "el"


def test_scan_finds_keys() -> None:
    keys = used_keys()
    assert {"login.title", "products.price.saved", "preview.no_price"} <= keys


def test_every_used_key_exists() -> None:
    missing = sorted((used_keys() | set(all_runtime_keys())) - set(catalogue()))
    assert missing == []


def test_no_unused_keys() -> None:
    unused = sorted(set(catalogue()) - used_keys() - set(all_runtime_keys()))
    assert unused == []


def test_every_error_code_in_code_is_listed() -> None:
    codes: set[str] = set()
    for path in [*_sources(), ROOT / "core" / "services" / "users.py"]:
        text = path.read_text(encoding="utf-8")
        for pattern in _CODE_CALLS:
            codes.update(m.group(2) for m in pattern.finditer(text))
    assert codes, "the scan found no error codes"
    assert sorted(codes - set(ERROR_CODES)) == []
    assert set(USER_ERROR_CODES) <= set(ERROR_CODES)
    for code in ERROR_CODES:
        assert error_message(code) == catalogue()[f"error.{code}"]


def test_placeholders_are_plain_names() -> None:
    for key, text in catalogue().items():
        for _, name, spec, conversion in string.Formatter().parse(text):
            if name is not None:
                assert name.isidentifier() and not spec and not conversion, key


def test_t_fills_params_and_survives_mistakes(caplog: pytest.LogCaptureFixture) -> None:
    template = catalogue()["products.price.saved"]
    assert t("products.price.saved", price="12,90 €") == template.replace("{price}", "12,90 €")
    assert t("no.such.key") == "no.such.key"
    assert t("products.price.saved") == catalogue()["products.price.saved"]  # no params
    assert t("products.price.saved", other="x") == catalogue()["products.price.saved"]
    assert "missing UI string" in caplog.text


def test_unknown_error_code_falls_back() -> None:
    assert error_message("no.such.code") == error_message("invalid_input")


class _TextFinder(HTMLParser):
    """Text nodes and human-readable attributes of a template whose Jinja has been
    blanked out."""

    ATTRIBUTES = frozenset({"placeholder", "title", "aria-label", "alt", "hx-confirm"})

    def __init__(self) -> None:
        super().__init__()
        self.found: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        for name, value in attrs:
            if name in self.ATTRIBUTES and value and _has_letters(value):
                self.found.append(f"{tag}[{name}]={value!r}")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and _has_letters(data):
            self.found.append(data.strip())


def _has_letters(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)


@pytest.mark.parametrize(
    "template", sorted(TEMPLATES.rglob("*.html")), ids=lambda p: str(p.relative_to(TEMPLATES))
)
def test_templates_contain_no_text_of_their_own(template: Path) -> None:
    source = _JINJA.sub(" ", template.read_text(encoding="utf-8"))
    finder = _TextFinder()
    finder.feed(source)
    assert finder.found == []
