"""Tests for the audio capture primitives (format + frame assembly)."""

from __future__ import annotations

import pytest

from jarvis.audio import AudioFormat, FrameAssembler
from jarvis.audio.format import DEFAULT_FORMAT


class TestAudioFormat:
    def test_defaults_are_16k_mono_s16le(self) -> None:
        assert DEFAULT_FORMAT.sample_rate == 16_000
        assert DEFAULT_FORMAT.channels == 1
        assert DEFAULT_FORMAT.sample_width == 2

    def test_bytes_for_samples(self) -> None:
        assert DEFAULT_FORMAT.bytes_for_samples(1280) == 2560

    def test_duration_ms(self) -> None:
        assert DEFAULT_FORMAT.duration_ms(1280) == pytest.approx(80.0)
        assert DEFAULT_FORMAT.duration_ms(512) == pytest.approx(32.0)

    def test_rejects_stereo(self) -> None:
        with pytest.raises(ValueError, match="mono"):
            AudioFormat(channels=2)

    def test_rejects_non_16bit(self) -> None:
        with pytest.raises(ValueError, match="16-bit"):
            AudioFormat(sample_width=4)

    def test_rejects_bad_rate(self) -> None:
        with pytest.raises(ValueError, match="sample_rate"):
            AudioFormat(sample_rate=0)


class TestFrameAssembler:
    def test_exact_frame_passes_through(self) -> None:
        asm = FrameAssembler(4)
        assert asm.push(b"abcd") == [b"abcd"]
        assert asm.pending_bytes == 0

    def test_small_chunks_accumulate(self) -> None:
        asm = FrameAssembler(4)
        assert asm.push(b"ab") == []
        assert asm.push(b"c") == []
        assert asm.push(b"defgh") == [b"abcd", b"efgh"]
        assert asm.pending_bytes == 0

    def test_large_chunk_yields_multiple_frames_keeps_remainder(self) -> None:
        asm = FrameAssembler(4)
        frames = asm.push(b"0123456789")
        assert frames == [b"0123", b"4567"]
        assert asm.pending_bytes == 2

    def test_no_bytes_lost_across_random_split(self) -> None:
        payload = bytes(range(256)) * 10
        asm = FrameAssembler(64)
        out = bytearray()
        # Feed in awkward chunk sizes: 1, 3, 7, 13, ...
        position = 0
        step = 1
        while position < len(payload):
            for frame in asm.push(payload[position : position + step]):
                out.extend(frame)
            position += step
            step = (step * 2 + 1) % 97 + 1
        # Everything except the (possibly incomplete) tail must round-trip.
        assert bytes(out) == payload[: len(out)]
        assert len(out) + asm.pending_bytes == len(payload)

    def test_reset_drops_partial(self) -> None:
        asm = FrameAssembler(4)
        asm.push(b"ab")
        asm.reset()
        assert asm.pending_bytes == 0
        assert asm.push(b"cd") == []  # old bytes must not leak in

    def test_rejects_nonpositive_frame(self) -> None:
        with pytest.raises(ValueError, match="frame_bytes"):
            FrameAssembler(0)
