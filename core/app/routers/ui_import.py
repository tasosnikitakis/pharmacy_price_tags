"""Import page (section 9, decision 0011): upload, column mapping (profile), preview
diff, apply, and the list of past imports.

The uploaded file is kept in a temporary file between preview and apply only
(``core.app.uploads``); applying re-parses it and rebuilds the diff under the
catalogue's locks (``apply_import``). A suspicious preview (too many price jumps)
needs an explicit second confirmation, which passes ``confirm_price_jumps=True``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response

from core.app.i18n import N_, error_message
from core.app.import_messages import file_error
from core.app.uploads import MAX_BYTES, UploadStore, get_upload_store
from core.app.web import (
    AdminUser,
    CurrentUser,
    NowDep,
    PipelineDep,
    SessionDep,
    actor,
    check_csrf,
    flash,
    redirect,
    render,
)
from core.db.models import DEFAULT_STORE_ID, ImportProfile
from core.domain.catalogue import Column
from core.importers.csv_file import ImportFormatError
from core.importers.profile import ImportSettings
from core.services import admin_queries
from core.services.catalogue import (
    ImportConfirmationRequired,
    ImportPreview,
    apply_import,
    get_profile,
    list_profiles,
    preview_import,
    save_profile,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/import", dependencies=[Depends(check_csrf)], include_in_schema=False)

UploadDep = Annotated[UploadStore, Depends(get_upload_store)]
_PENDING = "import"
#: The delimiter choices of the mapping form; "tab" stands for the tab character.
DELIMITER_CHOICES = {"": None, ";": ";", ",": ",", "tab": "\t"}
ENCODING_CHOICES = {"": None, "utf-8": "utf-8", "cp1253": "cp1253"}
COLUMNS = [c.value for c in Column]


class PageError(Exception):
    """The upload or mapping cannot be used; ``message`` is the UI text."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _page(
    request: Request,
    session: SessionDep,
    *,
    error: str | None = None,
    values: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    context = {
        "profiles": list_profiles(session),
        "runs": admin_queries.import_runs(session),
        "columns": COLUMNS,
        "error": error,
        "values": values or {},
        "pending": request.session.get(_PENDING),
    }
    return render(request, "import/index.html", context, status_code=status_code)


@router.get("")
def import_page(request: Request, session: SessionDep, user: CurrentUser) -> Response:
    return _page(request, session)


def _profile_from(pending: dict[str, Any], session: SessionDep) -> ImportProfile:
    """The stored profile named in ``pending``, or a transient one (not added to the
    session) for a mapping that was not saved."""
    name = pending.get("profile")
    if name:
        profile = get_profile(session, name)
        if profile is None:
            raise PageError(error_message("import.profile_missing"))
        return profile
    settings = ImportSettings.create(
        pending["mapping"], pending.get("encoding"), pending.get("delimiter")
    )
    return ImportProfile(
        store_id=DEFAULT_STORE_ID,
        name="unsaved mapping",
        mapping=settings.mapping_json(),
        encoding=settings.encoding,
        delimiter=settings.delimiter,
    )


