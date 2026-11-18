"""Price-jump warning in the import preview (Tasos). No database: runs on Windows."""

# ruff: noqa: RUF001  (Greek test data is meant to look like Latin letters)
from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.config import Settings, get_settings
from core.domain.catalogue import (
    CatalogueEntry,
    ImportRow,
    JumpThresholds,
    PriceJump,
    build_diff,
    is_suspicious,
    price_jumps,
)
from core.services.catalogue import jump_thresholds

EAN = "5901234123457"


def _jumps(old: int, new: int, warn: int = 50) -> list[PriceJump]:
    return price_jumps([(EAN, "Κρέμα", old, new)], warn)


# --- One price change -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("new", "jumps", "percent"),
    [
        (1500, False, None),  # exactly +50%: strict, no warning
        (1501, True, 50),  # just over; +50.1% rounds to 50
        (500, False, None),  # exactly -50%
        (499, True, -50),  # just over, downwards
        (1000 + 1, False, None),
        (3000, True, 200),
        (1, True, -100),  # -99.9% rounds half away from zero
        (1505, True, 51),  # +50.5% rounds up
    ],
)
def test_jump_boundaries_at_50_percent(new: int, jumps: bool, percent: int | None) -> None:
    result = _jumps(1000, new)
    assert bool(result) is jumps
    if jumps:
        assert result == [PriceJump(EAN, "Κρέμα", 1000, new, percent)]


def test_jump_boundary_with_custom_percent() -> None:
    assert _jumps(1000, 1100, warn=10) == []
    assert _jumps(1000, 1101, warn=10)[0].percent_change == 10
    assert _jumps(1000, 899, warn=10)[0].percent_change == -10
    assert _jumps(1000, 3000, warn=200) == []
    assert _jumps(1000, 3001, warn=200)[0].percent_change == 200


def test_jump_from_a_zero_price() -> None:
    assert _jumps(0, 100) == [PriceJump(EAN, "Κρέμα", 0, 100, None)]


def test_jumps_sorted_by_size_of_change() -> None:
    changes = [
        ("2000000000015", "Α", 1000, 1600),  # +60%
        ("2000000000022", "Β", 1000, 200),  # -80%
        ("2000000000039", "Γ", 1000, 1100),  # +10%: no warning
        ("2000000000046", "Δ", 0, 50),  # from zero: first
        ("2000000000053", "Ε", 300, 1200),  # +300%
        ("2000000000060", "Ζ", 500, 800),  # +60%, ties with 15: by EAN
    ]
    order = [(j.ean, j.percent_change) for j in price_jumps(changes, 50)]
    assert order == [
        ("2000000000046", None),
        ("2000000000053", 300),
        ("2000000000022", -80),
        ("2000000000015", 60),
        ("2000000000060", 60),
    ]


# --- Banner ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("jumps", "changes", "banner"),
    [
        (1, 5, False),  # exactly 20%: strict, no banner
        (2, 10, False),
        (2, 9, True),  # 22%
        (1, 4, True),  # 25%
        (21, 100, True),  # just over
        (20, 100, False),
        (0, 0, False),  # no price changes: never a banner
        (0, 10, False),
    ],
)
def test_banner_boundaries_at_20_percent(jumps: int, changes: int, banner: bool) -> None:
    assert is_suspicious(jumps, changes, 20) is banner


def test_banner_with_custom_percent() -> None:
    assert is_suspicious(5, 10, 50) is False
    assert is_suspicious(6, 10, 50) is True
    assert is_suspicious(10, 10, 100) is False  # 100% can never be exceeded
    assert is_suspicious(1, 100, 1) is False
    assert is_suspicious(2, 100, 1) is True


# --- In the diff ----------------------------------------------------------------------


def _entry(pid: int, ean: str, cents: int | None, name: str = "Π") -> CatalogueEntry:
    return CatalogueEntry(pid, ean, name, None, None, None, cents)


def test_diff_counts_only_price_changes() -> None:
    catalogue = [
        _entry(1, "2000000000015", 1000),  # jumps
        _entry(2, "2000000000022", 1000),  # unchanged
        _entry(3, "2000000000039", 1000, name="Παλιό"),  # name change only
        _entry(4, "2000000000046", None),  # no price before: not a jump
        _entry(5, "2000000000053", 1000),  # small price change
    ]
    rows = [
        ImportRow(2, "2000000000015", "Π", 2000),
        ImportRow(3, "2000000000022", "Π", 1000),
        ImportRow(4, "2000000000039", "Νέο", 1000),
        ImportRow(5, "2000000000046", "Π", 9000),
        ImportRow(6, "2000000000053", "Π", 1100),
        ImportRow(7, "2000000000060", "Νέο προϊόν", 99_999),  # new product
    ]
    diff = build_diff(rows, [], catalogue)
    assert diff.price_jumps == [PriceJump("2000000000015", "Π", 1000, 2000, 100)]
    # Two price changes (15 and 53), one jump: 50% > 20%, so a banner.
    assert diff.suspicious is True
    assert diff.jump_thresholds == JumpThresholds(50, 20)
    assert diff.counts()["price_jumps"] == 1


