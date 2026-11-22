#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///

"""
An idempotent handler that sits before the job processor and deduplicates requests based on client provided keys.

Core requirements:
- Identical jobs must not be submitted to the job processor more than once, within the TTL.
- Results (both success and failure) must be cached for the TTL, and returned to the client if they submit a duplicate request.
- Jobs are resubmitted once the cached results expire.
- Thread-safe handling of concurrent requests.

Stretch goals:
- Efficient with high concurrency without serializing all requests.
- Bounded memory usage.
- Do NOT cache unexpected transient errors.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


# ---------- Scaffolding ----------
@dataclass(frozen=True)
class Request:
    """A job submission. `idempotency_key` is supplied by the caller.

    Two Requests are "the same request" if they compare equal (all fields).
    """

    idempotency_key: str
    user_id: str
    amount_cents: int


@dataclass(frozen=True)
class Response:
    """Result of a successfully processed Request."""

    job_id: str
    user_id: str
    amount_cents: int


class BackendError(Exception):
    """Raised by the backend when processing a request fails."""


class KeyConflictError(Exception):
    """Raised by IdempotentHandler when an idempotency key is reused with a
    *different* Request than the one it was first seen with."""


class SimulatedBackend:
    """Stands in for the real system that performs the work.

    Contract:
      - `process()` either returns a Response or raises an Exception
        (typically BackendError). It may take a while, but it always finishes.
      - It is thread-safe and may be called concurrently from many threads.
      - Every call that actually executes produces a NEW unique `job_id`, so
        re-running a job is observable.
      - It has side effects. Calling it twice for one logical request is the
        bug that idempotency is meant to prevent.
    """

    def __init__(self, latency_seconds: float = 0.0):
        self.latency_seconds = latency_seconds
        self.failing_keys: set[str] = set()
        self.on_process: Callable[[Request], None] | None = None
        self._lock = threading.Lock()
        self._call_count = 0
        self._calls: list[str] = []

    @property
    def call_count(self) -> int:
        with self._lock:
            return self._call_count

    def calls_for(self, key: str) -> int:
        with self._lock:
            return self._calls.count(key)

    def process(self, request: Request) -> Response:
        with self._lock:
            self._call_count += 1
            job_number = self._call_count
            self._calls.append(request.idempotency_key)

        if self.on_process is not None:
            self.on_process(request)
        if self.latency_seconds:
            time.sleep(self.latency_seconds)

        if request.idempotency_key in self.failing_keys:
            raise BackendError(f"backend failed for key {request.idempotency_key!r}")

        return Response(
            job_id=f"job-{job_number}",
            user_id=request.user_id,
            amount_cents=request.amount_cents,
        )


# ---------- Implementation ----------
from concurrent.futures import Future


class TransientError(Exception):
    """Raised when requests fail due to unknown transient issues."""


@dataclass
class JobCache:
    """Stores the result of a job, along with the time it was completed."""

    request: Request | None = None
    fut: Future[Response] | None = None
    completed_at: float | None = None


class IdempotentHandler:
    """Idempotent front end for a SimulatedBackend."""

    def __init__(
        self,
        backend: SimulatedBackend,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._backend = backend
        self._ttl_seconds = ttl_seconds
        self._cleanup_period = ttl_seconds * 2
        self._clock = clock

        self._lock = threading.Lock()
        self._cache: dict[str, JobCache] = {}
        self._cleaner_thread = threading.Thread(target=self._cache_cleaner, daemon=True)

        self._cleaner_thread.start()

    def handle(self, request: Request) -> Response:
        is_owner: bool = False
        cache_entry: JobCache | None = None

        with self._lock:
            key = request.idempotency_key
            if key in self._cache:
                cache_entry = self._cache[key]
                if (
                    cache_entry.completed_at is not None
                    and self._clock() - cache_entry.completed_at > self._ttl_seconds
                ):
                    # Expired entry, refresh it
                    cache_entry = JobCache(request=request, fut=Future())
                    self._cache[key] = cache_entry
                    is_owner = True
                elif cache_entry.request != request:
                    raise KeyConflictError(
                        f"Idempotency key {key!r} reused with a different request"
                    )
            else:
                cache_entry = JobCache(request=request, fut=Future())
                self._cache[key] = cache_entry
                is_owner = True

        if is_owner:
            try:
                response = self._backend.process(request)
                cache_entry.fut.set_result(response)
                cache_entry.completed_at = self._clock()
            except BackendError as e:
                cache_entry.fut.set_exception(e)
                # Cache negative result too
                cache_entry.completed_at = self._clock()
            except BaseException:
                cache_entry.fut.set_exception(
                    TransientError("Transient error occurred, please retry.")
                )
                # Expire immediately
                cache_entry.completed_at = self._clock() - self._ttl_seconds - 100

        return cache_entry.fut.result()

    def _cache_cleaner(self) -> None:
        """Remove expired entries from the cache periodically."""
        while True:
            time.sleep(self._cleanup_period)
            now = self._clock()
            with self._lock:
                expired_keys = [
                    key
                    for key, cache in self._cache.items()
                    if cache.completed_at is not None
                    and now - cache.completed_at > self._ttl_seconds
                ]
                for key in expired_keys:
                    del self._cache[key]


# ---------- Testing ----------
import unittest

TTL = 10.0
TIMEOUT = 5.0


class FakeClock:
    """Manually advanced clock, so TTL tests never sleep."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Worker(threading.Thread):
    """Runs `fn` in a daemon thread and captures its result or exception."""

    def __init__(self, fn: Callable[[], Response]):
        super().__init__(daemon=True)
        self._fn = fn
        self.result: Response | None = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self._fn()
        except BaseException as exc:
            self.error = exc


