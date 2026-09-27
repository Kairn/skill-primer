#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///

"""
An implementation of a batch processing client. Requests are much more efficiently processed in batches.

Core requirements:
- Simple request/response interface. Batching is implicit and transparent to the user.
- Batching is best effort subject to a max size and a max wait time.

Stretch goals:
- Respect backend and cap the number of inflight batches.
- Support graceful shutdown, finish all pending requests before shutdown is called.
- Retry failed requests automatically if retryable.
"""

import asyncio
import random
import time
from dataclasses import dataclass

# ---------- Scaffolding ----------
MAX_BATCH = 64
BATCH_OVERHEAD_MS = 50
REQUEST_OVERHEAD_MS = 1


class RetryableError(Exception):
    """An exception indicating that the request is retryable."""


class ExpensiveService:
    """
    A simulated expensive backend service the client calls to process each request.

    - One interface for processing a batch of requests (texts), returning one vector for each in the same order.
    - Fixed overhead per batch (amortized) + fixed processing time per item.
    - Rejects batches that exceed a certain size with a hard error.
    - May raise RetryableError for transient failures (randomly).
    """

    def __init__(self, fail_rate: float = 0.0, seed: int = 0):
        self.fail_rate = fail_rate
        self.rng = random.Random(seed)
        self.calls = 0
        self.batch_sizes: list[int] = []
        self.concurrent = 0
        self.max_concurrent = 0

    async def serve_batch(self, texts: list[str]) -> list[list[float]]:
        if len(texts) > MAX_BATCH:
            raise ValueError(f"Batch too large: {len(texts)} > {MAX_BATCH}")
        if not texts:
            raise ValueError("Empty batch")

        self.calls += 1
        self.batch_sizes.append(len(texts))
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)

        try:
            await asyncio.sleep(
                (BATCH_OVERHEAD_MS + REQUEST_OVERHEAD_MS * len(texts)) / 1000
            )
            if self.rng.random() < self.fail_rate:
                raise RetryableError("503 transient failure")
            return [[float(len(t)), float(i)] for i, t in enumerate(texts)]
        finally:
            self.concurrent -= 1


@dataclass
class ClientConfig:
    max_batch: int = MAX_BATCH
    max_wait_ms: float = 10.0
    max_inflight: int = 8
    max_attempts: int = 3
    base_backoff_ms: float = 20.0


# ---------- Implementation ----------
_SHUTDOWN = object()  # Shutdown sentinel


class BatchProcessClient:
    def __init__(self, service: ExpensiveService, config: ClientConfig):
        self.service = service
        self.config = config
        self.max_batch = min(config.max_batch, MAX_BATCH)
        self.queue = asyncio.Queue()
        self.sem = asyncio.Semaphore(config.max_inflight)
        self.closed = False

        self._rng = random.Random()
        self._inflight: set[asyncio.Task] = set()
        self._batcher_daemon = asyncio.create_task(self._run_batcher())

    async def process(self, text: str) -> list[float]:
        if self.closed:
            raise RuntimeError("Cannot process after shutdown")
        fut = asyncio.get_running_loop().create_future()
        await self.queue.put(
            (text, fut)
        )  # Each enqueued item is the text and its future
        return await fut

    async def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.queue.put(_SHUTDOWN)
        await self._batcher_daemon
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)

    async def _run_batcher(self) -> None:
        while True:
            next = await self.queue.get()  # Blocks if empty
            if next is _SHUTDOWN:
                return

            batch = [next]
            shutdown = False
            deadline = time.monotonic() + self.config.max_wait_ms / 1000
            while len(batch) < self.max_batch and time.monotonic() < deadline:
                try:
                    next = await asyncio.wait_for(
                        self.queue.get(), timeout=deadline - time.monotonic()
                    )
                except TimeoutError:
                    break
                if next is _SHUTDOWN:
                    shutdown = True
                    break
                batch.append(next)

            self._spawn_batch_task(batch)
            if shutdown:
                return

    def _spawn_batch_task(self, reqs: list[tuple[str, asyncio.Future]]) -> None:
        task = asyncio.create_task(self._submit_batch(reqs))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _submit_batch(self, reqs: list[tuple[str, asyncio.Future]]) -> None:
        texts: list[str] = [t for t, _ in reqs]
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                async with self.sem:
                    results = await self.service.serve_batch(texts)
            except RetryableError as e:
                if attempt >= self.config.max_attempts:
                    # Give up due to exhausting retries
                    for _, fut in reqs:
                        if not fut.done():
                            fut.set_exception(e)
                    return

                # Retry with backoff and jitter
                max_sleep = 2 ** (attempt - 1) * self.config.base_backoff_ms / 1000
                await asyncio.sleep(max_sleep * self._rng.random())
            except Exception as e:
                # Non-retryable error, give up
                for _, fut in reqs:
                    if not fut.done():
                        fut.set_exception(e)
                return
            else:
                # Succeeded, set future values
                for (_, fut), res in zip(reqs, results):
                    if not fut.done():
                        fut.set_result(res)
                return


