"""Typed, immutable configuration model.

The raw layered mapping produced by :mod:`jarvis.config.loader` is turned
into frozen dataclasses here. Validation is *fail-fast*: the first invalid
or unknown key aborts startup with a :class:`ConfigurationError` whose
message pinpoints the exact dotted key path (e.g. ``logging.level``).

Adding a new section in a later phase means: add a dataclass, register it
in :meth:`AppConfig.from_mapping`, and document the keys in
``defaults.yaml`` — nothing else.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Final

from jarvis.core.exceptions import ConfigurationError

VALID_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"development", "production"})
VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)
VALID_WAKEWORD_ENGINES: Final[frozenset[str]] = frozenset({"openwakeword", "porcupine", "asr"})
VALID_VAD_ENGINES: Final[frozenset[str]] = frozenset({"silero"})
VALID_ASR_ENGINES: Final[frozenset[str]] = frozenset({"sensevoice"})
VALID_TTS_ENGINES: Final[frozenset[str]] = frozenset({"cosyvoice", "edge_tts"})
VALID_EMBEDDING_ENGINES: Final[frozenset[str]] = frozenset({"hashing", "http"})
VALID_JOURNAL_MODES: Final[frozenset[str]] = frozenset(
    {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}
)
VALID_SYNCHRONOUS: Final[frozenset[str]] = frozenset({"OFF", "NORMAL", "FULL", "EXTRA"})
VALID_OCR_ENGINES: Final[frozenset[str]] = frozenset({"rapidocr"})
VALID_CAPTURE_BACKENDS: Final[frozenset[str]] = frozenset({"mss", "pillow"})
VALID_MCP_TRANSPORTS: Final[frozenset[str]] = frozenset({"stdio", "http"})


# ---------------------------------------------------------------------------
# Validation helpers (shared by every section)
# ---------------------------------------------------------------------------


def _key_error(key_path: str, problem: str, value: object = None) -> ConfigurationError:
    return ConfigurationError(
        f"invalid configuration at '{key_path}': {problem}",
        details={"key": key_path, "value": repr(value)},
    )


def require_section(data: Mapping[str, object], key: str) -> Mapping[str, object]:
    """Extract a nested mapping section, failing with a precise message."""
    value = data.get(key)
    if value is None:
        raise _key_error(key, "section is missing")
    if not isinstance(value, Mapping):
        raise _key_error(key, f"expected a mapping, got {type(value).__name__}", value)
    return value


def reject_unknown_keys(data: Mapping[str, object], allowed: frozenset[str], prefix: str) -> None:
    """Fail on unknown keys so typos never pass silently."""
    unknown = sorted(set(data) - allowed)
    if unknown:
        key_path = f"{prefix}.{unknown[0]}" if prefix else unknown[0]
        raise _key_error(
            key_path,
            f"unknown key (allowed: {', '.join(sorted(allowed))})",
        )


def require_str(data: Mapping[str, object], key: str, prefix: str) -> str:
    value = data.get(key)
    key_path = f"{prefix}.{key}"
    if not isinstance(value, str) or not value.strip():
        raise _key_error(key_path, "expected a non-empty string", value)
    return value


def require_bool(data: Mapping[str, object], key: str, prefix: str) -> bool:
    value = data.get(key)
    key_path = f"{prefix}.{key}"
    if not isinstance(value, bool):
        raise _key_error(key_path, "expected a boolean (true/false)", value)
    return value


def require_int(
    data: Mapping[str, object],
    key: str,
    prefix: str,
    *,
    minimum: int | None = None,
) -> int:
    value = data.get(key)
    key_path = f"{prefix}.{key}"
    # bool is a subclass of int — reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _key_error(key_path, "expected an integer", value)
    if minimum is not None and value < minimum:
        raise _key_error(key_path, f"must be >= {minimum}", value)
    return value


def require_float(
    data: Mapping[str, object],
    key: str,
    prefix: str,
    *,
    minimum: float | None = None,
) -> float:
    """Accept int or float YAML scalars (bool is rejected explicitly)."""
    value = data.get(key)
    key_path = f"{prefix}.{key}"
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _key_error(key_path, "expected a number", value)
    number = float(value)
    if minimum is not None and number < minimum:
        raise _key_error(key_path, f"must be >= {minimum:g}", value)
    return number


def require_str_list(data: Mapping[str, object], key: str, prefix: str) -> tuple[str, ...]:
    """A non-empty list of non-empty strings, returned as an immutable tuple."""
    value = data.get(key)
    key_path = f"{prefix}.{key}"
    if not isinstance(value, list) or not value:
        raise _key_error(key_path, "expected a non-empty list of strings", value)
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise _key_error(f"{key_path}[{index}]", "expected a non-empty string", item)
        items.append(item)
    return tuple(items)


def require_optional_str_list(data: Mapping[str, object], key: str, prefix: str) -> tuple[str, ...]:
    """Like :func:`require_str_list` but an empty list is allowed.

    Used for the allow/deny lists: "empty" is the meaningful default ("no
    restriction"), so rejecting it would make the feature impossible to disable.
    """
    value = data.get(key)
    key_path = f"{prefix}.{key}"
    if not isinstance(value, list):
        raise _key_error(key_path, "expected a list of strings", value)
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise _key_error(f"{key_path}[{index}]", "expected a non-empty string", item)
        items.append(item)
    return tuple(items)


def require_mapping(data: Mapping[str, object], key: str, prefix: str) -> Mapping[str, object]:
    """A nested mapping that may be empty (unlike :func:`require_section`)."""
    value = data.get(key)
    key_path = f"{prefix}.{key}"
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _key_error(key_path, f"expected a mapping, got {type(value).__name__}", value)
    return value


def require_str_map(data: Mapping[str, object], key: str, prefix: str) -> Mapping[str, str]:
    """A mapping of string to string (environment variables, headers...)."""
    raw = require_mapping(data, key, prefix)
    key_path = f"{prefix}.{key}"
    result: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise _key_error(f"{key_path}.{name}", "expected a string value", value)
        result[name] = value
    return result


def require_choice(
    data: Mapping[str, object],
    key: str,
    prefix: str,
    choices: frozenset[str],
    *,
    normalize_upper: bool = False,
) -> str:
    raw = require_str(data, key, prefix)
    value = raw.upper() if normalize_upper else raw
    if value not in choices:
        raise _key_error(
            f"{prefix}.{key}",
            f"must be one of: {', '.join(sorted(choices))}",
            raw,
        )
    return value


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppSection:
    """General application behaviour (``app.*``)."""

    environment: str
    """Runtime profile: ``development`` or ``production``."""

    language: str
    """UI / prompt language tag, e.g. ``zh-CN``."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset({"environment", "language"})

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> AppSection:
        reject_unknown_keys(data, cls._ALLOWED, "app")
        return cls(
            environment=require_choice(data, "environment", "app", VALID_ENVIRONMENTS),
            language=require_str(data, "language", "app"),
        )


@dataclass(frozen=True, slots=True)
class LoggingSection:
    """Logging behaviour (``logging.*``), consumed by phase 4."""

    level: str
    """Root log level: DEBUG/INFO/WARNING/ERROR/CRITICAL."""

    console: bool
    """Mirror logs to the console."""

    file_enabled: bool
    """Write rotating log files under ``<data>/logs``."""

    max_bytes: int
    """Rotate a log file after this many bytes."""

    backup_count: int
    """How many rotated files to keep."""

    third_party_level: str
    """Log level applied to non-``jarvis`` loggers (noise suppression)."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {"level", "console", "file_enabled", "max_bytes", "backup_count", "third_party_level"}
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> LoggingSection:
        reject_unknown_keys(data, cls._ALLOWED, "logging")
        return cls(
            level=require_choice(data, "level", "logging", VALID_LOG_LEVELS, normalize_upper=True),
            console=require_bool(data, "console", "logging"),
            file_enabled=require_bool(data, "file_enabled", "logging"),
            max_bytes=require_int(data, "max_bytes", "logging", minimum=1),
            backup_count=require_int(data, "backup_count", "logging", minimum=0),
            third_party_level=require_choice(
                data, "third_party_level", "logging", VALID_LOG_LEVELS, normalize_upper=True
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderSection:
    """One LLM provider endpoint (``llm.providers.<name>.*``).

    Providers are *data*: adding another OpenAI-compatible vendor is a pure
    YAML edit, no code change. The API key itself never appears here — only
    the name of the environment variable that holds it (``api_key_env``).
    """

    name: str
    """Provider key, taken from the mapping key under ``llm.providers``."""

    base_url: str
    """OpenAI-compatible API root, e.g. ``https://api.deepseek.com/v1``."""

    model: str
    """Model identifier sent with every request."""

    api_key_env: str
    """Environment variable that holds the API key (never the key itself)."""

    cost_input_per_1m: float
    """USD per 1M prompt tokens for cost accounting (0 = don't report)."""

    cost_output_per_1m: float
    """USD per 1M completion tokens for cost accounting (0 = don't report)."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {"base_url", "model", "api_key_env", "cost_input_per_1m", "cost_output_per_1m"}
    )

    @classmethod
    def from_mapping(cls, name: str, data: Mapping[str, object]) -> ProviderSection:
        prefix = f"llm.providers.{name}"
        reject_unknown_keys(data, cls._ALLOWED, prefix)
        return cls(
            name=name,
            base_url=require_str(data, "base_url", prefix).rstrip("/"),
            model=require_str(data, "model", prefix),
            api_key_env=require_str(data, "api_key_env", prefix),
            cost_input_per_1m=require_float(data, "cost_input_per_1m", prefix, minimum=0),
            cost_output_per_1m=require_float(data, "cost_output_per_1m", prefix, minimum=0),
        )


@dataclass(frozen=True, slots=True)
class LlmSection:
    """LLM access policy and provider registry (``llm.*``)."""

    default_provider: str
    """Which entry of ``providers`` is used unless a caller picks another."""

    timeout_seconds: float
    """Per-request HTTP timeout."""

    max_retries: int
    """Extra attempts on retryable failures (429/5xx/timeout/network)."""

    retry_backoff_seconds: float
    """Base delay for exponential backoff (0 disables waiting)."""

    providers: Mapping[str, ProviderSection]
    """All configured providers, keyed by name."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {"default_provider", "timeout_seconds", "max_retries", "retry_backoff_seconds", "providers"}
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> LlmSection:
        reject_unknown_keys(data, cls._ALLOWED, "llm")
        raw_providers = require_section(data, "providers")
        if not raw_providers:
            raise _key_error("llm.providers", "at least one provider must be configured")
        providers: dict[str, ProviderSection] = {}
        for name, raw in raw_providers.items():
            if not isinstance(raw, Mapping):
                raise _key_error(
                    f"llm.providers.{name}",
                    f"expected a mapping, got {type(raw).__name__}",
                    raw,
                )
            providers[name] = ProviderSection.from_mapping(name, raw)
        default_provider = require_str(data, "default_provider", "llm")
        if default_provider not in providers:
            raise _key_error(
                "llm.default_provider",
                f"unknown provider (configured: {', '.join(sorted(providers))})",
                default_provider,
            )
        return cls(
            default_provider=default_provider,
            timeout_seconds=require_float(data, "timeout_seconds", "llm", minimum=1),
            max_retries=require_int(data, "max_retries", "llm", minimum=0),
            retry_backoff_seconds=require_float(data, "retry_backoff_seconds", "llm", minimum=0),
            providers=providers,
        )

    @property
    def default(self) -> ProviderSection:
        """The provider selected by ``default_provider`` (always present)."""
        return self.providers[self.default_provider]


@dataclass(frozen=True, slots=True)
class PorcupineSection:
    """Porcupine-specific settings (``wakeword.porcupine.*``)."""

    access_key_env: str
    """Environment variable holding the Picovoice AccessKey (never the key)."""

    sensitivity: float
    """Per-keyword sensitivity in [0, 1]; higher = more hits, more false alarms."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset({"access_key_env", "sensitivity"})

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> PorcupineSection:
        prefix = "wakeword.porcupine"
        reject_unknown_keys(data, cls._ALLOWED, prefix)
        sensitivity = require_float(data, "sensitivity", prefix, minimum=0)
        if sensitivity > 1:
            raise _key_error(f"{prefix}.sensitivity", "must be <= 1", sensitivity)
        return cls(
            access_key_env=require_str(data, "access_key_env", prefix),
            sensitivity=sensitivity,
        )


@dataclass(frozen=True, slots=True)
class WakeWordSection:
    """Always-on wake-word detection (``wakeword.*``)."""

    enabled: bool
    """Grab the microphone and listen. Off by default — explicit opt-in."""

    engine: str
    """``openwakeword`` (offline, key-free) or ``porcupine`` (needs a key)."""

    keywords: tuple[str, ...]
    """Model names / keywords to spot (e.g. ``hey_jarvis``)."""

    threshold: float
    """Detector-side confidence threshold in [0, 1]."""

    cooldown_seconds: float
    """Suppress further wake events for this long after a hit."""

    porcupine: PorcupineSection
    """Engine-specific settings, validated even when unused."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {"enabled", "engine", "keywords", "threshold", "cooldown_seconds", "porcupine"}
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> WakeWordSection:
        reject_unknown_keys(data, cls._ALLOWED, "wakeword")
        threshold = require_float(data, "threshold", "wakeword", minimum=0)
        if threshold > 1:
            raise _key_error("wakeword.threshold", "must be <= 1", threshold)
        return cls(
            enabled=require_bool(data, "enabled", "wakeword"),
            engine=require_choice(data, "engine", "wakeword", VALID_WAKEWORD_ENGINES),
            keywords=require_str_list(data, "keywords", "wakeword"),
            threshold=threshold,
            cooldown_seconds=require_float(data, "cooldown_seconds", "wakeword", minimum=0),
            porcupine=PorcupineSection.from_mapping(require_section(data, "porcupine")),
        )


@dataclass(frozen=True, slots=True)
class VadSection:
    """Streaming voice-activity detection (``vad.*``).

    Endpointing policy only — the engine is a dumb per-frame speech
    scorer and all temporal decisions live in
    :class:`jarvis.vad.segmenter.VoiceActivitySegmenter`.
    """

    enabled: bool
    """Grab the microphone and run endpointing. Off by default — explicit opt-in."""

    engine: str
    """``silero`` (offline, ONNX) is the only engine for now."""

    threshold: float
    """Speech-probability gate in [0, 1]; frames at/above are speech."""

    min_speech_ms: int
    """Ignore utterances shorter than this (drops clicks / breath blips)."""

    max_silence_ms: int
    """End the utterance after this much continuous silence."""

    speech_pad_ms: int
    """Pad added around the detected speech so ASR gets clean edges."""

    max_speech_ms: int
    """Hard cap on a single utterance (ms); 0 = no cap (auto-segment)."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {
            "enabled",
            "engine",
            "threshold",
            "min_speech_ms",
            "max_silence_ms",
            "speech_pad_ms",
            "max_speech_ms",
        }
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> VadSection:
        reject_unknown_keys(data, cls._ALLOWED, "vad")
        threshold = require_float(data, "threshold", "vad", minimum=0)
        if threshold > 1:
            raise _key_error("vad.threshold", "must be <= 1", threshold)
        min_speech_ms = require_int(data, "min_speech_ms", "vad", minimum=0)
        max_silence_ms = require_int(data, "max_silence_ms", "vad", minimum=1)
        speech_pad_ms = require_int(data, "speech_pad_ms", "vad", minimum=0)
        max_speech_ms = require_int(data, "max_speech_ms", "vad", minimum=0)
        return cls(
            enabled=require_bool(data, "enabled", "vad"),
            engine=require_choice(data, "engine", "vad", VALID_VAD_ENGINES),
            threshold=threshold,
            min_speech_ms=min_speech_ms,
            max_silence_ms=max_silence_ms,
            speech_pad_ms=speech_pad_ms,
            max_speech_ms=max_speech_ms,
        )


@dataclass(frozen=True, slots=True)
class AsrSection:
    """Speech recognition (``asr.*``).

    The engine is *offline* (SenseVoice / FunASR, ONNX). ``model`` is the
    HuggingFace / ModelScope id (auto-downloaded on first use). ``language``
    is ``auto`` for detection or a forced tag (``zh`` / ``en`` …). ``temperature``
    and ``beam_size`` steer decoding; ``device`` selects the accelerator.
    """

    enabled: bool
    """Load the model and allow recognition. Off by default — explicit opt-in."""

    engine: str
    """``sensevoice`` (offline, ONNX) is the only engine for now."""

    model: str
    """Model id / path, e.g. ``iic/SenseVoiceSmall``."""

    language: str
    """``auto`` for detection, or a forced language tag (``zh`` / ``en``)."""

    temperature: float
    """Decoding temperature in [0, 1]; 0 = greedy."""

    beam_size: int
    """Beam-search width (>= 1); 1 = greedy."""

    device: str
    """Inference device: ``cpu`` or ``cuda``."""

    punctuation: bool = False
    """Restore punctuation with the ``ct-punc`` model.

    Off by default: it is a second, ~850 MB download on top of SenseVoice, and
    the transcripts are usable without it.
    """

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {
            "enabled",
            "engine",
            "model",
            "language",
            "temperature",
            "beam_size",
            "device",
            "punctuation",
        }
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> AsrSection:
        reject_unknown_keys(data, cls._ALLOWED, "asr")
        temperature = require_float(data, "temperature", "asr", minimum=0)
        if temperature > 1:
            raise _key_error("asr.temperature", "must be <= 1", temperature)
        beam_size = require_int(data, "beam_size", "asr", minimum=1)
        return cls(
            enabled=require_bool(data, "enabled", "asr"),
            engine=require_choice(data, "engine", "asr", VALID_ASR_ENGINES),
            model=require_str(data, "model", "asr"),
            language=require_str(data, "language", "asr"),
            temperature=temperature,
            beam_size=beam_size,
            device=require_str(data, "device", "asr"),
            punctuation=require_bool(data, "punctuation", "asr"),
        )


@dataclass(frozen=True, slots=True)
class TtsSection:
    """Speech synthesis (``tts.*``).

    The engine is selected by ``engine``: ``cosyvoice`` (offline, ONNX) or
    ``edge_tts`` (cloud, pure-Python). ``voice`` is the engine-specific
    voice / speaker id (e.g. ``中文女`` for CosyVoice, ``zh-CN-XiaoxiaoNeural``
    for Edge-TTS). ``speed`` is a multiplier (1.0 = normal) and ``volume`` a
    gain in [0, 1]; both are advisory for engines that do not expose them.
    ``model`` / ``device`` apply to the offline engine.
    """

    enabled: bool
    """Load the engine and allow synthesis. Off by default — explicit opt-in."""

    engine: str
    """``cosyvoice`` (offline, ONNX) or ``edge_tts`` (cloud)."""

    voice: str
    """Engine-specific voice / speaker id."""

    speed: float
    """Playback speed multiplier (>= 0); 1.0 = normal."""

    volume: float
    """Output gain in [0, 1]; 1.0 = full."""

    device: str
    """Inference device for the offline engine: ``cpu`` or ``cuda``."""

    model: str
    """Model id / path for the offline engine (e.g. ``iic/CosyVoice2-0.5B``)."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {"enabled", "engine", "voice", "speed", "volume", "device", "model"}
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> TtsSection:
        reject_unknown_keys(data, cls._ALLOWED, "tts")
        speed = require_float(data, "speed", "tts", minimum=0)
        volume = require_float(data, "volume", "tts", minimum=0)
        if volume > 1:
            raise _key_error("tts.volume", "must be <= 1", volume)
        return cls(
            enabled=require_bool(data, "enabled", "tts"),
            engine=require_choice(data, "engine", "tts", VALID_TTS_ENGINES),
            voice=require_str(data, "voice", "tts"),
            speed=speed,
            volume=volume,
            device=require_str(data, "device", "tts"),
            model=require_str(data, "model", "tts"),
        )


@dataclass(frozen=True, slots=True)
class OrchestrationSection:
    """End-to-end voice orchestration (``orchestration.*``).

    When enabled, the orchestration service owns a *single* microphone loop
    and drives the full chain wake-word → VAD → ASR → multi-agent (LangGraph)
    → TTS, including Barge-In. The standalone ``wakeword`` / ``vad`` loops are
    NOT started in this mode (their engines are driven internally instead);
    ``asr`` and ``tts`` must still be enabled so their models are loaded for
    the orchestrator to consume.
    """

    enabled: bool
    """Run the full voice pipeline. Off by default — explicit opt-in."""

    barge_in: bool
    """Allow the user to interrupt an in-flight spoken reply (Barge-In)."""

    default_agent: str
    """The agent the supervisor routes to when intent is ambiguous."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset({"enabled", "barge_in", "default_agent"})

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> OrchestrationSection:
        reject_unknown_keys(data, cls._ALLOWED, "orchestration")
        return cls(
            enabled=require_bool(data, "enabled", "orchestration"),
            barge_in=require_bool(data, "barge_in", "orchestration"),
            default_agent=require_str(data, "default_agent", "orchestration"),
        )


@dataclass(frozen=True, slots=True)
class MemorySection:
    """Long-term memory (``memory.*``).

    ``auto_extract`` costs one model call per *interesting* turn — a cheap marker
    filter runs first, so small talk never reaches the model. Turning it off
    leaves explicit ``记住…`` commands working.
    """

    enabled: bool
    """Run the memory service. Off means no persistence and no recall."""

    max_recall: int
    """How many memories recall returns when the caller does not say."""

    min_score: float
    """Blended-score floor. A wrong memory recited confidently is worse than none."""

    history_turns: int
    """Conversation turns replayed into the prompt for a session."""

    auto_extract: bool
    """Mine each turn for durable facts (needs an LLM)."""

    max_extract: int
    """Upper bound on memories proposed per turn."""

    summarize_after_turns: int
    """Compress a session once it holds this many turns; 0 disables it."""

    max_memories: int
    """Soft cap per scope; the least useful memories are pruned past it."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {
            "enabled",
            "max_recall",
            "min_score",
            "history_turns",
            "auto_extract",
            "max_extract",
            "summarize_after_turns",
            "max_memories",
        }
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> MemorySection:
        reject_unknown_keys(data, cls._ALLOWED, "memory")
        return cls(
            enabled=require_bool(data, "enabled", "memory"),
            max_recall=require_int(data, "max_recall", "memory", minimum=0),
            min_score=require_float(data, "min_score", "memory"),
            history_turns=require_int(data, "history_turns", "memory", minimum=0),
            auto_extract=require_bool(data, "auto_extract", "memory"),
            max_extract=require_int(data, "max_extract", "memory", minimum=0),
            summarize_after_turns=require_int(data, "summarize_after_turns", "memory", minimum=0),
            max_memories=require_int(data, "max_memories", "memory", minimum=0),
        )


