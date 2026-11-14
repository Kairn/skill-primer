#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///

"""
A circuit breaker for an unreliable API that trips after repeated failures and recovers after a cooldown with trials.

Core requirements:
- Trip after a configurable number of consecutive failures and block calls when circuit is open.
- Transition to a half-open state after a cooldown and allow trial calls.
- Close the circuit after a configurable number of successful trial calls, or transition back to open on failure.
- Handle concurrent calls safely.

Stretch goals:
- Implement escalating cooldown for trial failures.
- Handle stale results correctly (e.g. results that arrive after the circuit has already transitioned).
"""

import threading
import time
import unittest
from collections.abc import Callable
from dataclasses import dataclass, field


# ---------- Scaffolding ----------
@dataclass(frozen=True)
class Request:
    path: str = "/v1/resource"
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Response:
    status: int
    body: dict


class ApiError(Exception):
    """Raised by the API when a request fails."""


class CircuitOpenError(Exception):
    """Raised by the breaker when a call is rejected without reaching the API."""


class FakeClock:
    """Manually controlled clock. Pass an instance as the breaker's `clock`."""

    def __init__(self, start=1000.0):
        self._now = start

    def __call__(self):
        return self._now

    def advance(self, seconds):
        self._now += seconds


class FlakyApi:
    """
    Fake API: call it like `api(request)`.

    Returns a Response or raises ApiError.
    """

    def __init__(self, script=(), healthy=True):
        self.script = list(script)
        self.healthy = healthy
        self.on_call: Callable[[Request], None] | None = None
        self.call_count = 0
        self._lock = threading.Lock()

    def __call__(self, request: Request) -> Response:
        with self._lock:
            self.call_count += 1
            ok = self.script.pop(0) if self.script else self.healthy
        if self.on_call is not None:
            self.on_call(request)
        if not ok:
            raise ApiError("api failure")
        return Response(200, {"echo": request.path})


# ---------- Implementation ----------
class CircuitBreaker:
    def __init__(
        self,
        api: Callable[[Request], Response],
        failure_threshold: int = 3,
        recovery_timeout: float = 5.0,
        success_threshold: int = 2,
        clock: Callable[[], float] = time.monotonic,
        backoff_multiplier: float = 1.0,
        max_recovery_timeout: float | None = None,
    ):
        self.api = api
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold
        self.clock = clock
        self.backoff_multiplier = backoff_multiplier
        self.max_recovery_timeout = max_recovery_timeout or float("inf")

        self._lock = threading.Lock()
        self._state = 0  # 0 = closed, 1 = open, 2 = half-open
        self._next_recovery_timeout = recovery_timeout
        self._consecutive_failures = 0
        self._trial_id = 0
        self._in_trial = False
        self._trial_successes = 0
        self._last_transition_time = 0.0

    def call(self, request: Request) -> Response:
        """
        Send `request` to the API through the breaker.

        Returns the API's Response, raises CircuitOpenError if rejected,
        and re-raises any exception from the API unchanged.
        """

        trial_id: int = -1

        with self._lock:
            if self._state == 1:
                if (
                    self.clock() - self._last_transition_time
                    < self._next_recovery_timeout
                ):
                    raise CircuitOpenError("circuit is open")
                # Transition to half-open
                self._state = 2
                self._last_transition_time = self.clock()
                self._in_trial = True
                self._trial_id += 1
                self._trial_successes = 0
                trial_id = self._trial_id
            elif self._state == 2:
                if self._in_trial:
                    raise CircuitOpenError("circuit is open")
                self._in_trial = True
                self._trial_id += 1
                trial_id = self._trial_id

        response: Response | None = None
        exception: BaseException | None = None
        start_time: float = self.clock()

        try:
            response = self.api(request)
        except BaseException as e:
            if isinstance(e, ApiError):
                exception = e
            else:
                # Some unexpected exception, don't count
                with self._lock:
                    if trial_id == self._trial_id:
                        self._in_trial = False
                raise

        with self._lock:
            if response:
                if trial_id == self._trial_id:
                    self._in_trial = False
                    self._trial_successes += 1
                    if self._trial_successes >= self.success_threshold:
                        self._state = 0
                        self._consecutive_failures = 0
                        self._last_transition_time = self.clock()
                elif self._state == 0 and start_time >= self._last_transition_time:
                    self._consecutive_failures = 0
                return response
            if exception:
                if trial_id == self._trial_id:
                    self._in_trial = False
                    self._state = 1
                    self._last_transition_time = self.clock()
                    self._next_recovery_timeout = min(
                        self._next_recovery_timeout * self.backoff_multiplier,
                        self.max_recovery_timeout,
                    )
                elif self._state == 0 and start_time >= self._last_transition_time:
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= self.failure_threshold:
                        self._state = 1
                        self._last_transition_time = self.clock()
                        self._next_recovery_timeout = self.recovery_timeout
                raise exception


# ---------- Testing ----------
REQ = Request("/users/1")


def attempt(breaker, request=REQ):
    """Make one call and report what happened: 'ok', 'error' or 'rejected'."""
    try:
        breaker.call(request)
        return "ok"
    except CircuitOpenError:
        return "rejected"
    except ApiError:
        return "error"


