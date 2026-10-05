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

from jarvis.config.schema import LlmSection, ModelSpec, ProviderSection
from jarvis.core.events import UsageEvent
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
        usage_sink: Called once per answered request with its token accounting.
            ``llm`` must not import the storage layer, so the composition root
            passes the recorder in -- see :class:`~jarvis.core.events.UsageEvent`.
    """

    def __init__(
        self,
        settings_provider: Callable[[], LlmSection],
        *,
        transport: HttpTransport | None = None,
        environ: Mapping[str, str] | None = None,
        usage_sink: Callable[[UsageEvent], None] | None = None,
    ) -> None:
        self._settings_provider = settings_provider
        self._transport = transport
        self._environ = environ
        self._usage_sink = usage_sink
        self._override: LlmSection | None = None
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
            if env.get(provider.api_key_env, "").strip():
                continue
            if provider.key_optional:
                # A local server without a credential is the intended shape, not a
                # problem to warn about every start. Still said once, at a lower level,
                # so "why isn't it answering" has a line to point at.
                logger.info(
                    "provider '%s' is configured to answer without a key (%s)",
                    provider.name,
                    provider.base_url,
                )
                continue
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

    def set_section_override(self, section: LlmSection | None) -> None:
        """Replace the effective ``llm`` section at runtime, or clear the override.

        Only the settings service calls this, and only for the four keys the window
        is allowed to change (endpoint, model, provider). It is an override rather
        than a mutation because the config tree is frozen on purpose -- and because
        a client bakes ``base_url`` into its endpoint at construction, so changing
        the address without rebuilding would silently keep calling the old one.
        """
        self._override = section
        self._clients.clear()

    @property
    def has_override(self) -> bool:
        return self._override is not None

    def attach_usage_sink(self, sink: Callable[[UsageEvent], None] | None) -> None:
        """Start recording token usage, after the service was already built.

        The awkward ordering is structural, not an oversight: the ledger lives in
        the database, the database path comes from configuration, and this service
        is constructed from that same configuration -- so the sink can only be
        wired once the store exists, which is after construction. Clients are built
        lazily, so calling this before the first request costs nothing; calling it
        later drops the cache rather than keeping a client that cannot count.
        """
        self._usage_sink = sink
        self._clients.clear()

    # -- client access -------------------------------------------------------

    @property
    def client(self) -> LlmClient:
        """Client for the configured default provider."""
        section = self._require_started()
        return self.client_for(section.default_provider)

    def client_for(self, provider_name: str, model_id: str = "") -> LlmClient:
        """Client for one provider/model pair (cached per pair).

        The model is part of the cache key, not just the provider: one endpoint
        serves several models, and a cache keyed by provider alone would hand a
        request for the small model a client wired to the big one.

        Args:
            provider_name: Key under ``llm.providers``.
            model_id: Which of that provider's models. Blank means its default.

        Raises:
            ConfigurationError: unknown provider name.
        """
        section = self._require_started()
        provider = section.providers.get(provider_name)
        if provider is None:
            raise ConfigurationError(
                f"unknown LLM provider: '{provider_name}'",
                details={"configured": sorted(section.providers)},
            )
        spec = provider.model_spec(model_id)
        key = f"{provider_name}\x00{spec.id}"
        cached = self._clients.get(key)
        if cached is not None:
            return cached
        client = self._build_client(section, provider, spec)
        self._clients[key] = client
        return client

    def _require_started(self) -> LlmSection:
        if self._section is None:
            raise ConfigurationError("LlmService is not started")
        return self._override if self._override is not None else self._section

    def _build_client(
        self, section: LlmSection, provider: ProviderSection, spec: ModelSpec
    ) -> LlmClient:
        settings = OpenAiCompatSettings(
            provider_name=provider.name,
            base_url=provider.base_url,
            model=spec.id,
            api_key_env=provider.api_key_env,
            timeout_seconds=section.timeout_seconds,
            max_retries=section.max_retries,
            retry_backoff_seconds=section.retry_backoff_seconds,
            cost_input_per_1m=provider.cost_input_per_1m,
            cost_output_per_1m=provider.cost_output_per_1m,
            key_optional=provider.key_optional,
        )
        if self._transport is not None:
            return OpenAiCompatClient(
                settings,
                self._transport,
                environ=self._environ,
                on_usage=self._usage_sink,
            )
        return OpenAiCompatClient(settings, environ=self._environ, on_usage=self._usage_sink)
