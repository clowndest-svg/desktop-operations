"""LLM error taxonomy.

Refines :class:`jarvis.core.exceptions.LlmError` into actionable categories
so callers (and the retry policy) can react by *kind* instead of parsing
messages:

* retryable: :class:`LlmRateLimitError`, :class:`LlmServerError`,
  :class:`LlmTimeoutError`, :class:`LlmConnectionError`;
* not retryable: :class:`LlmAuthError`, :class:`LlmRequestError`,
  :class:`LlmResponseError`.

Only these classes cross the package boundary; transport-level signals in
:mod:`jarvis.llm.transport` are internal and always get mapped here.
"""

from __future__ import annotations

from jarvis.core.exceptions import LlmError

__all__ = [
    "LlmAuthError",
    "LlmConnectionError",
    "LlmError",
    "LlmRateLimitError",
    "LlmRequestError",
    "LlmResponseError",
    "LlmServerError",
    "LlmTimeoutError",
]


class LlmAuthError(LlmError):
    """Authentication/authorization failed (HTTP 401/403, missing API key)."""


class LlmRateLimitError(LlmError):
    """Provider throttled the request (HTTP 429). Retryable."""


class LlmServerError(LlmError):
    """Provider-side failure (HTTP 5xx). Retryable."""


class LlmRequestError(LlmError):
    """The request itself was rejected (other HTTP 4xx: bad model, too long...)."""


class LlmResponseError(LlmError):
    """The provider answered, but the body is not a valid chat response."""


class LlmTimeoutError(LlmError):
    """No response within the configured timeout. Retryable."""


class LlmConnectionError(LlmError):
    """Network-level failure (DNS, refused, reset). Retryable."""