@dataclass(frozen=True, slots=True)
class KnowledgeSection:
    """RAG knowledge base (``knowledge.*``)."""

    enabled: bool
    """Ingest and retrieve documents. Off leaves the vector index unused."""

    chunk_size: int
    """Target chunk length in characters."""

    chunk_overlap: int
    """Characters shared between neighbours, so a sentence split in half is
    still recoverable from either side. Must be smaller than ``chunk_size``."""

    top_k: int
    """Chunks retrieved per question when the caller does not say."""

    min_score: float
    """Similarity floor for a chunk to count as evidence."""

    max_file_mb: int
    """Refuse files larger than this rather than reading them into memory."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {"enabled", "chunk_size", "chunk_overlap", "top_k", "min_score", "max_file_mb"}
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> KnowledgeSection:
        reject_unknown_keys(data, cls._ALLOWED, "knowledge")
        chunk_size = require_int(data, "chunk_size", "knowledge", minimum=1)
        chunk_overlap = require_int(data, "chunk_overlap", "knowledge", minimum=0)
        if chunk_overlap >= chunk_size:
            raise _key_error(
                "knowledge.chunk_overlap",
                "must be smaller than chunk_size (otherwise chunking never advances)",
                chunk_overlap,
            )
        return cls(
            enabled=require_bool(data, "enabled", "knowledge"),
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            top_k=require_int(data, "top_k", "knowledge", minimum=0),
            min_score=require_float(data, "min_score", "knowledge"),
            max_file_mb=require_int(data, "max_file_mb", "knowledge", minimum=1),
        )


@dataclass(frozen=True, slots=True)
class ToolsSection:
    """Tool framework and its safety policy (``tools.*``).

    The two ``allow_*`` switches default to ``false`` because they are the only
    ones that can change the machine: reading a file is recoverable, running a
    shell command and overwriting a document are not.
    """

    enabled: bool
    """Register and expose tools. Off leaves only the built-in chat agent."""

    confirm_dangerous: bool
    """Require an explicit confirmation flag on dangerous tools."""

    allow_write: bool
    """Permit tools that create, modify or delete files."""

    allow_shell: bool
    """Permit tools that run a shell command."""

    file_roots: tuple[str, ...]
    """Directories tools may touch. Empty means "the user's home directory"."""

    max_result_chars: int
    """Truncate tool output before it reaches the model, so one ``ls`` of a
    huge directory cannot blow the context window."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {
            "enabled",
            "confirm_dangerous",
            "allow_write",
            "allow_shell",
            "file_roots",
            "max_result_chars",
        }
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> ToolsSection:
        reject_unknown_keys(data, cls._ALLOWED, "tools")
        return cls(
            enabled=require_bool(data, "enabled", "tools"),
            confirm_dangerous=require_bool(data, "confirm_dangerous", "tools"),
            allow_write=require_bool(data, "allow_write", "tools"),
            allow_shell=require_bool(data, "allow_shell", "tools"),
            file_roots=require_optional_str_list(data, "file_roots", "tools"),
            max_result_chars=require_int(data, "max_result_chars", "tools", minimum=1),
        )


