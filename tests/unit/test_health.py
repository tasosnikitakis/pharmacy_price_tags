from __future__ import annotations

from collections.abc import Iterator

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from core.app.main import create_app
from core.db.session import get_session


def test_health_ok_with_database(db: Session) -> None:
    app = create_app()

    def _session() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_session] = _session
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["database"] == "ok"


class _BrokenSession:
    def execute(self, *_: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("down"))


def test_health_reports_unavailable_database() -> None:
    app = create_app()

    def _session() -> Iterator[_BrokenSession]:
        yield _BrokenSession()

    app.dependency_overrides[get_session] = _session
    response = TestClient(app).get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "unavailable", "version": "0.1.0"}
