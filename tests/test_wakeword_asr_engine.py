"""Tests for the ASR wake-word engine: keyword matching over utterance transcripts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis.core.exceptions import AsrError
from jarvis.wakeword.asr_engine import AsrWakeWordEngine, normalize_for_match

FRAME_BYTES = 512 * 2  # the engine's declared frame_samples, s16le


def make_event(start: int, end: int) -> SimpleNamespace:
    """A stand-in for the SPEECH_END VadEvent the real segmenter emits."""
    return SimpleNamespace(segment=SimpleNamespace(start_sample=start, end_sample=end))


class FakeSegmenter:
    """Emits scripted events on scripted feed() call numbers."""

    def __init__(self, events_by_call: dict[int, list[SimpleNamespace]] | None = None) -> None:
        self.events_by_call = events_by_call or {}
        self.calls = 0
        self.resets = 0
        self.chunks: list[bytes] = []

    def feed(self, chunk: bytes) -> list[SimpleNamespace]:
        self.chunks.append(chunk)
        self.calls += 1
        return list(self.events_by_call.get(self.calls, []))

    def flush(self) -> list[SimpleNamespace]:
        return []

    def reset(self) -> None:
        self.resets += 1


class FakeTranscriber:
    """Returns queued texts, recording the audio it was asked about."""

    def __init__(self, texts: list[str], *, fail_on_call: int | None = None) -> None:
        self.texts = list(texts)
        self.fail_on_call = fail_on_call
        self.calls = 0
        self.audio: list[bytes] = []

    def recognize(
        self, audio: bytes, *, segment: object = None, language: object = None
    ) -> SimpleNamespace:
        self.calls += 1
        text = self.texts.pop(0) if self.texts else ""
        if self.fail_on_call == self.calls:
            raise AsrError("recognition blew up")
        self.audio.append(audio)
        return SimpleNamespace(text=text)


def build(
    keywords: list[str], texts: list[str], events_by_call: dict[int, list[SimpleNamespace]]
) -> tuple[AsrWakeWordEngine, FakeTranscriber, FakeSegmenter]:
    """Wire an engine over fakes that emit one utterance boundary."""
    transcriber = FakeTranscriber(texts)
    segmenter = FakeSegmenter(events_by_call)
    engine = AsrWakeWordEngine(keywords=keywords, transcriber=transcriber, segmenter=segmenter)
    return engine, transcriber, segmenter


def feed_frames(engine: AsrWakeWordEngine, count: int) -> None:
    for index in range(count):
        engine.process(bytes([index % 200]) * FRAME_BYTES)


class TestNormalize:
    def test_collapses_spaces_and_trailing_punctuation(self) -> None:
        assert normalize_for_match("贾 维 斯，") == "贾维斯"
        assert normalize_for_match("  hey  jarvis. ") == "heyjarvis"

    def test_keeps_inner_word_boundaries_together(self) -> None:
        assert normalize_for_match("贾维斯, 帮我看下订单") == "贾维斯帮我看下订单"


class TestWakeDetection:
    def test_keyword_in_transcript_fires_once(self) -> None:
        engine, transcriber, _ = build(
            ["贾维斯"], ["贾维斯帮我看下订单"], {4: [make_event(0, 1536)]}
        )

        feed_frames(engine, 2)
        assert engine.process(b"\x00" * FRAME_BYTES) == ()  # no boundary yet
        hits = engine.process(b"\x01" * FRAME_BYTES)  # call 4 closes the utterance

        assert [hit.keyword for hit in hits] == ["贾维斯"]
        assert [hit.score for hit in hits] == [1.0]
        assert transcriber.calls == 1

    def test_transcribed_audio_is_exactly_the_utterance_span(self) -> None:
        engine, transcriber, _ = build(["贾维斯"], ["贾维斯"], {4: [make_event(512, 1536)]})

        for index in range(3):
            engine.process(bytes([index]) * FRAME_BYTES)
        engine.process(b"\x09" * FRAME_BYTES)

        # samples 512..1536 == frames 1 and 2, i.e. byte tags 0x01 then 0x02
        assert transcriber.audio[0] == b"\x01" * FRAME_BYTES + b"\x02" * FRAME_BYTES

    def test_unrelated_transcript_does_not_wake(self) -> None:
        engine, transcriber, segmenter = build(
            ["贾维斯"], ["今天天气不错"], {2: [make_event(0, 1024)]}
        )

        feed_frames(engine, 1)
        assert engine.process(b"\x00" * FRAME_BYTES) == ()
        assert transcriber.calls == 1
        assert segmenter.resets == 0

    def test_spacing_and_punctuation_in_text_still_match(self) -> None:
        engine, _, _ = build(["贾维斯"], ["贾 维 斯，在吗？"], {1: [make_event(0, 512)]})

        hits = engine.process(b"\x00" * FRAME_BYTES)

        assert len(hits) == 1

    def test_model_style_keyword_matches_spoken_text(self) -> None:
        """The shipped default ``hey_jarvis`` must still wake via transcript."""
        engine, _, _ = build(["hey_jarvis"], ["Hey Jarvis!"], {1: [make_event(0, 512)]})

        hits = engine.process(b"\x00" * FRAME_BYTES)

        assert len(hits) == 1
        assert hits[0].keyword == "hey_jarvis"

    def test_keyword_with_a_space_matches_run_together_text(self) -> None:
        engine, _, _ = build(["hey jarvis"], ["heyjarvis"], {1: [make_event(0, 512)]})

        assert len(engine.process(b"\x00" * FRAME_BYTES)) == 1


class TestStateAfterWake:
    def test_wake_resets_segmenter_and_audio_window(self) -> None:
        engine, _, segmenter = build(["贾维斯"], ["贾维斯"], {1: [make_event(0, 512)]})

        engine.process(b"\x00" * FRAME_BYTES)

        assert segmenter.resets == 1

    def test_second_utterance_is_not_matched_against_stale_audio(self) -> None:
        events = {2: [make_event(0, 512)], 4: [make_event(512, 1024)]}
        engine, transcriber, _ = build(["贾维斯"], ["贾维斯", "随便说点什么"], events)

        engine.process(b"\x00" * FRAME_BYTES)  # call 1: silence
        engine.process(b"\x01" * FRAME_BYTES)  # call 2: wake
        engine.process(b"\x02" * FRAME_BYTES)  # call 3: silence, new window starts
        assert engine.process(b"\x03" * FRAME_BYTES) == ()  # call 4: no keyword

        # The segmenter restarted with the window, so its sample numbering is
        # relative to after the wake: frames from before it can never be
        # re-transcribed and re-trigger.
        assert transcriber.audio[0] == b"\x00" * FRAME_BYTES
        assert transcriber.audio[1] == b"\x03" * FRAME_BYTES


class TestFailureHandling:
    def test_recognition_error_is_survivable(self) -> None:
        transcriber = FakeTranscriber(["贾维斯"], fail_on_call=1)
        segmenter = FakeSegmenter({1: [make_event(0, 512)], 2: [make_event(512, 1024)]})
        engine = AsrWakeWordEngine(
            keywords=["贾维斯"], transcriber=transcriber, segmenter=segmenter
        )

        assert engine.process(b"\x00" * FRAME_BYTES) == ()  # ASR blew up, no crash
        assert engine.process(b"\x01" * FRAME_BYTES) == ()  # still listening afterwards
        assert transcriber.calls == 2

    def test_event_without_segment_is_ignored(self) -> None:
        engine, transcriber, _ = build(["贾维斯"], ["贾维斯"], {1: [SimpleNamespace(segment=None)]})

        assert engine.process(b"\x00" * FRAME_BYTES) == ()
        assert transcriber.calls == 0


class TestCommandChannel:
    """The wake phrase and the command can share one breath."""

    def test_text_after_the_keyword_rides_along_on_the_hit(self) -> None:
        engine, _, _ = build(["贾维斯"], ["贾维斯帮我看下今天的订单"], {1: [make_event(0, 512)]})

        hits = engine.process(b"\x00" * FRAME_BYTES)

        assert hits[0].command == "帮我看下今天的订单"

    def test_keyword_with_leading_punctuation_still_splits(self) -> None:
        engine, _, _ = build(
            ["hey_jarvis"], ["Hey Jarvis, what is the weather"], {1: [make_event(0, 512)]}
        )

        hits = engine.process(b"\x00" * FRAME_BYTES)

        assert hits[0].keyword == "hey_jarvis"
        assert hits[0].command == "what is the weather"

    def test_bare_wake_word_carries_no_command(self) -> None:
        engine, _, _ = build(["贾维斯"], ["贾维斯"], {1: [make_event(0, 512)]})

        hits = engine.process(b"\x00" * FRAME_BYTES)

        assert hits[0].keyword == "贾维斯"
        assert hits[0].command == ""


class TestConstruction:
    def test_empty_keywords_rejected(self) -> None:
        with pytest.raises(AsrError, match="non-empty keyword"):
            AsrWakeWordEngine(
                keywords=["  ", ""],
                transcriber=FakeTranscriber([]),
                segmenter=FakeSegmenter(),
            )

    def test_satisfies_the_wake_engine_protocol(self) -> None:
        engine = AsrWakeWordEngine(
            keywords=["贾维斯"], transcriber=FakeTranscriber([]), segmenter=FakeSegmenter()
        )

        assert engine.name == "asr"
        assert engine.frame_samples == 512
        engine.close()
        assert engine.process(b"\x00" * FRAME_BYTES) == ()
