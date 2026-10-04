#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///

"""
A load balancer with automated health checking.

Core requirements:
- Automatically health checks the registered endpoints and remove unhealthy ones.
- Restore unhealthy endpoints to the pool after passing consecutive health checks.
- Slow/hung health checks cannot block other health checks or the overall operation.

Stretch goals:
- Smart backoff of health checks for unhealthy endpoints.
- Smart scheduling of health checks for optimal performance when many endpoints are being monitored.
"""

import threading
import time
from contextlib import contextmanager

# ---------- Scaffolding ----------
CHECK_INTERVAL = 1.0  # Seconds between checks of one endpoint
CHECK_TIMEOUT = 0.5  # A check slower than this counts as a failure
HEALTHY_THRESHOLD = 3  # Consecutive passes needed to re-enable


class NoHealthyEndpointError(Exception):
    """Raised by route() when no healthy endpoint is available."""


class Endpoint:
    """
    A fake backend server endpoint with simulated health states.

    Mode controls how health_check() behaves:
      HEALTHY  -> returns True
      FAILING  -> returns False
      ERROR    -> raises ConnectionError
      HANG     -> blocks until the mode is changed or 30 seconds elapse
    """

    HEALTHY, FAILING, ERROR, HANG = "healthy", "failing", "error", "hang"

    def __init__(self, name: str, mode=HEALTHY):
        self.name = name
        self.mode = mode
        self.check_count = 0
        self._lock = threading.Lock()

    def health_check(self):
        with self._lock:
            self.check_count += 1
        if self.mode == Endpoint.HANG:
            deadline = time.monotonic() + 30
            while self.mode == Endpoint.HANG and time.monotonic() < deadline:
                time.sleep(0.05)
            return True
        time.sleep(0.02)  # small network delay
        if self.mode == Endpoint.ERROR:
            raise ConnectionError(f"{self.name}: connection refused")
        return self.mode == Endpoint.HEALTHY

    def __repr__(self):
        return f"Endpoint({self.name})"


# ---------- Implementation ----------
class LoadBalancer:
    def __init__(self):
        raise NotImplementedError

    def register(self, endpoint: Endpoint):
        raise NotImplementedError

    def deregister(self, endpoint: Endpoint):
        raise NotImplementedError

    def route(self) -> str:
        raise NotImplementedError


# ---------- Testing ----------
TEST_TIMEOUT = 30
GRACE = CHECK_INTERVAL + CHECK_TIMEOUT + 1.0  # Time to notice a failure
RECOVERY = HEALTHY_THRESHOLD * CHECK_INTERVAL + 2.5  # Time to be re-enabled


@contextmanager
def balancer(*endpoints):
    """A fresh LoadBalancer with the endpoints registered; cleans up after."""
    lb = LoadBalancer()
    try:
        for ep in endpoints:
            lb.register(ep)
        yield lb
    finally:
        for ep in endpoints:
            try:
                lb.deregister(ep)
            except Exception:
                pass
            ep.mode = Endpoint.HEALTHY


def routed(lb, n=200):
    """The set of endpoints returned by n route() calls."""
    out = set()
    for _ in range(n):
        try:
            out.add(lb.route())
        except NoHealthyEndpointError:
            pass
    return out


def wait_until(pred, timeout):
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() >= end:
            return False
        time.sleep(0.05)
    return True


def t_routes_to_registered_healthy_endpoints():
    a, b, c = Endpoint("a"), Endpoint("b"), Endpoint("c")
    with balancer(a, b, c) as lb:
        assert routed(lb) == {a, b, c}, f"Expected all of a, b, c, got {routed(lb)}"
    with balancer() as lb:
        try:
            got = lb.route()
        except NoHealthyEndpointError:
            return
        raise AssertionError(f"route() returned {got} with nothing registered")


def t_failing_endpoints_are_removed():
    good = Endpoint("good")
    down = Endpoint("down", Endpoint.FAILING)
    broken = Endpoint("broken", Endpoint.ERROR)
    with balancer(good, down, broken) as lb:
        assert wait_until(lambda: routed(lb) == {good}, GRACE), (
            f"After {GRACE}s still routing to {routed(lb)}, expected only good"
        )
        time.sleep(1.5)
        assert routed(lb) == {good}, f"Routing to {routed(lb)}, expected only good"


def t_endpoint_recovers_after_three_consecutive_passes():
    good = Endpoint("good")
    flaky = Endpoint("flaky", Endpoint.ERROR)
    with balancer(good, flaky) as lb:
        assert wait_until(lambda: routed(lb) == {good}, GRACE), "Flaky not removed"
        flaky.mode = Endpoint.HEALTHY
        time.sleep(1.5)
        assert flaky not in routed(lb), "Re-enabled before 3 consecutive passes"
        assert wait_until(lambda: flaky in routed(lb), RECOVERY), (
            "Endpoint was never re-enabled after passing repeatedly"
        )


def t_hung_endpoints_do_not_block_other_checks():
    hung = [Endpoint(f"hung{i}", Endpoint.HANG) for i in range(4)]
    good, victim = Endpoint("good"), Endpoint("victim")
    t0 = time.monotonic()
    with balancer(*hung, good, victim) as lb:
        assert time.monotonic() - t0 < 0.5, "register() blocked on a hung check"
        assert wait_until(lambda: routed(lb) == {good, victim}, GRACE), (
            f"After {GRACE}s still routing to {routed(lb)}, expected good and victim"
        )
        victim.mode = Endpoint.FAILING
        assert wait_until(lambda: routed(lb) == {good}, GRACE), (
            "Victim went bad but was not removed while other endpoints hang"
        )


def t_deregister_removes_endpoint():
    a, b = Endpoint("a"), Endpoint("b")
    with balancer(a, b) as lb:
        time.sleep(1.2)
        lb.deregister(b)
        assert b not in routed(lb), "Deregistered endpoint still returned by route()"
        time.sleep(1.0)
        before = b.check_count
        time.sleep(2.0)
        assert b.check_count == before, (
            "Deregistered endpoint is still being health-checked"
        )
        assert routed(lb) == {a}


TESTS = [
    t_routes_to_registered_healthy_endpoints,
    t_failing_endpoints_are_removed,
    t_endpoint_recovers_after_three_consecutive_passes,
    t_hung_endpoints_do_not_block_other_checks,
    t_deregister_removes_endpoint,
]


def run_with_timeout(test, timeout=TEST_TIMEOUT):
    """Run a single test in a daemon thread with timeout."""
    result = []

    def run():
        try:
            test()
            result.append(None)
        except AssertionError as e:
            result.append(str(e) or "Assertion failed")
        except Exception as e:
            result.append(f"{type(e).__name__}: {e}")

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    return result[0] if result else "Test timed out"


def run_tests():
    passed = 0
    for t in TESTS:
        print(f"{t.__name__}:")
        error = run_with_timeout(t)
        passed += error is None
        print(f"{'  OK:' if error is None else '  FAIL:'} {t.__name__[2:]:<55} ")
        if error:
            print(f"        -> {error}")
    print(f"\n{passed}/{len(TESTS)} tests passed")


if __name__ == "__main__":
    run_tests()