def join_all(test: unittest.TestCase, workers: list) -> None:
    for w in workers:
        w.join(TIMEOUT)
        test.assertFalse(w.is_alive(), "worker did not finish (deadlock?)")


def make_request(
    key: str = "key-1", user_id: str = "user-1", amount: int = 100
) -> Request:
    return Request(idempotency_key=key, user_id=user_id, amount_cents=amount)


class TestIdempotentHandler(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.backend = SimulatedBackend()
        self.handler = IdempotentHandler(
            self.backend, ttl_seconds=TTL, clock=self.clock
        )

    def test_duplicate_within_ttl_returns_stored_response(self):
        req = make_request()
        first = self.handler.handle(req)
        self.clock.advance(TTL / 2)
        second = self.handler.handle(req)

        self.assertEqual(first, second)
        self.assertEqual(self.backend.call_count, 1)

    def test_duplicate_after_ttl_runs_backend_again(self):
        req = make_request()
        first = self.handler.handle(req)
        self.clock.advance(TTL + 1)
        second = self.handler.handle(req)

        self.assertEqual(self.backend.call_count, 2)
        self.assertNotEqual(first.job_id, second.job_id)

    def test_ttl_is_measured_from_completion_not_submission(self):
        self.backend.on_process = lambda req: self.clock.advance(8)
        req = make_request()
        first = self.handler.handle(req)

        self.clock.advance(5)
        second = self.handler.handle(req)

        self.assertEqual(first, second)
        self.assertEqual(self.backend.call_count, 1)

    def test_same_key_different_request_after_completion_conflicts(self):
        original = self.handler.handle(make_request(amount=100))

        with self.assertRaises(KeyConflictError):
            self.handler.handle(make_request(amount=999))

        self.assertEqual(self.handler.handle(make_request(amount=100)), original)
        self.assertEqual(self.backend.call_count, 1)

    def test_concurrent_duplicates_run_backend_once(self):
        n = 8
        entered = threading.Event()
        release = threading.Event()

        def gate(req: Request) -> None:
            entered.set()
            release.wait(TIMEOUT)

        self.backend.on_process = gate
        start = threading.Barrier(n)
        req = make_request()

        def call() -> Response:
            start.wait(TIMEOUT)
            return self.handler.handle(req)

        workers = [Worker(call) for _ in range(n)]
        for w in workers:
            w.start()

        self.assertTrue(entered.wait(TIMEOUT), "backend was never called")
        time.sleep(0.1)
        release.set()
        join_all(self, workers)

        for w in workers:
            self.assertIsNone(w.error)
        self.assertEqual(self.backend.call_count, 1)
        self.assertEqual(len({w.result for w in workers}), 1)

    def test_in_flight_conflict_raises_without_waiting(self):
        entered = threading.Event()
        release = threading.Event()

        def gate(req: Request) -> None:
            entered.set()
            release.wait(TIMEOUT)

        self.backend.on_process = gate
        first = Worker(lambda: self.handler.handle(make_request(amount=100)))
        first.start()
        self.assertTrue(entered.wait(TIMEOUT), "backend was never called")

        with self.assertRaises(KeyConflictError):
            self.handler.handle(make_request(amount=999))

        release.set()
        join_all(self, [first])
        self.assertIsNone(first.error)
        self.assertEqual(first.result.amount_cents, 100)
        self.assertEqual(self.backend.call_count, 1)

    def test_different_keys_are_processed_concurrently(self):
        barrier = threading.Barrier(2, timeout=2.0)
        self.backend.on_process = lambda req: barrier.wait()

        workers = [
            Worker(lambda: self.handler.handle(make_request(key="key-a"))),
            Worker(lambda: self.handler.handle(make_request(key="key-b"))),
        ]
        for w in workers:
            w.start()
        join_all(self, workers)

        for w in workers:
            self.assertIsNone(w.error, f"unexpected error: {w.error!r}")
        self.assertEqual(self.backend.call_count, 2)

    def test_failure_is_stored_until_ttl_expires(self):
        req = make_request()
        self.backend.failing_keys.add(req.idempotency_key)

        with self.assertRaises(BackendError):
            self.handler.handle(req)

        self.backend.failing_keys.clear()
        with self.assertRaises(BackendError):
            self.handler.handle(req)
        self.assertEqual(self.backend.call_count, 1)

        self.clock.advance(TTL + 1)
        response = self.handler.handle(req)
        self.assertEqual(self.backend.call_count, 2)
        self.assertEqual(response.amount_cents, req.amount_cents)


if __name__ == "__main__":
    unittest.main(verbosity=2)