class CircuitBreakerTests(unittest.TestCase):
    def make(self, script=(), **overrides):
        clock = FakeClock()
        api = FlakyApi(script)
        params = {
            "failure_threshold": 3,
            "recovery_timeout": 10.0,
            "success_threshold": 2,
            "clock": clock,
        }
        params.update(overrides)
        return CircuitBreaker(api, **params), api, clock

    def trip(self, breaker, api):
        api.healthy = False
        self.assertEqual([attempt(breaker) for _ in range(3)], ["error"] * 3)
        api.healthy = True

    def test_passes_through_response(self):
        breaker, api, _ = self.make()
        response = breaker.call(Request("/users/42"))
        self.assertEqual(response, Response(200, {"echo": "/users/42"}))
        self.assertEqual(api.call_count, 1)

    def test_trips_after_consecutive_failures(self):
        breaker, api, _ = self.make()
        api.healthy = False
        for _ in range(3):
            with self.assertRaises(ApiError):
                breaker.call(REQ)
        api.healthy = True
        self.assertEqual(attempt(breaker), "rejected")
        self.assertEqual(api.call_count, 3)

    def test_success_resets_failure_count(self):
        breaker, api, _ = self.make(script=[False, False, True, False, False])
        results = [attempt(breaker) for _ in range(5)]
        self.assertEqual(results, ["error", "error", "ok", "error", "error"])
        self.assertEqual(attempt(breaker), "ok")
        self.assertEqual(api.call_count, 6)

    def test_open_rejects_without_calling_api(self):
        breaker, api, clock = self.make()
        self.trip(breaker, api)
        clock.advance(9.9)
        self.assertEqual([attempt(breaker) for _ in range(5)], ["rejected"] * 5)
        self.assertEqual(api.call_count, 3)

    def test_only_one_trial_in_flight_when_half_open(self):
        breaker, api, clock = self.make()
        self.trip(breaker, api)
        clock.advance(10.0)
        during_trial = []
        api.on_call = lambda req: during_trial.append(attempt(breaker))
        self.assertEqual(attempt(breaker), "ok")
        self.assertEqual(during_trial, ["rejected"])
        self.assertEqual(api.call_count, 4)

    def test_failed_trial_reopens_and_restarts_cooldown(self):
        breaker, api, clock = self.make()
        self.trip(breaker, api)
        clock.advance(10.0)
        api.healthy = False
        self.assertEqual(attempt(breaker), "error")
        api.healthy = True
        clock.advance(9.0)
        self.assertEqual(attempt(breaker), "rejected")
        self.assertEqual(api.call_count, 4)
        clock.advance(1.0)
        self.assertEqual(attempt(breaker), "ok")
        self.assertEqual(api.call_count, 5)

    def test_failure_during_trials_reopens_even_after_a_success(self):
        breaker, api, clock = self.make(success_threshold=2)
        self.trip(breaker, api)
        clock.advance(10.0)
        self.assertEqual(attempt(breaker), "ok")
        api.healthy = False
        self.assertEqual(attempt(breaker), "error")
        self.assertEqual(attempt(breaker), "rejected")

    def test_success_threshold_closes_breaker(self):
        breaker, api, clock = self.make(success_threshold=2)
        self.trip(breaker, api)
        clock.advance(10.0)
        self.assertEqual([attempt(breaker) for _ in range(2)], ["ok", "ok"])
        api.healthy = False
        results = [attempt(breaker) for _ in range(4)]
        self.assertEqual(results, ["error", "error", "error", "rejected"])

    def test_abnormal_exit_does_not_leak_trial_slot(self):
        breaker, api, clock = self.make()
        self.trip(breaker, api)
        clock.advance(10.0)

        def interrupted(req):
            api.on_call = None
            raise KeyboardInterrupt

        api.on_call = interrupted
        with self.assertRaises(KeyboardInterrupt):
            breaker.call(REQ)
        self.assertEqual(attempt(breaker), "ok")

    def test_cooldown_escalates_caps_and_resets(self):
        breaker, api, clock = self.make(
            recovery_timeout=10.0, backoff_multiplier=2.0, max_recovery_timeout=30.0
        )

        def failed_trial():
            api.healthy = False
            self.assertEqual(attempt(breaker), "error")
            api.healthy = True

        self.trip(breaker, api)
        clock.advance(10.0)
        failed_trial()
        clock.advance(19.0)
        self.assertEqual(attempt(breaker), "rejected")
        clock.advance(1.0)
        failed_trial()
        clock.advance(29.0)
        self.assertEqual(attempt(breaker), "rejected")
        clock.advance(1.0)
        self.assertEqual([attempt(breaker) for _ in range(2)], ["ok", "ok"])

        self.trip(breaker, api)
        clock.advance(9.0)
        self.assertEqual(attempt(breaker), "rejected")
        clock.advance(1.0)
        self.assertEqual(attempt(breaker), "ok")

    def test_late_result_from_before_a_state_change_is_ignored(self):
        """Call A is slow and fails late, after the breaker has tripped and
        closed again. A's failure must not count in the new closed period."""
        breaker, api, clock = self.make(
            script=[False, False, False, True], failure_threshold=2, success_threshold=1
        )
        mid_call = []

        def breaker_cycles_while_a_is_in_flight(req):
            api.on_call = None
            mid_call.extend(attempt(breaker) for _ in range(2))
            clock.advance(10.0)
            mid_call.append(attempt(breaker))

        api.on_call = breaker_cycles_while_a_is_in_flight
        self.assertEqual(attempt(breaker), "error")
        self.assertEqual(mid_call, ["error", "error", "ok"])

        api.healthy = False
        self.assertEqual(
            [attempt(breaker) for _ in range(3)], ["error", "error", "rejected"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
