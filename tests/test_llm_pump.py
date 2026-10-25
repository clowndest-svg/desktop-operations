"""Stream cancellation must release producer-owned resources on the producer thread."""

from __future__ import annotations

import threading
from collections.abc import Generator

from jarvis.llm.openai_compat import _ChunkPump
from jarvis.llm.types import StreamChunk


def _drain_worker(pump: _ChunkPump) -> None:
    """Also clean up the pre-fix implementation when a regression assertion fails."""
    for _ in range(8):
        if not pump._thread.is_alive():
            break
        pump.take(0.1)
    pump._thread.join(timeout=1)


def test_cancel_while_reading_releases_resource_on_producer() -> None:
    resource = threading.RLock()
    entered, unblock, finished = threading.Event(), threading.Event(), threading.Event()
    closed_by: list[int] = []

    def source() -> Generator[StreamChunk, None, None]:
        try:
            with resource:
                entered.set()
                assert unblock.wait(5)
                yield StreamChunk(text="one")
                yield StreamChunk(text="two")
                yield StreamChunk(text="three")
        finally:
            closed_by.append(threading.get_ident())
            finished.set()

    pump = _ChunkPump(source())
    try:
        assert entered.wait(1)
        pump.close()
        unblock.set()
        assert finished.wait(1), "cancelled pump kept the generator/resource lock alive"
        assert closed_by == [pump._thread.ident]
        assert resource.acquire(timeout=0.1)
        resource.release()
    finally:
        unblock.set()
        _drain_worker(pump)


def test_cancel_with_full_queue_never_closes_source_on_consumer() -> None:
    second, finished = threading.Event(), threading.Event()
    closed_by: list[int] = []

    def source() -> Generator[StreamChunk, None, None]:
        try:
            yield StreamChunk(text="one")
            second.set()
            yield StreamChunk(text="two")
            yield StreamChunk(text="three")
        finally:
            closed_by.append(threading.get_ident())
            finished.set()

    pump = _ChunkPump(source())
    try:
        assert second.wait(1)
        pump.close()
        assert finished.wait(1)
        pump._thread.join(timeout=1)
        assert not pump._thread.is_alive()
        assert closed_by == [pump._thread.ident]
    finally:
        _drain_worker(pump)


def test_pump_preserves_chunks_and_terminal_error() -> None:
    failure = ValueError("broken stream")

    def source() -> Generator[StreamChunk, None, None]:
        yield StreamChunk(text="first")
        raise failure

    pump = _ChunkPump(source())
    try:
        kind, value = pump.take(1)
        assert kind == "chunk" and value == StreamChunk(text="first")
        assert pump.take(1) == ("error", failure)
    finally:
        pump.close()
        _drain_worker(pump)


def test_pump_preserves_normal_exhaustion() -> None:
    def source() -> Generator[StreamChunk, None, None]:
        yield StreamChunk(text="first")

    pump = _ChunkPump(source())
    try:
        assert pump.take(1) == ("chunk", StreamChunk(text="first"))
        assert pump.take(1) == ("done", None)
    finally:
        pump.close()
        _drain_worker(pump)
