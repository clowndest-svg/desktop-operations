"""Fixed-size frame assembly for engines with strict input length."""

from __future__ import annotations


class FrameAssembler:
    """Regroup arbitrary-size PCM chunks into fixed-size frames.

    Capture devices deliver whatever block size the driver prefers, while
    wake-word engines demand an exact frame length (OpenWakeWord: 1280
    samples / 80 ms; Porcupine: 512 samples / 32 ms). This class buffers
    incoming bytes and yields complete frames, keeping any remainder for
    the next push — no samples are ever dropped or duplicated.

    Not thread-safe by design: each consumer owns its assembler on the
    audio thread.
    """

    def __init__(self, frame_bytes: int) -> None:
        if frame_bytes <= 0:
            raise ValueError(f"frame_bytes must be positive, got {frame_bytes}")
        self._frame_bytes = frame_bytes
        self._buffer = bytearray()

    @property
    def frame_bytes(self) -> int:
        """Exact size in bytes of every produced frame."""
        return self._frame_bytes

    @property
    def pending_bytes(self) -> int:
        """Bytes currently buffered, waiting to complete a frame."""
        return len(self._buffer)

    def push(self, chunk: bytes) -> list[bytes]:
        """Append ``chunk`` and return every complete frame now available."""
        self._buffer.extend(chunk)
        frames: list[bytes] = []
        size = self._frame_bytes
        while len(self._buffer) >= size:
            frames.append(bytes(self._buffer[:size]))
            del self._buffer[:size]
        return frames

    def reset(self) -> None:
        """Drop any buffered remainder (e.g. when the stream restarts)."""
        self._buffer.clear()