@dataclass(frozen=True, slots=True)
class SchedulerSection:
    """Timed jobs (``scheduler.*``)."""

    enabled: bool
    """Start the background scheduler thread."""

    timezone: str
    """IANA zone used to interpret cron expressions, e.g. ``Asia/Shanghai``."""

    max_concurrent: int
    """How many jobs may run at the same time."""

    misfire_grace_seconds: int
    """How late a job may still run after its scheduled time. Without a grace
    window, a laptop that was asleep at 09:00 never runs the 09:00 job."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {"enabled", "timezone", "max_concurrent", "misfire_grace_seconds"}
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> SchedulerSection:
        reject_unknown_keys(data, cls._ALLOWED, "scheduler")
        return cls(
            enabled=require_bool(data, "enabled", "scheduler"),
            timezone=require_str(data, "timezone", "scheduler"),
            max_concurrent=require_int(data, "max_concurrent", "scheduler", minimum=1),
            misfire_grace_seconds=require_int(
                data, "misfire_grace_seconds", "scheduler", minimum=0
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkflowSection:
    """Trigger-condition-action workflows (``workflow.*``)."""

    enabled: bool
    """Load and run workflow definitions."""

    directory: str
    """Folder under the data root holding ``*.yaml`` workflow definitions."""

    max_steps: int
    """Hard cap on steps per run, so a cycle in a definition cannot spin."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset({"enabled", "directory", "max_steps"})

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> WorkflowSection:
        reject_unknown_keys(data, cls._ALLOWED, "workflow")
        return cls(
            enabled=require_bool(data, "enabled", "workflow"),
            directory=require_str(data, "directory", "workflow"),
            max_steps=require_int(data, "max_steps", "workflow", minimum=1),
        )


