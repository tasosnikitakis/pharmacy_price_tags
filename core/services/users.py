"""Admin UI users: password hashing, login check, and the admin's user management.

Passwords are hashed with ``hashlib.scrypt`` and a random per-user salt (standard
library only) and stored as ``scrypt$<n>$<r>$<p>$<salt hex>$<hash hex>``, the same
format as the gateway token (decision 0013). Checks compare in constant time. A
password is never logged, stored in clear or put in an event payload.

Errors carry a machine code (``UserError.code``); the UI looks up its message.
Functions here never commit; they run in the caller's transaction.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.db.models import DEFAULT_STORE_ID, ROLES, AppUser, EventLog

_N, _R, _P = 2**14, 8, 1
_DKLEN = 32
_SALT_BYTES = 16

PASSWORD_MIN_LEN = 10
#: Bounds the work an attacker can force on scrypt with a huge password.
PASSWORD_MAX_LEN = 1024
EMAIL_MAX_LEN = 320
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")

#: Error codes raised as ``UserError.code``.
USER_ERROR_CODES = (
    "user.email_invalid",
    "user.exists",
    "user.not_found",
    "user.password_too_short",
    "user.password_too_long",
    "user.role_invalid",
    "user.last_admin",
)


class UserError(ValueError):
    """A user cannot be created or changed as asked. ``code`` names the reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def hash_password(password: str) -> str:
    """A salted scrypt hash of ``password`` in the stored format."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Whether ``password`` matches ``stored``. Constant-time; never raises on bad input."""
    if len(password) > PASSWORD_MAX_LEN:
        return False
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


#: Checked against when the email is unknown, so a login takes as long either way.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def normalise_email(email: str) -> str:
    return email.strip().lower()


def check_password(password: str) -> None:
    if len(password) < PASSWORD_MIN_LEN:
        raise UserError("user.password_too_short")
    if len(password) > PASSWORD_MAX_LEN:
        raise UserError("user.password_too_long")


def _check_email(email: str) -> str:
    email = normalise_email(email)
    if len(email) > EMAIL_MAX_LEN or not _EMAIL.match(email):
        raise UserError("user.email_invalid")
    return email


def _log(session: Session, kind: str, user: AppUser, actor: str, now: datetime) -> None:
    payload = {"user_id": user.id, "email": user.email, "role": user.role, "active": user.active}
    session.add(EventLog(store_id=user.store_id, at=now, actor=actor, kind=kind, payload=payload))


def authenticate(
    session: Session, email: str, password: str, store_id: int = DEFAULT_STORE_ID
) -> AppUser | None:
    """The active user with this email and password, else ``None``. An inactive user,
    an unknown email and a wrong password all give ``None`` and take about as long."""
    user = session.scalar(
        select(AppUser).where(AppUser.store_id == store_id, AppUser.email == normalise_email(email))
    )
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash) or not user.active:
        return None
    return user


def get_user(session: Session, user_id: int, store_id: int = DEFAULT_STORE_ID) -> AppUser | None:
    user = session.get(AppUser, user_id)
    return user if user is not None and user.store_id == store_id else None


def list_users(session: Session, store_id: int = DEFAULT_STORE_ID) -> list[AppUser]:
    return list(
        session.scalars(select(AppUser).where(AppUser.store_id == store_id).order_by(AppUser.email))
    )


def create_user(
    session: Session,
    email: str,
    password: str,
    role: str,
    *,
    actor: str,
    now: datetime,
    store_id: int = DEFAULT_STORE_ID,
) -> AppUser:
    """Add an active user. Logs ``user.added`` (never the password)."""
    email = _check_email(email)
    if role not in ROLES:
        raise UserError("user.role_invalid")
    check_password(password)
    exists = session.scalar(
        select(AppUser.id).where(AppUser.store_id == store_id, AppUser.email == email)
    )
    if exists is not None:
        raise UserError("user.exists")
    user = AppUser(
        store_id=store_id,
        email=email,
        password_hash=hash_password(password),
        role=role,
        active=True,
        created_at=now,
    )
    try:
        with session.begin_nested():
            session.add(user)
            session.flush()
    except IntegrityError:
        raise UserError("user.exists") from None
    _log(session, "user.added", user, actor, now)
    session.flush()
    return user


def _lock_user(session: Session, user_id: int, store_id: int) -> AppUser:
    user = session.scalar(
        select(AppUser)
        .where(AppUser.id == user_id, AppUser.store_id == store_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise UserError("user.not_found")
    return user


def _active_admins(session: Session, store_id: int) -> int:
    # Lock the admin rows so two deactivations cannot both pass the check.
    ids = session.scalars(
        select(AppUser.id)
        .where(AppUser.store_id == store_id, AppUser.role == "admin", AppUser.active)
        .order_by(AppUser.id)
        .with_for_update()
    ).all()
    return len(ids)


def set_active(
    session: Session,
    user_id: int,
    active: bool,
    *,
    actor: str,
    now: datetime,
    store_id: int = DEFAULT_STORE_ID,
) -> AppUser:
    """Deactivate or reactivate a user; logs ``user.deactivated`` or ``user.activated``.
    The last active admin cannot be deactivated, so nobody is locked out."""
    admins = _active_admins(session, store_id) if not active else None
    user = _lock_user(session, user_id, store_id)
    if admins is not None and admins <= 1 and user.role == "admin" and user.active:
        raise UserError("user.last_admin")
    if user.active == active:
        return user
    user.active = active
    _log(session, "user.activated" if active else "user.deactivated", user, actor, now)
    session.flush()
    return user


def reset_password(
    session: Session,
    user_id: int,
    password: str,
    *,
    actor: str,
    now: datetime,
    store_id: int = DEFAULT_STORE_ID,
) -> AppUser:
    """Set a new password; logs ``user.password_reset``. The UI ties each login to the
    password hash, so the user's other sessions end."""
    check_password(password)
    user = _lock_user(session, user_id, store_id)
    user.password_hash = hash_password(password)
    _log(session, "user.password_reset", user, actor, now)
    session.flush()
    return user
