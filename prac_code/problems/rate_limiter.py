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
class TokenBucket:
    def __init__(self, rate: int, burst: int, clock=time.monotonic):
        self.rate = rate
        self.burst = burst
        self.tokens: float = burst
        self.clock = clock
        self.last_check = self.clock()
        self.lock = threading.Lock()

    def check(self, weight=1) -> bool:
        with self.lock:
            # Refill tokens based on elapsed time, by second
            now = self.clock()
            elapsed = now - self.last_check
            refill_amount = elapsed * self.rate
            self.tokens = min(self.burst, self.tokens + refill_amount)

            # Check tokens
            self.last_check = now
            if self.tokens >= weight:
                self.tokens -= weight
                return True
            else:
                return False

    def is_stale(self) -> bool:
        now = self.clock()
        elapsed = now - self.last_check
        refill_amount = elapsed * self.rate
        return refill_amount + self.tokens >= self.burst and elapsed > 30


class RateLimiter:
    SHARDS = 16  # Number of shards for the buckets dictionary
    CAPACITY = 1000  # Soft limit for the number of keys per shard

    def __init__(self, rate: int, burst: int, clock=time.monotonic):
        if rate <= 0 or burst <= 0:
            raise ValueError("Rate and burst must be positive integers")
        self.rate = rate
        self.burst = burst
        self.clock = clock
        self.shards: list[tuple[threading.Lock, dict[str, TokenBucket]]] = [
            (threading.Lock(), {}) for _ in range(self.SHARDS)
        ]

        # Cleanup daemon
        self._cleanup = threading.Thread(
            target=self._stale_buckets_cleanup_daemon, daemon=True
        )
        self._cleanup.start()

    def allow(self, key, weight: int = 1) -> bool:
        if type(weight) is not int or weight <= 0:
            raise ValueError("Weight must be positive")
        if weight > self.burst:
            return False  # Deny requests we cannot ever satisfy

        shard_index = hash(key) % self.SHARDS
        lock, buckets = self.shards[shard_index]
        with lock:
            bucket = buckets.setdefault(
                key, TokenBucket(self.rate, self.burst, clock=self.clock)
            )
            return bucket.check(weight)

    def _stale_buckets_cleanup_daemon(self):
        while True:
            time.sleep(60)  # Check every 60 seconds
            for lock, buckets in self.shards:
                if len(buckets) > self.CAPACITY * 0.8:  # Start cleanup when 80% full
                    with lock:
                        # Best-effort cleanup
                        stale_keys = [k for k, b in buckets.items() if b.is_stale()]
                        for k in stale_keys:
                            del buckets[k]


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
