"""Configuration system: typed loading & validation of YAML config.

Responsibility (delivered in phase 3):
    * Load layered YAML config (built-in ``defaults.yaml`` -> user override
      file -> ``JARVIS_SECTION__KEY`` environment variables) and validate it
      into typed, immutable objects (:class:`AppConfig`).
    * Own the Windows data-directory layout (:class:`AppPaths`).
    * No other package reads YAML or environment variables directly —
      everything is injected by the composition root via
      :class:`ConfigService`.

Allowed dependencies: ``core`` only.
"""

from jarvis.config.paths import AppPaths, default_data_dir, export_model_cache_env
from jarvis.config.schema import (
    AppConfig,
    AppSection,
    AsrSection,
    BrowserSection,
    ComputerSection,
    DatabaseSection,
    EmbeddingSection,
    KnowledgeSection,
    LlmSection,
    LoggingSection,
    McpSection,
    McpServerSection,
    MemorySection,
    MobileSection,
    ModelSpec,
    OcrSection,
    OrchestrationSection,
    PlannerSection,
    PluginsSection,
    PorcupineSection,
    PromptSection,
    ProviderSection,
    SchedulerSection,
    ToolsSection,
    TtsSection,
    VadSection,
    VectorSection,
    VisionSection,
    WakeWordSection,
    WorkflowSection,
)
from jarvis.config.service import ConfigService

__all__ = [
    "AppConfig",
    "AppPaths",
    "AppSection",
    "AsrSection",
    "BrowserSection",
    "ComputerSection",
    "ConfigService",
    "DatabaseSection",
    "EmbeddingSection",
    "KnowledgeSection",
    "LlmSection",
    "LoggingSection",
    "McpSection",
    "McpServerSection",
    "MemorySection",
    "MobileSection",
    "ModelSpec",
    "OcrSection",
    "OrchestrationSection",
    "PlannerSection",
    "PluginsSection",
    "PorcupineSection",
    "PromptSection",
    "ProviderSection",
    "SchedulerSection",
    "ToolsSection",
    "TtsSection",
    "VadSection",
    "VectorSection",
    "VisionSection",
    "WakeWordSection",
    "WorkflowSection",
    "default_data_dir",
    "export_model_cache_env",
]
