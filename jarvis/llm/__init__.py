"""LLM access layer: OpenAI-compatible chat/completions abstraction.

Responsibility (delivered in phase 5):
    * Provider-agnostic client (GPT / Claude / Kimi / DeepSeek / Qwen /
      Gemini via compatible endpoints), streaming, tool-call passthrough,
      retry/timeout policy, token & cost accounting hooks.
    * Consumers never talk to HTTP SDKs directly — only to this package's
      interfaces.

Allowed dependencies: ``core``, ``config``, ``prompt``.
"""

from jarvis.llm.client import LlmClient
from jarvis.llm.errors import (
    LlmAuthError,
    LlmConnectionError,
    LlmError,
    LlmRateLimitError,
    LlmRequestError,
    LlmResponseError,
    LlmServerError,
    LlmTimeoutError,
)
from jarvis.llm.openai_compat import OpenAiCompatClient, OpenAiCompatSettings
from jarvis.llm.service import LlmService
from jarvis.llm.types import (
    ChatMessage,
    ChatResponse,
    GenerationOptions,
    Role,
    StreamChunk,
    ToolCall,
    Usage,
)

__all__ = [
    "ChatMessage",
    "ChatResponse",
    "GenerationOptions",
    "LlmAuthError",
    "LlmClient",
    "LlmConnectionError",
    "LlmError",
    "LlmRateLimitError",
    "LlmRequestError",
    "LlmResponseError",
    "LlmServerError",
    "LlmService",
    "LlmTimeoutError",
    "OpenAiCompatClient",
    "OpenAiCompatSettings",
    "Role",
    "StreamChunk",
    "ToolCall",
    "Usage",
]
