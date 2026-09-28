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


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


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

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {"app", "logging", "llm", "wakeword", "vad", "asr", "tts", "orchestration"}
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
        )
