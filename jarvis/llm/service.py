"""``LlmService`` — lifecycle component that owns LLM client instances.

Start does **no network I/O** (JARVIS must boot offline); it only builds
client objects from configuration. A missing API key env var is a warning
at startup (the assistant can still run text-less features) and a hard
:class:`~jarvis.llm.errors.LlmAuthError` at first actual use.

Consumers receive clients from here — constructing
:class:`~jarvis.llm.openai_compat.OpenAiCompatClient` anywhere else is an
architecture violation.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping

from jarvis.config.schema import LlmSection, ProviderSection
from jarvis.core.exceptions import ConfigurationError
from jarvis.llm.client import LlmClient
from jarvis.llm.openai_compat import OpenAiCompatClient, OpenAiCompatSettings
from jarvis.llm.transport import HttpTransport

__all__ = ["LlmService"]

logger = logging.getLogger("jarvis.llm.service")


class LlmService:
    """Builds and hands out :class:`LlmClient` instances per provider.

    Args:
        settings_provider: Callable returning the validated ``llm`` config
            section; evaluated at :meth:`start` because configuration does
            not exist yet at registration time.
        transport: Optional shared transport override (tests inject fakes).
        environ: Injectable environment mapping for key presence checks.
    """

    def __init__(
        self,
        settings_provider: Callable[[], LlmSection],
        *,
        transport: HttpTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._settings_provider = settings_provider
        self._transport = transport
        self._environ = environ
        self._section: LlmSection | None = None
        self._clients: dict[str, LlmClient] = {}

    @property
    def name(self) -> str:
        return "llm"

    def start(self) -> None:
        if self._section is not None:
            return
        section = self._settings_provider()
        self._section = section
        env = os.environ if self._environ is None else self._environ
        for provider in section.providers.values():
            if not env.get(provider.api_key_env, "").strip():
                logger.warning(
                    "provider '%s' has no API key yet (set %s); requests to it will fail",
                    provider.name,
                    provider.api_key_env,
                )
        logger.info(
            "llm ready (default=%s, providers: %s)",
            section.default_provider,
            ", ".join(sorted(section.providers)),
        )

    def stop(self) -> None:
        self._section = None
        self._clients.clear()

    # -- client access -------------------------------------------------------

    @property
    def client(self) -> LlmClient:
        """Client for the configured default provider."""
        section = self._require_started()
        return self.client_for(section.default_provider)

    def client_for(self, provider_name: str) -> LlmClient:
        """Client for a specific provider (cached per name).

        Raises:
            ConfigurationError: unknown provider name.
        """
        section = self._require_started()
        cached = self._clients.get(provider_name)
        if cached is not None:
            return cached
        provider = section.providers.get(provider_name)
        if provider is None:
            raise ConfigurationError(
                f"unknown LLM provider: '{provider_name}'",
                details={"configured": sorted(section.providers)},
            )
        client = self._build_client(section, provider)
        self._clients[provider_name] = client
        return client

    def _require_started(self) -> LlmSection:
        if self._section is None:
            raise ConfigurationError("LlmService is not started")
        return self._section

    def _build_client(self, section: LlmSection, provider: ProviderSection) -> LlmClient:
        settings = OpenAiCompatSettings(
            provider_name=provider.name,
            base_url=provider.base_url,
            model=provider.model,
            api_key_env=provider.api_key_env,
            timeout_seconds=section.timeout_seconds,
            max_retries=section.max_retries,
            retry_backoff_seconds=section.retry_backoff_seconds,
            cost_input_per_1m=provider.cost_input_per_1m,
            cost_output_per_1m=provider.cost_output_per_1m,
        )
        if self._transport is not None:
            return OpenAiCompatClient(settings, self._transport, environ=self._environ)
        return OpenAiCompatClient(settings, environ=self._environ)
