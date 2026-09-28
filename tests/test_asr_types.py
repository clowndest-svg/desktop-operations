"""Tests for ASR types: result flags and the VAD -> ASR audio slicer."""

from __future__ import annotations

from jarvis.asr.types import AsrResultType, RecognitionResult, slice_segment_audio
from jarvis.vad.types import SpeechSegment


def _segment(start: int, end: int, sample_rate: int = 16_000) -> SpeechSegment:
    return SpeechSegment(
        start_sample=start,
        end_sample=end,
        start_time=start / sample_rate,
        end_time=end / sample_rate,
        sample_rate=sample_rate,
    )


def test_final_result_is_final() -> None:
    result = RecognitionResult(text="hello", type=AsrResultType.FINAL)
    assert result.is_final
    assert result.type is AsrResultType.FINAL


def test_partial_result_is_not_final() -> None:
    result = RecognitionResult(text="hell", type=AsrResultType.PARTIAL)
    assert not result.is_final
    assert result.type is AsrResultType.PARTIAL


def test_slice_basic_range() -> None:
    # 100..200 samples at 2 bytes/sample -> bytes 200..400.
    audio = bytes(range(256)) + bytes(range(256)) + bytes(range(88))  # 600 bytes
    clipped = slice_segment_audio(audio, _segment(100, 200))
    assert clipped == audio[200:400]
    assert len(clipped) == 200


def test_slice_clamps_overflow_end() -> None:
    # Segment end (250 samples -> byte 500) exceeds the 300-byte buffer.
    audio = bytes(300)
    clipped = slice_segment_audio(audio, _segment(100, 250))
    assert clipped == audio[200:300]
    assert len(clipped) == 100


def test_slice_clamps_negative_start() -> None:
    audio = bytes(400)
    clipped = slice_segment_audio(audio, _segment(-50, 100))
    assert clipped == audio[0:200]


def test_slice_reversed_range_returns_empty() -> None:
    audio = bytes(400)
    assert slice_segment_audio(audio, _segment(200, 100)) == b""


def test_slice_respects_sample_width() -> None:
    # 1-byte samples (hypothetical 8-bit): 10..20 -> 10..20 bytes.
    audio = bytes(range(50))
    clipped = slice_segment_audio(audio, _segment(10, 20), sample_width=1)
    assert clipped == audio[10:20]


def test_recognition_result_carries_segment_and_language() -> None:
    seg = _segment(10, 20)
    result = RecognitionResult(
        text="hi",
        type=AsrResultType.FINAL,
        language="zh",
        confidence=0.9,
        segment=seg,
    )
    assert result.language == "zh"
    assert result.confidence == 0.9
    assert result.segment is seg
