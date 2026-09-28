"""Tests for TTS types (AudioChunk + protocol helpers)."""

from __future__ import annotations

from jarvis.tts.types import (
    FORMAT_MP3,
    FORMAT_PCM_S16LE,
    AudioChunk,
)


def test_audio_chunk_defaults() -> None:
    chunk = AudioChunk(audio=b"\x00\x01", sample_rate=16_000)
    assert chunk.audio == b"\x00\x01"
    assert chunk.sample_rate == 16_000
    assert chunk.is_final is False
    assert chunk.format == FORMAT_PCM_S16LE


def test_audio_chunk_final_mp3() -> None:
    chunk = AudioChunk(
        audio=b"mp3-bytes",
        sample_rate=24_000,
        is_final=True,
        format=FORMAT_MP3,
    )
    assert chunk.is_final is True
    assert chunk.format == FORMAT_MP3
    assert chunk.sample_rate == 24_000


def test_formats_are_distinct_tags() -> None:
    assert FORMAT_PCM_S16LE != FORMAT_MP3
    assert FORMAT_PCM_S16LE == "pcm_s16le"
    assert FORMAT_MP3 == "mp3"
