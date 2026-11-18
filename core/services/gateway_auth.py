"""Gateway bearer-token check. The core stores only an scrypt hash of ``GATEWAY_TOKEN``.

Stored format: ``scrypt$<n>$<r>$<p>$<salt hex>$<hash hex>``. Verification compares in
constant time. The token is never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from threading import Lock

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.db.models import DEFAULT_STORE_ID, GatewayRow

#: Name of the single gateway row in v1.
DEFAULT_GATEWAY_NAME = "gateway"

_N, _R, _P = 2**14, 8, 1
_DKLEN = 32

# scrypt is deliberately slow and the gateway calls several times a minute, so a
# successful check is remembered for this process: (stored hash, sha256 of token).
# A changed stored hash (token rotated) misses the cache.
_verified: set[tuple[str, bytes]] = set()
_verified_lock = Lock()
_MAX_CACHED = 16


def hash_token(token: str) -> str:
    """A salted scrypt hash of ``token`` in the stored format."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(token.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_token(token: str, stored: str) -> bool:
    """Whether ``token`` matches ``stored``. Constant-time; never raises on bad input."""
    key = (stored, hashlib.sha256(token.encode()).digest())
    with _verified_lock:
        if key in _verified:
            return True
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(
            token.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except ValueError:
        return False
    ok = hmac.compare_digest(actual, expected)
    if ok:
        with _verified_lock:
            if len(_verified) >= _MAX_CACHED:
                _verified.clear()
            _verified.add(key)
    return ok


def ensure_gateway(session: Session, token: str) -> GatewayRow:
    """Create or update the single v1 gateway row so that ``token`` is accepted.

    Run on startup with ``GATEWAY_TOKEN``. The hash is only rewritten when the token
    changed, so restarts do not churn the row.
    """
    row = session.scalars(
        select(GatewayRow)
        .where(GatewayRow.store_id == DEFAULT_STORE_ID)
        .order_by(GatewayRow.id)
        .limit(1)
        .with_for_update()
    ).first()
    if row is None:
        row = GatewayRow(
            store_id=DEFAULT_STORE_ID, name=DEFAULT_GATEWAY_NAME, token_hash=hash_token(token)
        )
        session.add(row)
    elif not verify_token(token, row.token_hash):
        row.token_hash = hash_token(token)
    session.flush()
    return row


def authenticate(session: Session, token: str) -> int | None:
    """The id of the gateway that ``token`` belongs to, or ``None``."""
    rows = session.execute(
        select(GatewayRow.id, GatewayRow.token_hash).where(GatewayRow.store_id == DEFAULT_STORE_ID)
    ).all()
    for gateway_id, stored in rows:
        if verify_token(token, stored):
            return int(gateway_id)
    return None
