"""``core.services.users`` and the ``python -m core.tools.users`` CLI."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from core.db.models import AppUser, EventLog
from core.services.users import (
    UserError,
    authenticate,
    create_user,
    reset_password,
    set_active,
)
from core.tools import users as tool
from tests.integration.test_ui_helpers import PASSWORD, T0, add_user, suffix

pytestmark = pytest.mark.db


@pytest.fixture
def no_other_admins(db: Session) -> Iterator[None]:
    """Deactivate committed admins of the development database inside this test's
    transaction (rolled back afterwards), so "last admin" means the test's own."""
    db.execute(update(AppUser).where(AppUser.role == "admin").values(active=False))
    db.flush()
    yield


def test_authenticate(db: Session) -> None:
    user = add_user(db, "staff")
    assert authenticate(db, user.email.upper() + " ", PASSWORD) == user
    assert authenticate(db, user.email, "wrong password") is None
    assert authenticate(db, f"nobody-{suffix()}@example.test", PASSWORD) is None
    set_active(db, user.id, False, actor="test", now=T0)
    assert authenticate(db, user.email, PASSWORD) is None


def test_last_active_admin_cannot_be_deactivated(db: Session, no_other_admins: None) -> None:
    first = add_user(db, "admin")
    with pytest.raises(UserError) as error:
        set_active(db, first.id, False, actor="test", now=T0)
    assert error.value.code == "user.last_admin"
    second = add_user(db, "admin")
    set_active(db, first.id, False, actor="test", now=T0)
    with pytest.raises(UserError):
        set_active(db, second.id, False, actor="test", now=T0)


def test_events_never_hold_passwords(db: Session) -> None:
    user = add_user(db, "staff")
    reset_password(db, user.id, "a fresh password here", actor="test", now=T0)
    kinds = {
        e.kind: e.payload
        for e in db.scalars(select(EventLog).where(EventLog.kind.like("user.%")))
        if e.payload.get("user_id") == user.id
    }
    assert set(kinds) == {"user.added", "user.password_reset"}
    assert all(
        "password" not in str(p).lower().replace("password_reset", "") for p in kinds.values()
    )


def test_role_and_email_checks(db: Session) -> None:
    for email, role, code in [("x@example.test", "owner", "user.role_invalid"),
                              ("no at sign", "staff", "user.email_invalid")]:  # fmt: skip
        with pytest.raises(UserError) as error:
            create_user(db, email, PASSWORD, role, actor="t", now=T0)
        assert error.value.code == code


def test_cli_adds_first_admin(db: Session) -> None:
    email = f"boss-{suffix()}@example.test"
    answers = iter([PASSWORD, PASSWORD])
    code, output = tool.run(
        db, ["add", "--email", email, "--role", "admin"], ask=lambda _: next(answers)
    )
    assert code == 0, output
    assert PASSWORD not in output
    user = db.scalars(select(AppUser).where(AppUser.email == email)).one()
    assert user.role == "admin" and authenticate(db, email, PASSWORD) == user
    code, output = tool.run(db, ["list"])
    assert email in output
    answers = iter([PASSWORD, PASSWORD])
    code, output = tool.run(
        db, ["add", "--email", email, "--role", "admin"], ask=lambda _: next(answers)
    )
    assert code == 1 and "already exists" in output


def test_cli_refuses_mismatched_or_short_password(db: Session) -> None:
    email = f"boss-{suffix()}@example.test"
    answers = iter([PASSWORD, "something else"])
    code, output = tool.run(
        db, ["add", "--email", email, "--role", "staff"], ask=lambda _: next(answers)
    )
    assert code == 1 and "do not match" in output
    answers = iter(["short", "short"])
    code, output = tool.run(
        db, ["add", "--email", email, "--role", "staff"], ask=lambda _: next(answers)
    )
    assert code == 1 and "at least 10" in output


def test_cli_takes_no_password_argument() -> None:
    with pytest.raises(SystemExit):
        tool.build_parser().parse_args(
            ["add", "--email", "a@b.c", "--role", "admin", "--password", "x"]
        )