@dataclass(frozen=True, slots=True)
class OcrSection:
    """Optical character recognition (``ocr.*``)."""

    enabled: bool
    """Load the recognition engine. Off by default: it pulls ONNX Runtime."""

    engine: str
    """``rapidocr`` (offline, ONNX, no PyTorch) is the only engine for now."""

    languages: tuple[str, ...]
    """Language hints, e.g. ``ch`` / ``en``."""

    min_confidence: float
    """Drop text blocks below this confidence; OCR noise in a prompt is worse
    than a missing line."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {"enabled", "engine", "languages", "min_confidence"}
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> OcrSection:
        reject_unknown_keys(data, cls._ALLOWED, "ocr")
        return cls(
            enabled=require_bool(data, "enabled", "ocr"),
            engine=require_choice(data, "engine", "ocr", VALID_OCR_ENGINES),
            languages=require_str_list(data, "languages", "ocr"),
            min_confidence=require_float(data, "min_confidence", "ocr", minimum=0),
        )


@dataclass(frozen=True, slots=True)
class VisionSection:
    """Screen capture and image understanding (``vision.*``)."""

    enabled: bool
    """Allow screenshots. Off by default: a screenshot is the most private
    thing this assistant can touch."""

    backend: str
    """``mss`` (fast, small) or ``pillow`` (already present on many installs)."""

    max_image_mb: int
    """Refuse images larger than this."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset({"enabled", "backend", "max_image_mb"})

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> VisionSection:
        reject_unknown_keys(data, cls._ALLOWED, "vision")
        return cls(
            enabled=require_bool(data, "enabled", "vision"),
            backend=require_choice(data, "backend", "vision", VALID_CAPTURE_BACKENDS),
            max_image_mb=require_int(data, "max_image_mb", "vision", minimum=1),
        )


