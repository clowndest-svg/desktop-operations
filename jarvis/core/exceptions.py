"""JARVIS exception hierarchy.

Every error raised by JARVIS code derives from :class:`JarvisError`, so
callers can distinguish "our" failures from third-party/stdlib ones with a
single ``except JarvisError`` clause. One subclass exists per bounded
context; subsystems may refine these further in their own packages, but the
classes below are the only ones other layers are allowed to depend on.

Guidelines:

* Raise the most specific class available.
* Use ``raise ... from exc`` to preserve the original cause.
* Put machine-readable context into ``details`` instead of formatting it
  into the message (messages are for humans, details are for logs/UI).
"""

from __future__ import annotations

from collections.abc import Mapping


class JarvisError(Exception):
    """Root of the JARVIS exception hierarchy.

    Args:
        message: Human-readable description of the failure.
        details: Optional machine-readable context (logged, shown in UI).
    """

    def __init__(self, message: str, *, details: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, object] = dict(details or {})

    def __str__(self) -> str:
        if self.details:
            joined = ", ".join(f"{key}={value!r}" for key, value in self.details.items())
            return f"{self.message} ({joined})"
        return self.message


# --------------------------------------------------------------------------
# Platform / infrastructure
# --------------------------------------------------------------------------


class ConfigurationError(JarvisError):
    """Invalid, missing, or unparsable configuration (YAML, env, schema)."""


class DatabaseError(JarvisError):
    """SQLite / persistence layer failure."""


class SecurityError(JarvisError):
    """Security policy violation (secret handling, blocked operation)."""


class DangerousOperationRejectedError(SecurityError):
    """A dangerous operation (delete, shell, format...) was not confirmed."""


# --------------------------------------------------------------------------
# LLM / prompt
# --------------------------------------------------------------------------


class LlmError(JarvisError):
    """LLM provider failure (network, auth, rate limit, bad response)."""


class PromptError(JarvisError):
    """Prompt template missing or failed to render."""


# --------------------------------------------------------------------------
# Voice pipeline
# --------------------------------------------------------------------------


class AudioError(JarvisError):
    """Base class for the audio/voice pipeline."""


class WakeWordError(AudioError):
    """Wake-word engine failure (Porcupine / OpenWakeWord)."""


class VadError(AudioError):
    """Voice-activity-detection failure (Silero VAD)."""


class AsrError(AudioError):
    """Speech recognition failure (SenseVoice / FunASR / Whisper)."""


class TtsError(AudioError):
    """Speech synthesis failure (CosyVoice / Edge-TTS / GPT-SoVITS)."""


# --------------------------------------------------------------------------
# Agents / orchestration
# --------------------------------------------------------------------------


class AgentError(JarvisError):
    """Agent execution failure (LangGraph node/graph level)."""


class PlannerError(AgentError):
    """Task planning / decomposition failure."""


class WorkflowError(JarvisError):
    """Workflow definition or execution failure."""


class SchedulerError(JarvisError):
    """Scheduled/triggered task failure."""


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------


class MemoryStoreError(JarvisError):
    """Short/long-term memory storage or recall failure."""


class KnowledgeError(JarvisError):
    """Knowledge base / RAG failure."""


class VectorStoreError(JarvisError):
    """Vector index (FAISS) failure."""


class VisionError(JarvisError):
    """Screen/image understanding failure."""


class OcrError(VisionError):
    """OCR engine failure (PaddleOCR)."""


class BrowserError(JarvisError):
    """Browser automation failure (Playwright)."""


class ComputerControlError(JarvisError):
    """Mouse/keyboard/window control failure."""


# --------------------------------------------------------------------------
# Extensibility
# --------------------------------------------------------------------------


class ToolError(JarvisError):
    """Base class for tool-calling failures."""


class ToolNotFoundError(ToolError):
    """Requested tool is not registered."""


class ToolExecutionError(ToolError):
    """A registered tool raised during execution."""


class PluginError(JarvisError):
    """Plugin discovery, loading, or lifecycle failure."""


class McpError(JarvisError):
    """MCP server connection or protocol failure."""
