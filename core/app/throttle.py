"""Login throttling: a growing delay after repeated failed logins (decision 0019).

Two independent key spaces, each a ``KeyThrottle``:

- **account**: the normalised email as typed, whether or not the account exists, so the
  throttle does not reveal which emails exist;
- **ip**: the client address. Its free allowance is higher, because the shop's phones
  probably share one address behind the router's NAT.

For each key, the first ``free`` failures cost nothing. Each further failure sets the
next allowed attempt to ``now + min(base * 2**(failures - free), max)``. Failures are
forgotten ``window`` seconds after the last one (and never before a running delay has
ended). There is no permanent lockout: the delay is capped at ``max``.

A throttled request is refused without checking the password (the router answers 429);
nobody sleeps. The state lives in memory, in one ``LoginThrottle`` per app
(``app.state.login_throttle``): the core runs as a single uvicorn process, a restart
clears the counters, and more than one worker would need shared state.

Clock: every method takes ``now`` (the request's ``NowDep`` reading), so tests control
time. Thread safety: one lock per ``LoginThrottle``; FastAPI runs sync routes in a
thread pool. Size: each key space keeps at most ``max_keys`` keys; when full, expired
keys go first, then the key whose last failure is oldest.

Nothing here touches the database or logs; the router turns ``ThrottleEvent`` values
into log lines and ``event_log`` rows.
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import Depends, Request

if TYPE_CHECKING:
    from core.config import Settings

KeyType = Literal["account", "ip"]
EventKind = Literal["throttled", "refused"]

#: Keys kept per key space. An entry is a few hundred bytes, so both spaces together
#: stay well under 10 MB even when an attacker sprays random emails.
DEFAULT_MAX_KEYS = 10_000

#: ``2**n`` beyond this is far past any sensible cap; avoids building huge integers.
_MAX_EXPONENT = 40


@dataclass(frozen=True)
class ThrottlePolicy:
    """The thresholds for one key space, in whole seconds."""

    free: int
    base_seconds: int
    max_seconds: int
    window_seconds: int

    def __post_init__(self) -> None:
        if min(self.free, self.base_seconds, self.max_seconds, self.window_seconds) <= 0:
            raise ValueError("throttle thresholds must be positive")
        if self.max_seconds < self.base_seconds:
            raise ValueError("max_seconds must be at least base_seconds")

    def delay_seconds(self, failures: int) -> int:
        """The delay after the ``failures``-th failure in a row; 0 while still free."""
        over = failures - self.free
        if over <= 0:
            return 0
        delay: int = self.base_seconds * 2 ** min(over, _MAX_EXPONENT)
        return min(delay, self.max_seconds)


@dataclass
class _Entry:
    failures: int
    last_failure: datetime
    blocked_until: datetime | None = None
    #: The ``blocked_until`` whose first refusal has been reported already.
    refusal_reported_for: datetime | None = None


@dataclass(frozen=True)
class ThrottleEvent:
    """Something to log: a key just became throttled, or a request was refused."""

    kind: EventKind
    key_type: KeyType
    failures: int
    #: The new delay ("throttled"), or the time left until the next attempt ("refused").
    delay_seconds: int


class KeyThrottle:
    """Failure counts and delays for one key space. Not locked; ``LoginThrottle`` is."""

    def __init__(
        self, key_type: KeyType, policy: ThrottlePolicy, max_keys: int = DEFAULT_MAX_KEYS
    ) -> None:
        if max_keys <= 0:
            raise ValueError("max_keys must be positive")
        self.key_type = key_type
        self.policy = policy
        self.max_keys = max_keys
        #: Ordered by last failure, oldest first.
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def _expired(self, entry: _Entry, now: datetime) -> bool:
        forgotten = now >= entry.last_failure + timedelta(seconds=self.policy.window_seconds)
        free = entry.blocked_until is None or now >= entry.blocked_until
        return forgotten and free

    def _get(self, key: str, now: datetime) -> _Entry | None:
        entry = self._entries.get(key)
        if entry is not None and self._expired(entry, now):
            del self._entries[key]
            return None
        return entry

    def wait_seconds(self, key: str, now: datetime) -> float:
        """Seconds until ``key`` may try again; 0 when it may try now."""
        entry = self._get(key, now)
        if entry is None or entry.blocked_until is None or now >= entry.blocked_until:
            return 0.0
        return (entry.blocked_until - now).total_seconds()

    def refusal(self, key: str, now: datetime) -> ThrottleEvent | None:
        """The event to log for refusing ``key`` now, or ``None`` when ``key`` is not
        throttled or this throttle period's refusal was reported already."""
        entry = self._get(key, now)
        if entry is None or entry.blocked_until is None or now >= entry.blocked_until:
            return None
        if entry.refusal_reported_for == entry.blocked_until:
            return None
        entry.refusal_reported_for = entry.blocked_until
        left = math.ceil((entry.blocked_until - now).total_seconds())
        return ThrottleEvent("refused", self.key_type, entry.failures, left)

    def record_failure(self, key: str, now: datetime) -> ThrottleEvent | None:
        """Count a failed login for ``key``. Returns an event when it is now throttled."""
        entry = self._get(key, now)
        if entry is None:
            self._make_room(now)
            entry = _Entry(failures=0, last_failure=now)
            self._entries[key] = entry
        entry.failures += 1
        entry.last_failure = now
        self._entries.move_to_end(key)
        delay = self.policy.delay_seconds(entry.failures)
        if delay == 0:
            return None
        entry.blocked_until = now + timedelta(seconds=delay)
        return ThrottleEvent("throttled", self.key_type, entry.failures, delay)

    def clear(self, key: str) -> None:
        self._entries.pop(key, None)

    def _make_room(self, now: datetime) -> None:
        """Before adding a key: drop expired keys from the old end, then, if still
        full, the key whose last failure is oldest."""
        while self._entries:
            oldest_key, oldest = next(iter(self._entries.items()))
            if not self._expired(oldest, now):
                break
            del self._entries[oldest_key]
        while len(self._entries) >= self.max_keys:
            self._entries.popitem(last=False)


