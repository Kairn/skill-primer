#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///

"""
A weighted rate limiter that supports burst and per-key level throttling.

Core requirements:
- Rate limiting with burst support for a single tenant.
- Concurrent access from multiple threads.

Stretch goals:
- Multi-tenant support with per-key rate limiting.
- Memory cap for a large number of tenants.
- Efficient handling of highly contended keys.
"""

import sys
import threading
import time
import traceback


# ---------- Scaffolding ----------
class FakeClock:
    """A clock that only moves when you tell it to."""

    def __init__(self, start=1000.0):
        self._now = float(start)

    def __call__(self):
        return self._now

    def advance(self, seconds):
        self._now += seconds


def make(rate=10, burst=10):
    """Build a limiter wired to a FakeClock. Returns (limiter, clock)."""
    clock = FakeClock()
    return RateLimiter(rate, burst, clock=clock), clock


# ---------- Implementation ----------
class RateLimiter:
    def __init__(self, rate, burst, clock=time.monotonic):
        # TODO
        raise NotImplementedError

    def allow(self, key, weight=1):
        # TODO
        raise NotImplementedError


# ---------- Testing ----------
def t_new_key_starts_with_full_burst():
    rl, _ = make(rate=1, burst=5)
    assert [rl.allow("a") for _ in range(7)] == [True] * 5 + [False] * 2


def t_denied_request_consumes_nothing():
    rl, _ = make(rate=1, burst=5)
    assert rl.allow("a", 4)
    assert not rl.allow("a", 3)  # only 1 left
    assert rl.allow("a", 1)  # the denial must not have used it up
    assert not rl.allow("a", 1)


def t_budget_refills_over_time():
    rl, clock = make(rate=10, burst=10)
    assert rl.allow("a", 10)
    assert not rl.allow("a", 1)
    clock.advance(0.5)  # 0.5s * 10/s = 5 back
    assert rl.allow("a", 5)
    assert not rl.allow("a", 1)


def t_idle_key_is_capped_at_burst():
    rl, clock = make(rate=10, burst=10)
    assert rl.allow("a", 10)
    clock.advance(3600)
    assert rl.allow("a", 10)
    assert not rl.allow("a", 1), "budget grew past burst while idle"


def t_weighted_requests():
    rl, clock = make(rate=1, burst=10)
    assert rl.allow("a", 6)
    assert rl.allow("a", 4)
    assert not rl.allow("a", 1)
    clock.advance(3)
    assert not rl.allow("a", 4)  # only 3 available
    assert rl.allow("a", 3)
    assert rl.allow("b", 10)
    assert rl.allow("c", 11) is False  # larger than burst: denied, not an error


def t_keys_are_independent():
    rl, _ = make(rate=1, burst=3)
    assert [rl.allow("a") for _ in range(4)] == [True, True, True, False]
    assert [rl.allow("b") for _ in range(4)] == [True, True, True, False]


def t_invalid_inputs_raise_value_error():
    for rate, burst in [(0, 5), (5, 0), (-1, 5)]:
        try:
            RateLimiter(rate, burst)
        except ValueError:
            pass
        else:
            raise AssertionError(
                f"RateLimiter({rate}, {burst}) should raise ValueError"
            )
    rl, _ = make(rate=1, burst=5)
    for bad in (0, -1, "1", None):
        try:
            rl.allow("a", bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"allow(weight={bad!r}) should raise ValueError")


def t_threads_sharing_one_key_never_overspend():
    # The clock is frozen, so no refill happens: exactly `burst` requests
    # can ever succeed, no matter how the threads interleave.
    rl, _ = make(rate=1, burst=1000)
    counts = [0] * 16

    def worker(i):
        for _ in range(200):
            if rl.allow("hot"):
                counts[i] += 1

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # switch threads aggressively to expose races
    try:
        threads = [
            threading.Thread(target=worker, args=(i,), daemon=True) for i in range(16)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert not any(t.is_alive() for t in threads), (
            "threads did not finish (deadlock?)"
        )
    finally:
        sys.setswitchinterval(old)
    assert sum(counts) == 1000, f"admitted {sum(counts)}, expected exactly 1000"


TESTS = [
    t_new_key_starts_with_full_burst,
    t_denied_request_consumes_nothing,
    t_budget_refills_over_time,
    t_idle_key_is_capped_at_burst,
    t_weighted_requests,
    t_keys_are_independent,
    t_invalid_inputs_raise_value_error,
    t_threads_sharing_one_key_never_overspend,
]


def run_tests():
    passed = 0
    for t in TESTS:
        print(f"{t.__name__}:")
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL: {e}")
        except Exception:
            print(f"  ERROR: {traceback.format_exc().strip().splitlines()[-1]}")
        else:
            print(f"  OK: {t.__name__}")
            passed += 1
    print(f"\n{passed}/{len(TESTS)} tests passed")


if __name__ == "__main__":
    run_tests()
