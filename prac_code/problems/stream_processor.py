#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///

"""
An event processor consuming an async stream of data chunks (in bytes) and yields complete events as they become available.

Wire format for each event:

+-----------------------+---------------------------+
| 4 bytes               | N bytes                   |
| big-endian unsigned N | UTF-8 encoded JSON object |
+-----------------------+---------------------------+

Core requirements:
- Yield events in order as soon as they complete.
- Handle arbitrary chunking; a chunk may contain part of a frame, multiple frames, or a complete frame and a part of the next frame.
- Reject frames that exceed the max_frame_size with `FrameTooLarge` error.
- If the source finishes in the middle of a frame, raise `IncompleteStreamError`.

Stretch goals:
- Handle malformed JSON and idle timeout.
- Memory efficiency on large chunks/frames.
"""

import asyncio
import json
import struct
from collections.abc import AsyncIterator, Iterable


# ---------- Scaffolding ----------
class FrameError(Exception):
    """Base class for framing errors."""


class FrameTooLarge(FrameError):
    """A frame header declared a payload larger than max_frame_size."""


class IncompleteStreamError(FrameError):
    """The source ended in the middle of a frame."""


# Frame header: a 4-byte big-endian unsigned integer holding the payload length.
HEADER = struct.Struct(">I")
HEADER_SIZE = HEADER.size  # 4


# ---------- Implementation ----------
class StreamProcessor:
    def __init__(self, max_frame_size: int = 1_048_576):
        self.max_frame_size = max_frame_size

    async def events(self, source: AsyncIterator[bytes]) -> AsyncIterator[dict]:
        raise NotImplementedError
        yield {}


# ---------- Testing ----------
import unittest


def encode(event: dict) -> bytes:
    """Encode one event as a length-prefixed frame."""
    body = json.dumps(event).encode("utf-8")
    return HEADER.pack(len(body)) + body


async def source_from(chunks: Iterable[bytes]) -> AsyncIterator[bytes]:
    """Turn a list of chunks into an async source, yielding to the event loop
    between chunks like a real network source would."""
    for chunk in chunks:
        yield chunk
        await asyncio.sleep(0)


def split_every(data: bytes, size: int) -> list[bytes]:
    """Split data into chunks of at most `size` bytes."""
    return [data[i : i + size] for i in range(0, len(data), size)]


async def collect(processor: StreamProcessor, chunks: Iterable[bytes]) -> list[dict]:
    return [ev async for ev in processor.events(source_from(chunks))]


class StreamEventProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_frame_in_one_chunk(self):
        out = await collect(StreamProcessor(), [encode({"id": 1})])
        self.assertEqual(out, [{"id": 1}])

    async def test_multiple_frames_in_one_chunk(self):
        events = [{"id": 1}, {"id": 2}, {"id": 3}]
        data = b"".join(encode(e) for e in events)
        out = await collect(StreamProcessor(), [data])
        self.assertEqual(out, events)

    async def test_byte_at_a_time(self):
        events = [{"id": 1, "msg": "hello"}, {"id": 2, "msg": "wörld ✓"}, {}]
        data = b"".join(encode(e) for e in events)
        out = await collect(StreamProcessor(), split_every(data, 1))
        self.assertEqual(out, events)

    async def test_header_split_across_chunks(self):
        frame = encode({"id": 1})
        chunks = [frame[:2], frame[2:4], frame[4:]]  # header cut in half
        out = await collect(StreamProcessor(), chunks)
        self.assertEqual(out, [{"id": 1}])

    async def test_empty_chunks_and_empty_stream(self):
        self.assertEqual(await collect(StreamProcessor(), []), [])

        frame = encode({"id": 1})
        chunks = [b"", frame[:3], b"", b"", frame[3:], b""]
        out = await collect(StreamProcessor(), chunks)
        self.assertEqual(out, [{"id": 1}])

    async def test_events_emitted_before_source_finishes(self):
        first_seen = asyncio.Event()

        async def source():
            yield encode({"id": 1})
            await first_seen.wait()
            yield encode({"id": 2})

        async def consume():
            out = []
            async for ev in StreamProcessor().events(source()):
                out.append(ev)
                first_seen.set()
            return out

        out = await asyncio.wait_for(consume(), timeout=2)
        self.assertEqual(out, [{"id": 1}, {"id": 2}])

    async def test_truncated_stream_raises(self):
        frame = encode({"id": 2, "payload": "x" * 20})
        complete = encode({"id": 1})

        for name, tail in [("mid-payload", frame[:-5]), ("mid-header", frame[:2])]:
            with self.subTest(name):
                received = []
                with self.assertRaises(IncompleteStreamError):
                    async for ev in StreamProcessor().events(
                        source_from([complete + tail])
                    ):
                        received.append(ev)
                self.assertEqual(received, [{"id": 1}])

    async def test_oversized_frame_rejected_from_header_alone(self):
        async def source():
            yield HEADER.pack(10_000)
            await asyncio.Event().wait()

        async def consume():
            return [
                ev async for ev in StreamProcessor(max_frame_size=1024).events(source())
            ]

        with self.assertRaises(FrameTooLarge):
            await asyncio.wait_for(consume(), timeout=2)


if __name__ == "__main__":
    unittest.main()