# ---------- Testing ----------
TEST_TIMEOUT = 30


async def t_basic_correctness():
    es = ExpensiveService()
    c = BatchProcessClient(es, ClientConfig(max_batch=8, max_wait_ms=10))
    texts = [f"text-{i}" * (i + 1) for i in range(50)]
    got = await asyncio.gather(*(c.process(t) for t in texts))
    await c.shutdown()
    for t, v in zip(texts, got):
        assert v == [float(len(t)), v[1]], f"Wrong vector routed to {t!r}: {v}"
    assert es.calls > 1, "Expected multiple batches"
    print(f"  OK: 50 texts, {es.calls} backend calls, sizes={es.batch_sizes}")


async def t_batching_actually_happens():
    es = ExpensiveService()
    c = BatchProcessClient(es, ClientConfig(max_batch=32, max_wait_ms=20))
    await asyncio.gather(*(c.process(f"t{i}") for i in range(64)))
    await c.shutdown()
    assert es.calls <= 4, (
        f"Expected <=4 batched calls, got {es.calls} ({es.batch_sizes})"
    )
    print(f"  OK: 64 texts coalesced into {es.calls} calls {es.batch_sizes}")


async def t_max_wait_respected():
    es = ExpensiveService()
    c = BatchProcessClient(es, ClientConfig(max_batch=64, max_wait_ms=15))
    t0 = time.perf_counter()
    await c.process("lonely")
    dt = (time.perf_counter() - t0) * 1000
    await c.shutdown()
    assert dt < 120, f"Single request took {dt:.0f} ms, expected ~65-80"
    print(f"  OK: single request returned in {dt:.0f} ms")


async def t_inflight_cap():
    es = ExpensiveService()
    c = BatchProcessClient(es, ClientConfig(max_batch=4, max_wait_ms=5, max_inflight=3))
    await asyncio.gather(*(c.process(f"t{i}") for i in range(200)))
    await c.shutdown()
    assert es.max_concurrent <= 3, f"max_inflight violated: {es.max_concurrent} > 3"
    print(f"  OK: peak concurrency {es.max_concurrent} <= 3")


async def t_batch_size_cap():
    es = ExpensiveService()
    c = BatchProcessClient(es, ClientConfig(max_batch=64, max_wait_ms=50))
    await asyncio.gather(*(c.process(f"t{i}") for i in range(500)))
    await c.shutdown()
    assert max(es.batch_sizes) <= 64, f"Oversized batch {max(es.batch_sizes)}"
    print(f"  OK: max batch size {max(es.batch_sizes)}")


async def t_retry_succeeds():
    es = ExpensiveService(fail_rate=0.5, seed=7)
    c = BatchProcessClient(
        es, ClientConfig(max_batch=8, max_wait_ms=10, max_attempts=8)
    )
    got = await asyncio.gather(*(c.process(f"t{i}") for i in range(40)))
    await c.shutdown()
    assert all(v is not None for v in got)
    print(f"  OK: survived 50% failure rate, {es.calls} total backend calls")


async def t_exhausted_retries_propagate():
    es = ExpensiveService(fail_rate=1.0)
    c = BatchProcessClient(
        es, ClientConfig(max_batch=4, max_wait_ms=5, max_attempts=2, base_backoff_ms=1)
    )
    results = await asyncio.gather(
        *(c.process(f"t{i}") for i in range(8)), return_exceptions=True
    )
    await c.shutdown()
    assert all(isinstance(r, RetryableError) for r in results), (
        f"Expected every caller to see RetryableError, got {results}"
    )
    print("  OK: all 8 callers got RetryableError after retries exhausted")


async def t_shutdown_drains_and_rejects():
    es = ExpensiveService()
    c = BatchProcessClient(es, ClientConfig(max_batch=64, max_wait_ms=50))
    pending = [asyncio.create_task(c.process(f"t{i}")) for i in range(20)]
    await asyncio.sleep(0.005)
    await c.shutdown()
    done = await asyncio.gather(*pending, return_exceptions=True)
    assert all(not isinstance(d, Exception) for d in done), (
        f"Shutdown dropped accepted work: {done}"
    )
    try:
        await asyncio.wait_for(c.process("after shutdown"), timeout=0.5)
    except TimeoutError:
        raise AssertionError("Process after shutdown hung instead of failing fast")
    except Exception:
        pass
    else:
        raise AssertionError("Process after shutdown should have raised")
    print("  OK: drained 20 accepted items and rejects new ones")


TESTS = [
    t_basic_correctness,
    t_batching_actually_happens,
    t_max_wait_respected,
    t_inflight_cap,
    t_batch_size_cap,
    t_retry_succeeds,
    t_exhausted_retries_propagate,
    t_shutdown_drains_and_rejects,
]


async def run_tests():
    failed = 0
    for t in TESTS:
        print(f"{t.__name__}:")
        try:
            await asyncio.wait_for(t(), timeout=TEST_TIMEOUT)
        except Exception as e:
            failed += 1
            print(f"  FAIL: {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")


if __name__ == "__main__":
    asyncio.run(run_tests())