def test_diff_banner_exactly_at_threshold_and_just_over() -> None:
    catalogue = [_entry(i, f"200000000{i:03d}0", 1000) for i in range(1, 11)]
    small = [ImportRow(i + 1, e.ean, "Π", 1100) for i, e in enumerate(catalogue)]
    one_in_five = [r if r.line > 1 else ImportRow(r.line, r.ean, "Π", 5000) for r in small[:5]]
    diff = build_diff(one_in_five, [], catalogue)
    assert (len(diff.price_jumps), diff.suspicious) == (1, False)  # 1 of 5 = 20%
    one_in_four = one_in_five[:4]
    diff = build_diff(one_in_four, [], catalogue)
    assert (len(diff.price_jumps), diff.suspicious) == (1, True)  # 1 of 4 = 25%


def test_diff_with_custom_thresholds() -> None:
    catalogue = [_entry(1, "2000000000015", 1000), _entry(2, "2000000000022", 1000)]
    rows = [ImportRow(2, "2000000000015", "Π", 1200), ImportRow(3, "2000000000022", "Π", 1050)]
    assert build_diff(rows, [], catalogue).price_jumps == []
    strict = build_diff(rows, [], catalogue, JumpThresholds(warn_percent=10, banner_percent=50))
    assert [j.ean for j in strict.price_jumps] == ["2000000000015"]
    assert strict.suspicious is False  # 1 of 2 = 50%, not more
    assert strict.jump_thresholds == JumpThresholds(10, 50)


def test_diff_with_no_price_changes_has_no_banner() -> None:
    diff = build_diff([ImportRow(2, "2000000000015", "Π", 1000)], [], [])
    assert (diff.price_jumps, diff.suspicious) == ([], False)


@pytest.mark.parametrize(
    ("kw", "error"),
    [
        ({"warn_percent": 0}, ValueError),
        ({"warn_percent": -5}, ValueError),
        ({"banner_percent": 0}, ValueError),
        ({"banner_percent": 101}, ValueError),
        ({"warn_percent": 50.0}, TypeError),
        ({"banner_percent": True}, TypeError),
    ],
)
def test_thresholds_are_validated(kw: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        JumpThresholds(**kw)  # type: ignore[arg-type]


# --- Settings -------------------------------------------------------------------------

_SECRETS: dict[str, object] = {
    "database_url": "postgresql+psycopg://esl@localhost:5432/esl",
    "session_secret": "s" * 40,
    "gateway_token": "g" * 40,
}


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRICE_JUMP_WARN_PERCENT", raising=False)
    monkeypatch.delenv("PRICE_JUMP_BANNER_PERCENT", raising=False)
    settings = Settings(**_SECRETS)  # type: ignore[arg-type]
    assert (settings.price_jump_warn_percent, settings.price_jump_banner_percent) == (50, 20)
    assert jump_thresholds(settings) == JumpThresholds(50, 20)


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRICE_JUMP_WARN_PERCENT", "30")
    monkeypatch.setenv("PRICE_JUMP_BANNER_PERCENT", "40")
    assert jump_thresholds(Settings(**_SECRETS)) == JumpThresholds(30, 40)  # type: ignore[arg-type]
    # The default path reads get_settings(); clear its cache around the check.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://esl@localhost:5432/esl")
    get_settings.cache_clear()
    try:
        assert jump_thresholds() == JumpThresholds(30, 40)
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price_jump_warn_percent", 0),
        ("price_jump_warn_percent", -1),
        ("price_jump_warn_percent", 50.0),
        ("price_jump_warn_percent", "50.5"),
        ("price_jump_warn_percent", True),
        ("price_jump_banner_percent", 0),
        ("price_jump_banner_percent", 101),
        ("price_jump_banner_percent", 20.0),
    ],
)
def test_settings_refuse_bad_percents(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        Settings(**_SECRETS, **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["20.5", "50.0", "5e1", "٥٠", "", " ", "50%"])
def test_settings_refuse_non_integer_from_environment(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    monkeypatch.setenv("PRICE_JUMP_WARN_PERCENT", text)
    with pytest.raises(ValidationError, match="price_jump_warn_percent"):
        Settings(**_SECRETS)  # type: ignore[arg-type]


def test_settings_accept_plain_integer_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRICE_JUMP_WARN_PERCENT", " 75 ")
    monkeypatch.setenv("PRICE_JUMP_BANNER_PERCENT", "+30")
    assert jump_thresholds(Settings(**_SECRETS)) == JumpThresholds(75, 30)  # type: ignore[arg-type]