def _preview_page(
    request: Request,
    preview: ImportPreview,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    diff = preview.diff
    context = {
        "preview": preview,
        "diff": diff,
        "counts": diff.counts(),
        "error": error,
        "delimiter_name": {"\t": "tab"}.get(preview.delimiter, preview.delimiter),
    }
    return render(request, "import/preview.html", context, status_code=status_code)


@router.post("/preview")
def preview(
    request: Request,
    session: SessionDep,
    user: AdminUser,
    store: UploadDep,
    file: Annotated[UploadFile | None, File()] = None,
    profile: Annotated[str, Form(max_length=100)] = "",
    save_as: Annotated[str, Form(max_length=200)] = "",
    encoding: Annotated[str, Form(max_length=10)] = "",
    delimiter: Annotated[str, Form(max_length=5)] = "",
    map_ean: Annotated[str, Form(max_length=200)] = "",
    map_name: Annotated[str, Form(max_length=200)] = "",
    map_price: Annotated[str, Form(max_length=200)] = "",
    map_brand: Annotated[str, Form(max_length=200)] = "",
    map_pack_qty: Annotated[str, Form(max_length=200)] = "",
    map_pack_unit: Annotated[str, Form(max_length=200)] = "",
) -> Response:
    mapping = {
        "ean": map_ean, "name": map_name, "price": map_price, "brand": map_brand,
        "pack_qty": map_pack_qty, "pack_unit": map_pack_unit,
    }  # fmt: skip
    values = {"profile": profile, "save_as": save_as, "encoding": encoding,
              "delimiter": delimiter, **{f"map_{k}": v for k, v in mapping.items()}}  # fmt: skip
    try:
        if file is None or not file.filename:
            raise PageError(error_message("import.no_file"))
        data = file.file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise PageError(error_message("import.file_too_large"))
        filename = file.filename[-255:]
        pending: dict[str, Any] = {"filename": filename}
        if profile:
            pending["profile"] = profile
        else:
            if encoding not in ENCODING_CHOICES or delimiter not in DELIMITER_CHOICES:
                raise PageError(error_message("import.profile_invalid"))
            try:
                settings = ImportSettings.create(
                    {k: v for k, v in mapping.items() if v.strip()},
                    ENCODING_CHOICES[encoding],
                    DELIMITER_CHOICES[delimiter],
                )
            except ValueError as exc:
                log.info("import mapping refused: %s", exc)
                raise PageError(error_message("import.profile_invalid")) from None
            if save_as.strip():
                try:
                    saved = save_profile(session, save_as, settings)
                except ValueError as exc:
                    log.info("import profile not saved: %s", exc)
                    raise PageError(error_message("import.profile_name_invalid")) from None
                session.commit()
                flash(request, "success", N_("import.profile.saved"), name=saved.name)
                pending["profile"] = saved.name
            else:
                pending["mapping"] = settings.mapping_json()
                pending["encoding"] = settings.encoding
                pending["delimiter"] = settings.delimiter
        chosen = _profile_from(pending, session)
        try:
            result = preview_import(session, data, filename, chosen)
        except ImportFormatError as exc:
            log.info("import file refused: %s", exc)
            raise PageError(file_error(str(exc))) from None
    except PageError as exc:
        return _page(request, session, error=exc.message, values=values, status_code=422)
    _discard_pending(request, store)
    pending["token"] = store.save(data)
    request.session[_PENDING] = pending
    return _preview_page(request, result)


def _discard_pending(request: Request, store: UploadStore) -> None:
    pending = request.session.pop(_PENDING, None)
    if isinstance(pending, dict) and isinstance(pending.get("token"), str):
        store.delete(pending["token"])


@router.post("/apply")
def apply(
    request: Request,
    session: SessionDep,
    user: AdminUser,
    store: UploadDep,
    now: NowDep,
    pipeline: PipelineDep,
    confirm_price_jumps: Annotated[bool, Form()] = False,
) -> Response:
    pending = request.session.get(_PENDING)
    data = store.load(pending["token"]) if isinstance(pending, dict) else None
    if not isinstance(pending, dict) or data is None:
        request.session.pop(_PENDING, None)
        return _page(request, session, error=error_message("import.expired"), status_code=409)
    try:
        profile = _profile_from(pending, session)
        result = apply_import(
            session, data, pending["filename"], profile,
            actor=actor(user), created_by=user.id, now=now,
            confirm_price_jumps=confirm_price_jumps, pipeline=pipeline,
        )  # fmt: skip
    except ImportConfirmationRequired:
        again = preview_import(session, data, pending["filename"], profile)
        message = error_message("import.confirmation_required")
        return _preview_page(request, again, error=message, status_code=409)
    except PageError as exc:
        _discard_pending(request, store)
        return _page(request, session, error=exc.message, status_code=409)
    except ImportFormatError as exc:
        _discard_pending(request, store)
        return _page(request, session, error=file_error(str(exc)), status_code=422)
    session.commit()
    _discard_pending(request, store)
    counts = result.run.counts
    flash(
        request, "success", N_("import.applied"),
        new=counts.get("new", 0), changed=counts.get("changed", 0),
        unchanged=counts.get("unchanged", 0), rejected=counts.get("rejected", 0),
    )  # fmt: skip
    refused = sum(1 for e in result.price_events if e.kind == "promo.refused")
    if refused:
        flash(request, "warning", N_("import.applied_promos_refused"), count=refused)
    return redirect("/import")


@router.post("/cancel")
def cancel(request: Request, user: AdminUser, store: UploadDep) -> Response:
    _discard_pending(request, store)
    flash(request, "info", N_("import.cancelled"))
    return redirect("/import")
