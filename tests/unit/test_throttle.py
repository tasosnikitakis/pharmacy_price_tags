"""Login throttling state (``core.app.throttle``, decision 0019). No database."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from core.app.throttle import (
    KeyThrottle,
    LoginThrottle,
    ThrottleEvent,
    ThrottlePolicy,
    account_hash,
)
from core.config import Settings

T0 = datetime(2026, 12, 1, 8, 0, tzinfo=UTC)
ACCOUNT = ThrottlePolicy(free=5, base_seconds=2, max_seconds=900, window_seconds=3600)
IP = ThrottlePolicy(free=20, base_seconds=2, max_seconds=900, window_seconds=3600)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def throttle(max_keys: int = 1000) -> LoginThrottle:
    return LoginThrottle(account=ACCOUNT, ip=IP, max_keys=max_keys)


def test_free_failures_cost_nothing() -> None:
    lt = throttle()
    for _ in range(ACCOUNT.free):
        assert lt.failure("a@example.test", "10.0.0.1", T0) == ()
        assert lt.check("a@example.test", "10.0.0.1", T0) is None


def test_delay_doubles_up_to_the_cap() -> None:
    assert [ACCOUNT.delay_seconds(n) for n in range(1, 6)] == [0, 0, 0, 0, 0]
    assert [ACCOUNT.delay_seconds(n) for n in range(6, 16)] == [
        2, 4, 8, 16, 32, 64, 128, 256, 512, 900,
    ]  # fmt: skip
    assert ACCOUNT.delay_seconds(10_000) == 900


def test_failures_set_the_next_allowed_attempt() -> None:
    kt = KeyThrottle("account", ACCOUNT)
    now = T0
    for _ in range(ACCOUNT.free):
        assert kt.record_failure("k", now) is None
    expected = [2, 4, 8, 16, 32, 64, 128, 256, 512, 900, 900]
    for i, delay in enumerate(expected, start=ACCOUNT.free + 1):
        event = kt.record_failure("k", now)
        assert event == ThrottleEvent("throttled", "account", i, delay)
        assert kt.wait_seconds("k", now) == delay
        assert kt.wait_seconds("k", now + timedelta(seconds=delay - 1)) == 1
        now += timedelta(seconds=delay)
        assert kt.wait_seconds("k", now) == 0  # always unlocks: no permanent lockout


def test_refused_while_throttled_and_allowed_after() -> None:
    lt = throttle()
    for _ in range(ACCOUNT.free + 1):
        lt.failure("a", "ip", T0)
    refusal = lt.check("a", "ip", at(0.5))
    assert refusal is not None
    assert refusal.retry_after_seconds == 2
    assert refusal.events == (ThrottleEvent("refused", "account", 6, 2),)
    assert lt.check("a", "ip", at(2)) is None


def test_refusals_are_reported_once_per_throttle_period() -> None:
    lt = throttle()
    for _ in range(ACCOUNT.free + 1):
        lt.failure("a", "ip", T0)
    first = lt.check("a", "ip", T0)
    second = lt.check("a", "ip", at(1))
    assert first is not None and second is not None
    assert len(first.events) == 1
    assert second.events == ()  # still refused, not reported again
    assert second.retry_after_seconds == 1
    # A new failure starts a new throttle period, whose first refusal is reported.
    lt.failure("a", "ip", at(5))
    third = lt.check("a", "ip", at(6))
    assert third is not None
    assert third.events == (ThrottleEvent("refused", "account", 7, 3),)


def test_retry_after_rounds_up_to_whole_seconds() -> None:
    lt = throttle()
    for _ in range(ACCOUNT.free + 1):
        lt.failure("a", "ip", T0)
    refusal = lt.check("a", "ip", at(1.9))
    assert refusal is not None
    assert refusal.retry_after_seconds == 1


def test_failures_are_forgotten_after_the_window() -> None:
    lt = throttle()
    for _ in range(ACCOUNT.free):
        lt.failure("a", "ip", T0)
    # One second short of the window: still counted, so the next failure throttles.
    assert lt.failure("a", "ip", at(3599)) != ()
    lt2 = throttle()
    for _ in range(ACCOUNT.free):
        lt2.failure("a", "ip", T0)
    # A full window without a failure: forgotten, the next failure is free again.
    assert lt2.failure("a", "ip", at(3600)) == ()
    assert lt2.account._entries["a"].failures == 1


def test_window_never_ends_a_running_delay() -> None:
    policy = ThrottlePolicy(free=1, base_seconds=100, max_seconds=900, window_seconds=10)
    kt = KeyThrottle("account", policy)
    kt.record_failure("k", T0)
    kt.record_failure("k", T0)  # 100 s delay, longer than the 10 s window
    assert kt.wait_seconds("k", at(50)) == 50
    assert kt.wait_seconds("k", at(100)) == 0
    assert "k" not in kt


def test_success_clears_the_account_key_but_not_the_ip_key() -> None:
    lt = throttle()
    for _ in range(4):
        lt.failure("a", "ip", T0)
    lt.success("a")
    assert "a" not in lt.account
    assert lt.ip._entries["ip"].failures == 4
    # The account starts from zero again: five more free failures.
    for _ in range(ACCOUNT.free):
        assert lt.failure("a", "ip", T0) == ()


def test_ip_and_account_thresholds_are_independent() -> None:
    lt = throttle()
    # Many accounts, one failure each, from one IP: no account is throttled, and the
    # IP is throttled only at its own, higher threshold.
    for i in range(IP.free):
        assert lt.failure(f"user{i}", "shop", T0) == ()
    assert lt.check("user0", "shop", T0) is None
    events = lt.failure("user-last", "shop", T0)
    assert events == (ThrottleEvent("throttled", "ip", IP.free + 1, 2),)
    refusal = lt.check("never-failed", "shop", at(1))
    assert refusal is not None
    assert refusal.events[0].key_type == "ip"
    # Another IP for any of those accounts is not affected.
    assert lt.check("user0", "elsewhere", at(1)) is None


def test_one_account_from_many_ips_trips_the_account_key() -> None:
    lt = throttle()
    for i in range(ACCOUNT.free):
        lt.failure("a", f"10.0.0.{i}", T0)
    events = lt.failure("a", "10.0.0.99", T0)
    assert events == (ThrottleEvent("throttled", "account", 6, 2),)
    refusal = lt.check("a", "10.0.0.200", at(1))
    assert refusal is not None
    assert lt.check("b", "10.0.0.1", at(1)) is None


def test_both_keys_throttled_report_both() -> None:
    policy = ThrottlePolicy(free=1, base_seconds=2, max_seconds=900, window_seconds=3600)
    lt = LoginThrottle(account=policy, ip=policy)
    lt.failure("a", "ip", T0)
    events = lt.failure("a", "ip", T0)
    assert {e.key_type for e in events} == {"account", "ip"}
    refusal = lt.check("a", "ip", T0)
    assert refusal is not None
    assert {e.key_type for e in refusal.events} == {"account", "ip"}


def test_size_is_bounded_and_the_oldest_key_goes_first() -> None:
    kt = KeyThrottle("account", ACCOUNT, max_keys=3)
    for i, key in enumerate(("a", "b", "c")):
        kt.record_failure(key, at(i))
    kt.record_failure("a", at(3))  # "a" is now the most recent
    kt.record_failure("d", at(4))
    assert len(kt) == 3
    assert "b" not in kt
    assert {"a", "c", "d"} == set(kt._entries)


def test_expired_keys_are_dropped_before_live_ones() -> None:
    kt = KeyThrottle("account", ACCOUNT, max_keys=3)
    kt.record_failure("old", T0)
    kt.record_failure("x", at(3000))
    kt.record_failure("y", at(3000))
    kt.record_failure("z", at(3700))  # "old" has expired by now
    assert set(kt._entries) == {"x", "y", "z"}


def test_many_keys_stay_within_the_bound() -> None:
    lt = throttle(max_keys=50)
    for i in range(1000):
        lt.failure(f"user{i}", f"10.0.{i // 256}.{i % 256}", T0)
    assert len(lt.account) == 50
    assert len(lt.ip) == 50


def test_concurrent_failures_are_all_counted() -> None:
    policy = ThrottlePolicy(free=10_000, base_seconds=2, max_seconds=900, window_seconds=3600)
    lt = LoginThrottle(account=policy, ip=policy, max_keys=100)
    threads, per_thread = 8, 500
    barrier = threading.Barrier(threads)

    def work(n: int) -> None:
        barrier.wait()
        for i in range(per_thread):
            lt.failure("shared", "ip", T0)
            lt.failure(f"t{n}-{i % 20}", "ip", T0)
            lt.check("shared", "ip", T0)

    pool = [threading.Thread(target=work, args=(n,)) for n in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()
    assert lt.account._entries["shared"].failures == threads * per_thread
    assert lt.ip._entries["ip"].failures == 2 * threads * per_thread
    assert len(lt.account) <= 100


def test_account_hash_is_short_and_not_the_email() -> None:
    digest = account_hash("someone@example.test")
    assert len(digest) == 12
    assert "someone" not in digest
    assert digest == account_hash("someone@example.test")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"free": 0, "base_seconds": 2, "max_seconds": 900, "window_seconds": 3600},
        {"free": 5, "base_seconds": 0, "max_seconds": 900, "window_seconds": 3600},
        {"free": 5, "base_seconds": 10, "max_seconds": 5, "window_seconds": 3600},
        {"free": 5, "base_seconds": 2, "max_seconds": 900, "window_seconds": 0},
    ],
)
def test_policy_validation(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        ThrottlePolicy(**kwargs)


# --- Settings -------------------------------------------------------------------------


def make_settings(**overrides: Any) -> Settings:
    """Settings with a placeholder database URL; secrets come from the environment."""
    url = SecretStr("postgresql+psycopg://u@localhost/db")
    return Settings(database_url=url, **overrides)


def test_settings_defaults() -> None:
    settings = make_settings()
    assert settings.login_throttle_account_free == 5
    assert settings.login_throttle_ip_free == 20
    assert settings.login_throttle_base_seconds == 2
    assert settings.login_throttle_max_seconds == 900
    assert settings.login_throttle_window_seconds == 3600
    lt = LoginThrottle.from_settings(settings)
    assert lt.account.policy == ACCOUNT
    assert lt.ip.policy == IP


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOGIN_THROTTLE_ACCOUNT_FREE", "3")
    monkeypatch.setenv("LOGIN_THROTTLE_IP_FREE", "30")
    settings = make_settings()
    assert settings.login_throttle_account_free == 3
    assert settings.login_throttle_ip_free == 30


@pytest.mark.parametrize(
    "overrides",
    [
        {"login_throttle_account_free": 0},
        {"login_throttle_ip_free": -1},
        {"login_throttle_base_seconds": 0},
        {"login_throttle_max_seconds": 0},
        {"login_throttle_window_seconds": 0},
        {"login_throttle_base_seconds": 10, "login_throttle_max_seconds": 5},
        {"login_throttle_account_free": 10, "login_throttle_ip_free": 5},
        {"login_throttle_account_free": 7, "login_throttle_ip_free": 7},
    ],
)
def test_settings_validation(overrides: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        make_settings(**overrides)


def test_settings_need_a_larger_ip_allowance() -> None:
    with pytest.raises(ValidationError, match="greater than"):
        make_settings(login_throttle_account_free=7, login_throttle_ip_free=7)
    settings = make_settings(login_throttle_account_free=7, login_throttle_ip_free=8)
    assert settings.login_throttle_ip_free == 8