@dataclass(frozen=True, slots=True)
class BrowserSection:
    """Browser automation (``browser.*``)."""

    enabled: bool
    """Allow driving a browser. Off by default; it downloads a browser binary."""

    headless: bool
    """Run without a visible window."""

    timeout_seconds: float
    """Per-navigation timeout."""

    max_text_chars: int
    """Truncate extracted page text before it reaches the model."""

    allow_domains: tuple[str, ...]
    """If non-empty, only these domains may be visited. Empty means no
    allow-list, which is only sane together with ``block_domains``."""

    block_domains: tuple[str, ...]
    """Domains never visited, checked before the allow-list."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {
            "enabled",
            "headless",
            "timeout_seconds",
            "max_text_chars",
            "allow_domains",
            "block_domains",
        }
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> BrowserSection:
        reject_unknown_keys(data, cls._ALLOWED, "browser")
        return cls(
            enabled=require_bool(data, "enabled", "browser"),
            headless=require_bool(data, "headless", "browser"),
            timeout_seconds=require_float(data, "timeout_seconds", "browser", minimum=1),
            max_text_chars=require_int(data, "max_text_chars", "browser", minimum=1),
            allow_domains=require_optional_str_list(data, "allow_domains", "browser"),
            block_domains=require_optional_str_list(data, "block_domains", "browser"),
        )


@dataclass(frozen=True, slots=True)
class ComputerSection:
    """Mouse/keyboard control (``computer.*``).

    ``dry_run`` defaults to ``true``: the controller plans and reports the
    actions it *would* take without moving the cursor. Turning it off is a
    deliberate act, which is the right shape for the only feature in this
    project that can click "send" on the user's behalf.
    """

    enabled: bool
    """Register the control service at all."""

    dry_run: bool
    """Plan actions without executing them."""

    allow_mouse: bool
    """Permit pointer movement and clicks."""

    allow_keyboard: bool
    """Permit typing and key presses."""

    confirm_dangerous: bool
    """Require an explicit confirmation flag for risky actions."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {"enabled", "dry_run", "allow_mouse", "allow_keyboard", "confirm_dangerous"}
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> ComputerSection:
        reject_unknown_keys(data, cls._ALLOWED, "computer")
        return cls(
            enabled=require_bool(data, "enabled", "computer"),
            dry_run=require_bool(data, "dry_run", "computer"),
            allow_mouse=require_bool(data, "allow_mouse", "computer"),
            allow_keyboard=require_bool(data, "allow_keyboard", "computer"),
            confirm_dangerous=require_bool(data, "confirm_dangerous", "computer"),
        )


