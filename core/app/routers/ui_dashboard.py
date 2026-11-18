"""Dashboard (PLAN.md section 9 and the monitoring of section 1): tag counts, the age
of the last import, gateway status, failed updates, labels that cannot be built,
unseen tags and low batteries. Staff and admins.

What each count means is in ``core.services.admin_queries.dashboard``. The thresholds
below are constants of the UI, not settings; only the stale-import threshold is a
setting (``STALE_IMPORT_DAYS``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from core.app.web import CurrentUser, NowDep, SessionDep, check_csrf, render
from core.config import get_settings
from core.db.models import GatewayRow
from core.services import admin_queries

router = APIRouter(dependencies=[Depends(check_csrf)], include_in_schema=False)

#: A tag not seen for longer than this is "unseen" (section 9: "unseen for 24 h").
UNSEEN_AFTER = timedelta(hours=24)
#: Battery below this percentage is low, for tags that report a percentage.
LOW_BATTERY_PCT = 20
#: Battery below this voltage is low, for tags that report millivolts only. A 3 V
#: lithium coin cell is near its end at about 2.6 V.
LOW_BATTERY_MV = 2600
#: The gateway sends a heartbeat every 30 s; none for this long is a warning.
GATEWAY_STALE_AFTER = timedelta(minutes=5)
#: A label behind a promo start or end is listed only after this long, so the wait for
#: the next gateway claim (about a minute) is not reported as a failure.
FAILING_LABEL_GRACE = timedelta(minutes=5)
#: Rows shown per list; the count above each list is always complete.
LIST_LIMIT = 20


def gateway_stale(gateway: GatewayRow, now: datetime) -> bool:
    last = gateway.last_heartbeat_at
    return last is None or last < now - GATEWAY_STALE_AFTER


def import_stale(last_import_at: datetime | None, now: datetime, stale_days: int) -> bool:
    """No import yet, or the last one is strictly older than ``stale_days`` days."""
    return last_import_at is None or now - last_import_at > timedelta(days=stale_days)


@router.get("/")
def dashboard_page(
    request: Request, session: SessionDep, user: CurrentUser, now: NowDep
) -> Response:
    data = admin_queries.dashboard(
        session,
        now,
        unseen_after=UNSEEN_AFTER,
        low_battery_pct=LOW_BATTERY_PCT,
        low_battery_mv=LOW_BATTERY_MV,
        failing_grace=FAILING_LABEL_GRACE,
    )
    stale_days = get_settings().stale_import_days
    gateways = [(g, gateway_stale(g, now)) for g in data.gateways]
    ctx: dict[str, Any] = {
        "data": data,
        "counts": data.counts,
        "stale_days": stale_days,
        "import_stale": import_stale(data.last_import_at, now, stale_days),
        "import_age": now - data.last_import_at if data.last_import_at else None,
        "gateways": gateways,
        "gateway_warning": not gateways or any(stale for _, stale in gateways),
        "limit": LIST_LIMIT,
    }
    return render(request, "dashboard.html", ctx)