@dataclass(frozen=True)
class Refusal:
    """A login attempt refused while throttled."""

    retry_after_seconds: int
    events: tuple[ThrottleEvent, ...]


class LoginThrottle:
    """Both key spaces behind one lock. One per app (``app.state.login_throttle``)."""

    def __init__(
        self,
        account: ThrottlePolicy,
        ip: ThrottlePolicy,
        max_keys: int = DEFAULT_MAX_KEYS,
    ) -> None:
        self.account = KeyThrottle("account", account, max_keys)
        self.ip = KeyThrottle("ip", ip, max_keys)
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> LoginThrottle:
        common = {
            "base_seconds": settings.login_throttle_base_seconds,
            "max_seconds": settings.login_throttle_max_seconds,
            "window_seconds": settings.login_throttle_window_seconds,
        }
        return cls(
            account=ThrottlePolicy(free=settings.login_throttle_account_free, **common),
            ip=ThrottlePolicy(free=settings.login_throttle_ip_free, **common),
        )

    def check(self, account: str, ip: str, now: datetime) -> Refusal | None:
        """``None`` when the attempt may go ahead, else the refusal. A request is
        refused when either key is throttled. ``events`` holds the first refusal of
        each throttle period only, so a flood of refused requests logs once."""
        with self._lock:
            wait = max(self.account.wait_seconds(account, now), self.ip.wait_seconds(ip, now))
            if wait <= 0:
                return None
            events = tuple(
                event
                for event in (self.account.refusal(account, now), self.ip.refusal(ip, now))
                if event is not None
            )
            return Refusal(retry_after_seconds=max(1, math.ceil(wait)), events=events)

    def failure(self, account: str, ip: str, now: datetime) -> tuple[ThrottleEvent, ...]:
        """Count a failed login against both keys; the events of keys now throttled."""
        with self._lock:
            events = (
                self.account.record_failure(account, now),
                self.ip.record_failure(ip, now),
            )
            return tuple(event for event in events if event is not None)

    def success(self, account: str) -> None:
        """A successful login clears the account key, not the IP key."""
        with self._lock:
            self.account.clear(account)


def account_hash(normalised_email: str) -> str:
    """A short, stable id for an account key in logs: never the raw email."""
    return hashlib.sha256(normalised_email.encode()).hexdigest()[:12]


def get_login_throttle(request: Request) -> LoginThrottle:
    """FastAPI dependency: the app's throttle (set by ``create_app``)."""
    throttle: LoginThrottle = request.app.state.login_throttle
    return throttle


ThrottleDep = Annotated[LoginThrottle, Depends(get_login_throttle)]