@dataclass(frozen=True, slots=True)
class McpServerSection:
    """One MCP server entry (``mcp.servers.<name>.*``)."""

    name: str
    """Server key, taken from the mapping key."""

    transport: str
    """``stdio`` (spawn a command) or ``http`` (connect to a URL)."""

    command: str
    """Executable to spawn for ``stdio`` (empty for ``http``)."""

    args: tuple[str, ...]
    """Arguments passed to ``command``."""

    url: str
    """Endpoint for ``http`` (empty for ``stdio``)."""

    env: Mapping[str, str]
    """Extra environment variables for the spawned process."""

    enabled: bool
    """Whether to connect on start-up."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {"transport", "command", "args", "url", "env", "enabled"}
    )

    @classmethod
    def from_mapping(cls, name: str, data: Mapping[str, object]) -> McpServerSection:
        prefix = f"mcp.servers.{name}"
        reject_unknown_keys(data, cls._ALLOWED, prefix)
        transport = require_choice(data, "transport", prefix, VALID_MCP_TRANSPORTS)
        command = str(data.get("command") or "").strip()
        url = str(data.get("url") or "").strip()
        if transport == "stdio" and not command:
            raise _key_error(f"{prefix}.command", "required when transport is 'stdio'")
        if transport == "http" and not url:
            raise _key_error(f"{prefix}.url", "required when transport is 'http'")
        return cls(
            name=name,
            transport=transport,
            command=command,
            args=require_optional_str_list(data, "args", prefix),
            url=url,
            env=require_str_map(data, "env", prefix),
            enabled=require_bool(data, "enabled", prefix),
        )


@dataclass(frozen=True, slots=True)
class McpSection:
    """Model Context Protocol client (``mcp.*``)."""

    enabled: bool
    """Connect to the configured servers on start-up."""

    connect_timeout_seconds: float
    """How long a server may take to come up."""

    call_timeout_seconds: float
    """How long one tool call may take."""

    servers: Mapping[str, McpServerSection]
    """Configured servers, keyed by name."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {"enabled", "connect_timeout_seconds", "call_timeout_seconds", "servers"}
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> McpSection:
        reject_unknown_keys(data, cls._ALLOWED, "mcp")
        raw_servers = require_mapping(data, "servers", "mcp")
        servers: dict[str, McpServerSection] = {}
        for name, entry in raw_servers.items():
            if not isinstance(entry, Mapping):
                raise _key_error(f"mcp.servers.{name}", "expected a mapping", entry)
            servers[name] = McpServerSection.from_mapping(name, entry)
        return cls(
            enabled=require_bool(data, "enabled", "mcp"),
            connect_timeout_seconds=require_float(
                data, "connect_timeout_seconds", "mcp", minimum=1
            ),
            call_timeout_seconds=require_float(data, "call_timeout_seconds", "mcp", minimum=1),
            servers=servers,
        )


