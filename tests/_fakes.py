"""Shared test doubles for the orchestration / agent layers.

Every fake below satisfies the relevant *protocol* (not a concrete class), so
it can stand in for ``AsrService`` / ``TtsService`` / ``AgentGraph`` / the
engines without pulling in real models, network, or microphones.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any

from jarvis.asr.types import AsrResultType, RecognitionResult
from jarvis.audio.format import DEFAULT_FORMAT, AudioFormat
from jarvis.llm.types import ChatMessage, ChatResponse, GenerationOptions, StreamChunk
from jarvis.tts.types import AudioChunk
from jarvis.wakeword.types import WakeHit


class FakeLlmClient:
    """An :class:`LlmClient` that returns a canned reply and records calls."""

    def __init__(self, reply: str = "ok") -> None:
        self._reply = reply
        self.calls: list[list[ChatMessage]] = []

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake"

    def complete(
        self, messages: Sequence[ChatMessage], *, options: GenerationOptions | None = None
    ) -> ChatResponse:
        self.calls.append(list(messages))
        return ChatResponse(content=self._reply, model="fake")

    def stream(
        self, messages: Sequence[ChatMessage], *, options: GenerationOptions | None = None
    ) -> Iterator[StreamChunk]:
        yield from ()

    def complete_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
        on_text: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ChatResponse:
        """The canned answer, delivered in the shape a streamed turn expects.

        One ``on_text`` call with the whole reply rather than a token dribble: what the
        chat loop is being tested on is that it forwards the callback and honours the
        stop flag, not that it can parse SSE. The real assembly is covered in
        ``test_llm_client.py`` against a scripted stream.
        """
        del should_stop
        response = self.complete(messages, options=options)
        if on_text is not None and response.content:
            on_text(response.content)
        return response


class ScriptedVadEngine:
    """A :class:`VoiceActivityDetector` returning a scripted score per frame."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = list(scores)
        self._i = 0

    @property
    def name(self) -> str:
        return "fake-vad"

    @property
    def frame_samples(self) -> int:
        return 512

    def process(self, frame: bytes) -> float:
        if self._i >= len(self._scores):
            return 0.0
        score = self._scores[self._i]
        self._i += 1
        return score

    def close(self) -> None:
        pass


class ScriptedWakeWordEngine:
    """A :class:`WakeWordEngine` returning a scripted hit per frame."""

    def __init__(self, hits: list[WakeHit]) -> None:
        self._hits = list(hits)
        self._i = 0

    @property
    def name(self) -> str:
        return "fake-ww"

    @property
    def frame_samples(self) -> int:
        return 512

    def process(self, frame: bytes) -> tuple[WakeHit, ...]:
        if self._i >= len(self._hits):
            return ()
        hit = self._hits[self._i]
        self._i += 1
        return (hit,)

    def close(self) -> None:
        pass


class FakeAsr:
    """Stands in for :class:`~jarvis.asr.service.AsrService.recognize`."""

    def __init__(self, text: str = "你好", *, running: bool = True) -> None:
        self._text = text
        self.running = running
        self.calls: list[tuple[bytes, object]] = []

    def recognize(
        self, audio: bytes, *, segment: Any = None, language: str | None = None
    ) -> RecognitionResult:
        self.calls.append((audio, segment))
        return RecognitionResult(text=self._text, type=AsrResultType.FINAL)


class FakeTts:
    """Stands in for :class:`~jarvis.tts.service.TtsService.synthesize`."""

    def __init__(self, chunks: list[AudioChunk] | None = None, *, running: bool = True) -> None:
        self._chunks = chunks or [AudioChunk(audio=b"x", sample_rate=16_000, is_final=True)]
        self.running = running
        self.calls: list[str] = []

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        should_stop: Any = None,
    ) -> Iterator[AudioChunk]:
        self.calls.append(text)
        for chunk in self._chunks:
            if should_stop is not None and should_stop():
                break
            yield chunk


class FakeGraph:
    """Stands in for :class:`~jarvis.orchestration.graph.AgentGraph`."""

    def __init__(self, reply: str = "好的", raises: Exception | None = None) -> None:
        self._reply = reply
        self._raises = raises
        self.calls: list[tuple[str, Any]] = []

    def run(self, user_text: str, history: list[ChatMessage] | None = None) -> str:
        self.calls.append((user_text, history))
        if self._raises is not None:
            raise self._raises
        return self._reply


class ScriptedAudioSource:
    """An :class:`~jarvis.audio.source.AudioSource` replaying scripted chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self._i = 0
        self.format: AudioFormat = DEFAULT_FORMAT

    def open(self) -> None:
        pass

    def read(self, samples: int) -> bytes:
        if self._i >= len(self._chunks):
            return b"\x00" * (samples * 2)
        chunk = self._chunks[self._i]
        self._i += 1
        return chunk

    def close(self) -> None:
        pass
