"""Shared kernel: cross-cutting primitives every layer may depend on.

Contents: exception hierarchy, ``Result`` type, application constants, and the
cross-layer event contracts in :mod:`jarvis.core.events`.

Dependency rule: ``core`` depends on the standard library ONLY — never on
any other ``jarvis`` package and never on third-party libraries. Everything
else in the codebase is allowed to depend on ``core``.
"""

from jarvis.core.constants import APP_NAME, APP_SLUG, DEFAULT_ENCODING, ENV_PREFIX
from jarvis.core.events import PipelineEvent, VoicePhase, VoiceStatus
from jarvis.core.exceptions import (
    AgentError,
    AsrError,
    AudioError,
    BrowserError,
    ComputerControlError,
    ConfigurationError,
    DangerousOperationRejectedError,
    DatabaseError,
    JarvisError,
    KnowledgeError,
    LlmError,
    McpError,
    MemoryStoreError,
    OcrError,
    PlannerError,
    PluginError,
    PromptError,
    SchedulerError,
    SecurityError,
    ToolError,
    ToolExecutionError,
    ToolNotFoundError,
    TtsError,
    VadError,
    VectorStoreError,
    VisionError,
    WakeWordError,
    WorkflowError,
)
from jarvis.core.result import Err, Ok, Result, UnwrapError

__all__ = [
    "APP_NAME",
    "APP_SLUG",
    "DEFAULT_ENCODING",
    "ENV_PREFIX",
    "AgentError",
    "AsrError",
    "AudioError",
    "BrowserError",
    "ComputerControlError",
    "ConfigurationError",
    "DangerousOperationRejectedError",
    "DatabaseError",
    "Err",
    "JarvisError",
    "KnowledgeError",
    "LlmError",
    "McpError",
    "MemoryStoreError",
    "OcrError",
    "Ok",
    "PipelineEvent",
    "PlannerError",
    "PluginError",
    "PromptError",
    "Result",
    "SchedulerError",
    "SecurityError",
    "ToolError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "TtsError",
    "UnwrapError",
    "VadError",
    "VectorStoreError",
    "VisionError",
    "VoicePhase",
    "VoiceStatus",
    "WakeWordError",
    "WorkflowError",
]