@dataclass(frozen=True, slots=True)
class PluginsSection:
    """Third-party plugin loading (``plugins.*``).

    ``allow_entrypoints`` is off by default: scanning installed distributions
    for entry points runs code the user never asked for, from packages that have
    nothing to do with JARVIS. A plugin in the configured folder is a visible
    act; an entry point is not.
    """

    enabled: bool
    """Discover and load plugins."""

    directory: str
    """Folder under the data root holding one directory per plugin."""

    allow_entrypoints: bool
    """Also load plugins advertised through the ``jarvis.plugins`` entry point."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset({"enabled", "directory", "allow_entrypoints"})

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> PluginsSection:
        reject_unknown_keys(data, cls._ALLOWED, "plugins")
        return cls(
            enabled=require_bool(data, "enabled", "plugins"),
            directory=require_str(data, "directory", "plugins"),
            allow_entrypoints=require_bool(data, "allow_entrypoints", "plugins"),
        )


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatabaseSection:
    """Shared SQLite persistence (``database.*``).

    One file backs every capability that needs to remember something: long-term
    memory, the knowledge base, vector records, scheduled jobs and workflow runs.
    There is deliberately no ``enabled`` switch — the driver is the standard
    library, opening it costs nothing, and a half-disabled database would leave
    four other subsystems with a "why is this empty" bug instead of an error.
    """

    filename: str
    """File name under ``<data>/database``."""

    busy_timeout_ms: int
    """How long a blocked writer waits before raising "database is locked"."""

    journal_mode: str
    """One of WAL / DELETE / TRUNCATE / PERSIST / MEMORY / OFF."""

    synchronous: str
    """Durability level; ``NORMAL`` is the right default under WAL."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {"filename", "busy_timeout_ms", "journal_mode", "synchronous"}
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> DatabaseSection:
        reject_unknown_keys(data, cls._ALLOWED, "database")
        return cls(
            filename=require_str(data, "filename", "database"),
            busy_timeout_ms=require_int(data, "busy_timeout_ms", "database", minimum=0),
            journal_mode=require_choice(
                data, "journal_mode", "database", VALID_JOURNAL_MODES, normalize_upper=True
            ),
            synchronous=require_choice(
                data, "synchronous", "database", VALID_SYNCHRONOUS, normalize_upper=True
            ),
        )


@dataclass(frozen=True, slots=True)
class EmbeddingSection:
    """Embedding engine (``vector.embedding.*``).

    ``hashing`` is the default because it needs no key and no network, so a fresh
    install can ingest documents immediately. ``http`` points at any
    OpenAI-compatible ``/embeddings`` endpoint and is strictly better at recall.
    """

    engine: str
    """``hashing`` (offline, zero-dependency) or ``http`` (OpenAI-compatible)."""

    dimension: int
    """Vector width. For ``hashing`` this *is* the index width; for ``http`` it is
    a cross-check — ``0`` means "accept whatever the endpoint returns"."""

    base_url: str
    """API root for the ``http`` engine, e.g. ``https://api.openai.com/v1``."""

    model: str
    """Embedding model id for the ``http`` engine."""

    api_key_env: str
    """Environment variable holding the key (never the key itself)."""

    timeout_seconds: float
    """Per-request timeout for the ``http`` engine."""

    batch_size: int
    """Inputs per request for the ``http`` engine."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {"engine", "dimension", "base_url", "model", "api_key_env", "timeout_seconds", "batch_size"}
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> EmbeddingSection:
        reject_unknown_keys(data, cls._ALLOWED, "vector.embedding")
        return cls(
            engine=require_choice(data, "engine", "vector.embedding", VALID_EMBEDDING_ENGINES),
            dimension=require_int(data, "dimension", "vector.embedding", minimum=0),
            base_url=require_str(data, "base_url", "vector.embedding").rstrip("/"),
            model=require_str(data, "model", "vector.embedding"),
            api_key_env=require_str(data, "api_key_env", "vector.embedding"),
            timeout_seconds=require_float(data, "timeout_seconds", "vector.embedding", minimum=1),
            batch_size=require_int(data, "batch_size", "vector.embedding", minimum=1),
        )


@dataclass(frozen=True, slots=True)
class VectorSection:
    """Similarity search over the shared database (``vector.*``)."""

    embedding: EmbeddingSection
    """Which engine produces the vectors, and how."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset({"embedding"})

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> VectorSection:
        reject_unknown_keys(data, cls._ALLOWED, "vector")
        return cls(embedding=EmbeddingSection.from_mapping(require_section(data, "embedding")))


