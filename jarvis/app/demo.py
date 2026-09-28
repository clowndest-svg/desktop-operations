"""File-driven voice-chain demo: a WAV in, a transcript and a spoken reply out.

The assistant itself listens on the microphone, which makes the chain
impossible to verify deterministically or record in one take. This module drives
the same engines from a WAV file instead — no mic, no wake word, no capture
threads — so the pipeline can be smoke-tested and demonstrated end to end.

Only the stages that need hardware are bypassed; VAD, ASR, LLM and TTS all run
for real, on the configured engines.
"""

from __future__ import annotations

import dataclasses
import logging
import wave
from dataclasses import dataclass
from pathlib import Path

from jarvis.asr import AsrService, AsrSettings
from jarvis.config import AppConfig
from jarvis.core.exceptions import AudioError, LlmError
from jarvis.llm import ChatMessage, LlmService
from jarvis.tts import TtsService, TtsSettings
from jarvis.vad import SileroVadEngine, SpeechSegment, VoiceActivitySegmenter

logger = logging.getLogger("jarvis.app.demo")

DEMO_SAMPLE_RATE = 16_000
"""The pipeline-wide capture format the VAD/ASR stages expect."""

DEMO_SYSTEM_PROMPT = (
    "你是 JARVIS，一个语音助手。用不超过 40 个汉字回答，只说一句结论加一句做法。"
    "这是要朗读出来的文本：禁止列表、换行、引号、表情和 Markdown 标记。"
)
"""Length-capped on purpose: 40 characters is ~8 spoken seconds, and every
Markdown mark the model emits turns into an audible pause on the way out."""


@dataclass(frozen=True, slots=True)
class DemoResult:
    """What a demo run produced, for the caller to present."""

    transcript: str
    reply: str
    audio_path: Path | None
    audio_sample_rate: int
    utterances: int
    llm_skipped: bool


def read_mono_pcm16(path: Path) -> bytes:
    """Load a WAV file as the 16 kHz mono s16le PCM the stages expect."""
    try:
        with wave.open(str(path), "rb") as source:
            if source.getnchannels() != 1 or source.getframerate() != DEMO_SAMPLE_RATE:
                raise AudioError(
                    "the demo needs 16 kHz mono audio; convert the file first",
                    details={
                        "file": str(path),
                        "channels": source.getnchannels(),
                        "sample_rate": source.getframerate(),
                    },
                )
            if source.getsampwidth() != 2:
                raise AudioError(
                    "the demo needs 16-bit PCM; convert the file first",
                    details={"file": str(path), "sample_width": source.getsampwidth()},
                )
            return source.readframes(source.getnframes())
    except AudioError:
        raise
    except (OSError, wave.Error) as exc:
        raise AudioError(
            "failed to read the WAV file",
            details={"file": str(path), "cause": repr(exc)},
        ) from exc


class VoiceDemoSession:
    """One warmed-up set of models, reusable across every turn of a demo.

    Building it pays the cold start — SenseVoice alone takes tens of seconds —
    so a batch run reports the latency a user would actually experience rather
    than re-reading ~1 GB of weights per utterance. The LLM stays lazy: a run
    that only exercises recognition and synthesis should not make a model call.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        vad = config.vad
        self._engine = SileroVadEngine()
        self._segmenter = VoiceActivitySegmenter(
            self._engine,
            sample_rate=DEMO_SAMPLE_RATE,
            threshold=vad.threshold,
            min_speech_ms=vad.min_speech_ms,
            max_silence_ms=vad.max_silence_ms,
            speech_pad_ms=vad.speech_pad_ms,
            max_speech_ms=vad.max_speech_ms,
        )
        # ``enabled`` gates model loading in the services; a demo run opts in by
        # definition, so force it rather than requiring a config edit.
        self._asr = AsrService(
            lambda: AsrSettings(section=dataclasses.replace(config.asr, enabled=True))
        )
        self._asr.start()
        self._tts = TtsService(
            lambda: TtsSettings(section=dataclasses.replace(config.tts, enabled=True))
        )
        self._tts.start()

    def __enter__(self) -> VoiceDemoSession:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the models; the session is unusable afterwards."""
        self._tts.stop()
        self._asr.stop()
        self._engine.close()

    def listen(self, pcm: bytes) -> list[SpeechSegment]:
        """Return the speech spans of ``pcm``, restarting the VAD session."""
        self._segmenter.reset()
        events = self._segmenter.feed(pcm)
        events.extend(self._segmenter.flush())
        return [event.segment for event in events if event.segment is not None]

    def transcribe(self, pcm: bytes, segments: list[SpeechSegment]) -> str:
        """Recognize every speech span and join them into one transcript."""
        texts = [self._asr.transcribe_segment(pcm, segment).text.strip() for segment in segments]
        return " ".join(text for text in texts if text)

    def ask(self, transcript: str) -> str:
        """Send the transcript through the configured LLM provider."""
        service = LlmService(lambda: self._config.llm)
        service.start()
        try:
            response = service.client.complete(
                [
                    ChatMessage.system(DEMO_SYSTEM_PROMPT),
                    ChatMessage.user(transcript),
                ]
            )
        finally:
            service.stop()
        return response.content.strip()

    def speak(self, text: str, out_path: Path) -> tuple[int, int]:
        """Synthesize ``text``, write it as a WAV, and return (bytes, sample_rate)."""
        audio, fmt, sample_rate = self._tts.synthesize_to_bytes(text)
        if not audio:
            return 0, sample_rate
        if fmt != "pcm_s16le":
            raise AudioError(
                "the demo can only persist PCM audio",
                details={"format": fmt},
            )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as sink:
            sink.setnchannels(1)
            sink.setsampwidth(2)
            sink.setframerate(sample_rate)
            sink.writeframes(audio)
        return len(audio), sample_rate


def run_voice_demo(
    config: AppConfig,
    wav_path: Path,
    *,
    out_path: Path | None = None,
    skip_llm: bool = False,
) -> DemoResult:
    """Drive WAV -> VAD -> ASR -> LLM -> TTS and persist the spoken reply.

    With ``skip_llm`` the utterance is echoed back verbatim, which still
    exercises recognition and synthesis while leaving the model call out.
    """
    pcm = read_mono_pcm16(wav_path)
    duration = len(pcm) / 2 / DEMO_SAMPLE_RATE
    logger.info("demo input %s: %.2f s of audio", wav_path.name, duration)

    audio_path = out_path
    if audio_path is None:
        audio_path = wav_path.with_name(f"{wav_path.stem}_reply.wav")

    with VoiceDemoSession(config) as session:
        segments = session.listen(pcm)
        logger.info("VAD found %d utterance(s)", len(segments))
        transcript = session.transcribe(pcm, segments)
        if not transcript:
            return DemoResult("", "", None, DEMO_SAMPLE_RATE, len(segments), skip_llm)
        logger.info("ASR transcript: %s", transcript)

        if skip_llm:
            reply = transcript
        else:
            try:
                reply = session.ask(transcript)
            except LlmError as exc:
                # Never trade a visible failure for a silently wrong demo.
                raise AudioError(
                    "the LLM stage failed; rerun with --skip-llm to demo "
                    "recognition and synthesis alone",
                    details={"cause": str(exc)},
                ) from exc

        written, rate = session.speak(reply, audio_path)

    if written:
        logger.info("TTS wrote %d bytes to %s", written, audio_path)
        return DemoResult(transcript, reply, audio_path, rate, len(segments), skip_llm)
    logger.warning("TTS produced no audio for %r", reply)
    return DemoResult(transcript, reply, None, rate, len(segments), skip_llm)
