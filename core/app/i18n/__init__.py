"""UI strings (decision 0015).

Every user-facing string of the admin UI is in one file per language,
``<language>.json``, keyed by English identifiers such as ``import.preview.title``.
Templates and ``core/app/`` code call ``t(key, **params)`` and never contain
user-facing text themselves.

Scope: the admin UI only. Text drawn on labels stays in ``core/render/`` and
``core/domain/pricing.py``.

v1 is Greek only, so the language is fixed here (``LANGUAGE``). Adding English means
adding ``en.json`` and a configuration setting that picks the language; nothing else.

Keys that code builds at run time (``error.<code>``, ``role.<role>`` and the like) are
listed in ``RUNTIME_KEYS``, so the coverage test checks them as well.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from core.db.models import JOB_STATES, ROLES
from core.domain.catalogue import Column
from core.domain.pricing import PromoRefusal
from core.domain.types import PackUnit, PriceKind, PriceSource
from core.render.label import TemplateKey
from shared.gateway_api import PushErrorCode

log = logging.getLogger(__name__)

#: The UI language for v1. Becomes a configuration setting when en.json is added.
LANGUAGE = "el"

_DIR = Path(__file__).parent


@lru_cache
def catalogue(language: str = LANGUAGE) -> dict[str, str]:
    """All strings of ``language``, read from ``<language>.json``."""
    data = json.loads((_DIR / f"{language}.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise ValueError(f"{language}.json must map string keys to string values")
    return data


def has(key: str) -> bool:
    return key in catalogue()


def t(key: str, /, **params: object) -> str:
    """The UI string for ``key``, with ``{name}`` placeholders filled from ``params``.

    A missing key is logged and shown as the key itself rather than failing the page;
    the key coverage test keeps that from happening.
    """
    text = catalogue().get(key)
    if text is None:
        log.error("missing UI string %r", key)
        return key
    if not params:
        return text
    try:
        return text.format_map(params)
    except (KeyError, IndexError, ValueError):
        log.error("UI string %r does not fit its parameters", key)
        return text


def N_(key: str, /) -> str:
    """Marks a literal as a UI string key that is looked up later (like gettext's
    ``N_``), so the key coverage test finds it. Returns ``key`` unchanged."""
    return key


#: Error codes the UI can show; the message of each is ``error.<code>``. Code and logs
#: use the code, the page shows the message (decision 0015).
ERROR_CODES: tuple[str, ...] = (
    "auth.invalid_credentials",
    "auth.forbidden",
    "auth.login_required",
    "csrf.invalid",
    "not_found",
    "invalid_input",
    "price.invalid",
    "price.zero",
    "price.too_large",
    "product.name_empty",
    "product.name_too_long",
    "product.brand_too_long",
    "product.pack_invalid",
    "product.unit_price_needs_pack",
    "promo.no_price",
    "promo.refused",
    "promo.rx",
    "promo.price_invalid",
    "promo.dates_invalid",
    "promo.date_out_of_range",
    "request.too_large",
    "promo.prior_invalid",
    "import.no_file",
    "import.file_too_large",
    "import.expired",
    "import.profile_missing",
    "import.profile_invalid",
    "import.profile_name_invalid",
    "import.confirmation_required",
    "import.unreadable",
    "tag.not_found",
    "tag.not_bound",
    "job.not_retryable",
    "user.email_invalid",
    "user.exists",
    "user.not_found",
    "user.password_too_short",
    "user.password_too_long",
    "user.password_mismatch",
    "user.role_invalid",
    "user.last_admin",
    "user.self_deactivate",
)


def error_message(code: str, /, **params: object) -> str:
    """The UI message for an error code (``error.<code>``)."""
    key = f"error.{code}"
    if not has(key):
        log.error("no UI message for error code %r", code)
        key = "error.invalid_input"
    return t(key, **params)


#: Tag statuses on the Tags page (``core.services.admin_queries.TagStatus``).
TAG_STATUSES = ("up_to_date", "pending", "failed", "dead")
#: Product fields an import can change (``core.domain.catalogue.FieldChange.field``).
IMPORT_FIELDS = ("name", "brand", "pack_qty_milli", "pack_unit")

#: Every key that code builds at run time.
RUNTIME_KEYS: tuple[str, ...] = (
    *(f"error.{code}" for code in ERROR_CODES),
    *(f"promo.refusal.{r.value}" for r in PromoRefusal),
    *(f"role.{role}" for role in ROLES),
    *(f"job.state.{state}" for state in JOB_STATES),
    *(f"tags.status.{status}" for status in TAG_STATUSES),
    *(f"template.{key.value}" for key in TemplateKey),
    *(f"unit.{unit.value}" for unit in PackUnit),
    *(f"history.kind.{kind.value}" for kind in PriceKind),
    *(f"history.source.{source.value}" for source in PriceSource),
    *(f"import.column.{column.value}" for column in Column),
    *(f"import.field.{name}" for name in IMPORT_FIELDS),
    *(f"push_error.{code.value}" for code in PushErrorCode),
)


def all_runtime_keys() -> tuple[str, ...]:
    """``RUNTIME_KEYS`` plus the importer reasons of ``core.app.import_messages``."""
    from core.app.import_messages import MESSAGE_KEYS

    return (*RUNTIME_KEYS, *MESSAGE_KEYS)