@dataclass(frozen=True, slots=True)
class PromptSection:
    """Prompt templates (``prompt.*``).

    ``overrides`` is a free-form ``name -> body`` map. An override for a name
    that is not a registered template is **skipped with a warning** rather than
    refused: a typo in a config file should leave the assistant working with the
    shipped prompt, not stop it from starting.
    """

    overrides: Mapping[str, str]
    """Template name to replacement body. Empty by default."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset({"overrides"})

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> PromptSection:
        reject_unknown_keys(data, cls._ALLOWED, "prompt")
        return cls(overrides=require_str_map(data, "overrides", "prompt"))


@dataclass(frozen=True, slots=True)
class PlannerSection:
    """Task decomposition (``planner.*``).

    The planner produces and maintains plans; it does not execute them. Running
    a step belongs to the orchestration layer, which owns the tool registry and
    the event stream.
    """

    enabled: bool
    """Allow planning. Off means the assistant answers but never plans."""

    max_steps: int
    """Hard cap on steps per plan. Also passed to the model, which otherwise
    cheerfully returns twelve steps for a one-line request."""

    temperature: float | None
    """Sampling temperature for decomposition. ``None`` uses the provider
    default. Low values are right here: planning is a structured-output task,
    and creativity shows up as invented tools."""

    _ALLOWED: ClassVar[frozenset[str]] = frozenset({"enabled", "max_steps", "temperature"})

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> PlannerSection:
        reject_unknown_keys(data, cls._ALLOWED, "planner")
        raw_temperature = data.get("temperature")
        temperature: float | None = None
        if raw_temperature is not None:
            temperature = require_float(data, "temperature", "planner", minimum=0)
            if temperature > 2:
                raise _key_error("planner.temperature", "must be <= 2", temperature)
        return cls(
            enabled=require_bool(data, "enabled", "planner"),
            max_steps=require_int(data, "max_steps", "planner", minimum=1),
            temperature=temperature,
        )


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Root of the validated configuration tree."""

    app: AppSection
    logging: LoggingSection
    llm: LlmSection
    wakeword: WakeWordSection
    vad: VadSection
    asr: AsrSection
    tts: TtsSection
    orchestration: OrchestrationSection
    database: DatabaseSection
    vector: VectorSection
    memory: MemorySection
    knowledge: KnowledgeSection
    tools: ToolsSection
    scheduler: SchedulerSection
    workflow: WorkflowSection
    ocr: OcrSection
    vision: VisionSection
    browser: BrowserSection
    computer: ComputerSection
    mcp: McpSection
    plugins: PluginsSection
    prompt: PromptSection
    planner: PlannerSection

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {
            "app",
            "logging",
            "llm",
            "wakeword",
            "vad",
            "asr",
            "tts",
            "orchestration",
            "database",
            "vector",
            "memory",
            "knowledge",
            "tools",
            "scheduler",
            "workflow",
            "ocr",
            "vision",
            "browser",
            "computer",
            "mcp",
            "plugins",
            "prompt",
            "planner",
        }
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> AppConfig:
        """Validate the merged raw mapping into a typed config.

        Raises:
            ConfigurationError: on the first missing/unknown/ill-typed key.
        """
        reject_unknown_keys(data, cls._ALLOWED, "")
        return cls(
            app=AppSection.from_mapping(require_section(data, "app")),
            logging=LoggingSection.from_mapping(require_section(data, "logging")),
            llm=LlmSection.from_mapping(require_section(data, "llm")),
            wakeword=WakeWordSection.from_mapping(require_section(data, "wakeword")),
            vad=VadSection.from_mapping(require_section(data, "vad")),
            asr=AsrSection.from_mapping(require_section(data, "asr")),
            tts=TtsSection.from_mapping(require_section(data, "tts")),
            orchestration=OrchestrationSection.from_mapping(require_section(data, "orchestration")),
            database=DatabaseSection.from_mapping(require_section(data, "database")),
            vector=VectorSection.from_mapping(require_section(data, "vector")),
            memory=MemorySection.from_mapping(require_section(data, "memory")),
            knowledge=KnowledgeSection.from_mapping(require_section(data, "knowledge")),
            tools=ToolsSection.from_mapping(require_section(data, "tools")),
            scheduler=SchedulerSection.from_mapping(require_section(data, "scheduler")),
            workflow=WorkflowSection.from_mapping(require_section(data, "workflow")),
            ocr=OcrSection.from_mapping(require_section(data, "ocr")),
            vision=VisionSection.from_mapping(require_section(data, "vision")),
            browser=BrowserSection.from_mapping(require_section(data, "browser")),
            computer=ComputerSection.from_mapping(require_section(data, "computer")),
            mcp=McpSection.from_mapping(require_section(data, "mcp")),
            plugins=PluginsSection.from_mapping(require_section(data, "plugins")),
            prompt=PromptSection.from_mapping(require_section(data, "prompt")),
            planner=PlannerSection.from_mapping(require_section(data, "planner")),
        )
