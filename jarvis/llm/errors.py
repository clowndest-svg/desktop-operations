"""LLM error taxonomy.

Refines :class:`jarvis.core.exceptions.LlmError` into actionable categories
so callers (and the retry policy) can react by *kind* instead of parsing
messages:

* retryable: :class:`LlmRateLimitError`, :class:`LlmServerError`,
  :class:`LlmTimeoutError`, :class:`LlmConnectionError`;
* not retryable: :class:`LlmAuthError` (and its :class:`LlmPermissionError`),
  :class:`LlmRequestError`, :class:`LlmResponseError`.

Only these classes cross the package boundary; transport-level signals in
:mod:`jarvis.llm.transport` are internal and always get mapped here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from jarvis.core.exceptions import LlmError

__all__ = [
    "LlmAuthError",
    "LlmConnectionError",
    "LlmContextOverflowError",
    "LlmError",
    "LlmPermissionError",
    "LlmRateLimitError",
    "LlmRequestError",
    "LlmResponseError",
    "LlmServerError",
    "LlmTimeoutError",
]


class LlmAuthError(LlmError):
    """Authentication failed (HTTP 401, or a missing API key)."""

    kind: ClassVar[str] = "auth"


class LlmPermissionError(LlmAuthError):
    """The credential was understood but the request is not allowed (HTTP 403).

    Its own class because the two have different fixes and the panel says so: a 401 means
    the key itself is wrong, while a 403 usually means the key is fine and **this model**
    is not opened to it -- telling somebody to re-paste a working key is how a config
    screen teaches the wrong lesson. Subclasses :class:`LlmAuthError` so every caller that
    treats "not retryable, credential-side" as one group keeps working.
    """

    kind: ClassVar[str] = "forbidden"


class LlmRateLimitError(LlmError):
    """Provider throttled the request (HTTP 429). Retryable."""

    kind: ClassVar[str] = "quota"


class LlmServerError(LlmError):
    """Provider-side failure (HTTP 5xx). Retryable."""

    kind: ClassVar[str] = "server"


class LlmRequestError(LlmError):
    """The request itself was rejected (other HTTP 4xx: bad model, too long...)."""

    kind: ClassVar[str] = "request"


class LlmContextOverflowError(LlmRequestError):
    """The prompt is bigger than what this endpoint can hold in one go.

    Its own class because it is the one rejection with a *number* attached, and the
    fix is different: a bad model name is corrected in the row, while this is
    corrected by sending less. Measured on a local llama.cpp server (n_ctx=4096): one
    two-character question cost 4702 prompt tokens, 4639 of which were the 34 tool
    schemas -- so "she cannot use my local model" is this error, not a network one.

    ``prompt_tokens`` and ``context_tokens`` are what the server reported, not what we
    estimated; either may be ``None`` when the endpoint complains without numbers.
    """

    kind: ClassVar[str] = "overflow"

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
        prompt_tokens: int | None = None,
        context_tokens: int | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.prompt_tokens = prompt_tokens
        self.context_tokens = context_tokens


class LlmResponseError(LlmError):
    """The provider answered, but the body is not a valid chat response."""

    kind: ClassVar[str] = "malformed"


class LlmTimeoutError(LlmError):
    """No response within the configured timeout. Retryable."""

    kind: ClassVar[str] = "timeout"


class LlmConnectionError(LlmError):
    """Network-level failure (DNS, refused, reset). Retryable."""

    kind: ClassVar[str] = "network"
