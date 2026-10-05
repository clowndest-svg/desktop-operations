"""Settings service: what the HUD's gear button can actually change.

Why an override layer instead of making configuration mutable
------------------------------------------------------------
``AppConfig`` and every section under it are frozen dataclasses on purpose: a
request path that can be handed a different value halfway through is a class of
bug this codebase does not want. So the config tree stays immutable, and anything
the operator changes from the window lives here as a small, explicit override that
is re-applied at boot. Precedence is fixed and narrow:

    preferences.json (these four keys only)  >  config.yaml  >  defaults.yaml

Only these four keys can be overridden. Everything else stays a config file edit,
because "the UI can change any setting" would put a second, invisible copy of the
whole configuration into a JSON file nobody reviews.

Secrets
-------
The API key is never stored by this module. It is written into the running
process's environment (which is what the client reads, per request, so it takes
effect immediately) and optionally into the Windows user environment so it survives
a reboot. It is never written to a file this application owns, and never returned
to the page -- the screen is told the *variable name* and whether it is set.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from jarvis.app.preferences import (
    CHAT_HISTORY_TURNS,
    LLM_BASE_URL,
    LLM_EXTRAS,
    LLM_HIDDEN,
    LLM_MODEL,
    LLM_OVERRIDES,
    LLM_PROVIDER,
    LLM_THINKING,
    LLM_THINKING_BUDGET,
    LLM_TUNING,
    SETTINGS_AI_EDITED,
    TELEMETRY_INTERVAL_MS,
    THINKING_LOADER,
    THINKING_LOADERS,
    VOICE_AUTO_ARM,
    VOICE_AUTO_SPEAK_TYPED,
    WAKE_GREETING,
    Preferences,
    thinking_loader,
)
from jarvis.app.wake_greeting import DEFAULT_GREETING, MAX_GREETING_CHARS
from jarvis.config.schema import LlmSection, ModelSpec, ProviderSection
from jarvis.llm.service import LlmService

logger = logging.getLogger("jarvis.app.settings_service")

CLOUD_VOICE_PROVIDER = "voice_cloud"
"""The pseudo-provider name that identifies the cloud-voice key in a patch.

Not an LLM provider -- it is a name the settings screen can pass as ``key_for``
so that one ``api_key`` field serves both kinds of key. Stored in the same place
as any other key, because a secret belongs in the environment and not in
``preferences.json``.
"""

CLOUD_VOICE_KEY_ENV = "DASHSCOPE_API_KEY"
"""Must match :data:`jarvis.tts.cloud.DEFAULT_KEY_ENV`. Asserted in the tests --
the panel writes a name and the client reads a name, and two spellings of one
variable is a key that silently never takes effect."""

MAX_BASE_URL_CHARS = 300
MAX_MODEL_CHARS = 150
TELEMETRY_INTERVAL_BOUNDS = (500, 60_000)
"""Floor and ceiling for the HUD's poll interval, in milliseconds.

The ceiling matters more than the floor: the settings screen is reachable by
anyone who can see the window, and "poll every 10 ms" is a way to accidentally
take a laptop down with you.
"""

THINKING_BUDGET_BOUNDS = (64, 16_000)
"""Tokens of reasoning one answer may spend, when thinking is switched on.

Not a 高/中/低 label. The endpoint behind the qwenai key truncates ``reasoning_content``
at the number it is given while still answering in full, so this range is measured in
the same unit the reply reports back in ``reasoning_tokens`` -- which is how the
operator can tell the knob is not decoration.
"""

DEFAULT_THINKING_BUDGET = 2048

THINKING_LEVELS: Mapping[str, int] = {
    "off": 0,
    "low": 1024,
    "medium": 4096,
    "high": 16_000,
}
"""Named reasoning budgets, because a raw token count is a knob nobody can read.

Same unit as ``THINKING_BUDGET_BOUNDS`` and as the ``reasoning_tokens`` the reply
reports, so a level is a shortcut to a number rather than a second, incompatible
scale. ``off`` is zero, which means "send no thinking parameter at all".
"""

THINKING_LEVEL_ORDER: tuple[str, ...] = ("off", "low", "medium", "high")
"""The levels in the order a picker should show them."""

DEFAULT_THINKING_LEVEL = "medium"

HISTORY_TURNS_BOUNDS = (0, 50)
"""Exchanges replayed into each request. Zero means "answer from this turn alone".

The ceiling is what a page left open all day would otherwise grow to: context costs on
every request, not once, so the bound is the thing that keeps the panel honest about
what turning it up is for.
"""

DEFAULT_HISTORY_TURNS = 10
"""What the panel starts on, and what an unreadable stored value falls back to."""

AI_VISIBLE_SETTINGS: frozenset[str] = frozenset(
    {
        "thinking_enabled",
        "thinking_budget",
        "history_turns",
        "telemetry_interval_ms",
        "auto_speak_typed",
    }
)
"""Which settings the assistant's own tools may change, and therefore may be tagged.

The list is a whitelist twice over: a tool can only write a key in it, and only a key
in it can ever carry the 「小夜改的」 mark. A stale name left in the stored list cannot
make the panel claim she touched something she has no door to.

``tts.voice`` is deliberately absent: ``voice_pick`` writes that preference through
:class:`~jarvis.app.voice_picker` and never through this ``apply`` path, so listing it
here would describe a route that does not exist. That change is audible on the next
sentence, which is its own feedback.
"""


class SettingsService:
    """The writable surface behind the settings panel."""

    name = "settings"

    def __init__(
        self,
        preferences: Preferences,
        llm_service: LlmService,
        section_provider: Callable[[], LlmSection],
        *,
        environ: dict[str, str] | None = None,
        persist_env: Callable[[str, str | None], bool] | None = None,
        alerts: Any | None = None,
        wake_words: Any | None = None,
    ) -> None:
        """Create the service.

        Args:
            preferences: Where the non-secret overrides live.
            llm_service: The client factory that has to be told when something
                it baked into a client has changed.
            section_provider: Returns the validated ``llm`` section; called lazily
                because configuration is not loaded at registration time.
            environ: The environment mapping the running process reads keys from.
                Defaults to ``os.environ`` and is injectable so tests never touch
                the real one.
            persist_env: How a key is made to survive a reboot. Injectable for the
                same reason -- the real implementation touches the registry.
        """
        self._prefs = preferences
        self._llm = llm_service
        self._section_provider = section_provider
        self._alerts = alerts
        """The alert centre, when one is wired. Its thresholds are settings like any other,
        and the panel must not grow a second save path for them."""
        self._wake_words = wake_words
        """The wake-word layer above ``wakeword.keywords``. ``None`` means the panel shows
        no box for it, rather than a box that saves somewhere the engine never reads."""
        self._environ = environ if environ is not None else os.environ
        self._persist_env = persist_env if persist_env is not None else write_user_env_var
        self._lock = threading.Lock()
        self._pending_target = ""
        """Which model row a flat ``base_url``/``model`` field in the current patch edits.

        Set once per :meth:`apply` from the patch's own ``target``: the fields arrive
        flat while the storage is per row, and guessing the row from "whatever is
        selected" would rewrite the wrong model when the panel saves a new row and
        a selection change in the same click."""

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Re-apply whatever the operator saved last time, before any client exists."""
        self._migrate_global_overrides()
        self._apply_override()

    def _migrate_global_overrides(self) -> None:
        """Fold the old single-row override into the per-provider map, once.

        Earlier versions stored one address and one model name for whichever provider
        was selected, and *deleted them on provider switch* -- editing one model row and
        then looking at another lost the edit. Rows that survived get moved under the
        provider they belonged to.

        The marker is ``llm.base_url``: nothing writes that key any more, so its presence
        means the file predates per-provider rows, and forgetting it here is what makes
        this run exactly once.

        ``llm.model`` is deliberately **not** part of the test. It used to be, and by then
        that key had taken a second job -- it is the model the chat is currently using --
        so this function ran on every start, rewrote the table from the validated copy,
        and reset the operator's current selection. A row of four added models came back
        as one after a restart, which is the bug this paragraph exists to keep out.
        """
        url = self._prefs.text(LLM_BASE_URL)
        if not url:
            return
        target = self._prefs.text(LLM_PROVIDER) or self._safe_section().default_provider
        overrides = self._overrides()
        row = overrides.setdefault(target, {})
        if _valid_url(url):
            row.setdefault("base_url", url)
        model = self._prefs.text(LLM_MODEL)
        if model and _valid_model(model) and not row.get("models"):
            row.setdefault("model", model)
        self._prefs.set(LLM_OVERRIDES, overrides)
        self._prefs.forget(LLM_BASE_URL)
        logger.info("folded the old global llm override into provider %r", target)

    def stop(self) -> None:
        return None

    # -- reads ---------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Everything the settings screen shows. Never contains a key value.

        ``api_key_variable`` is a name, not a secret, and ``api_key_set`` is a
        boolean; that is all the page gets. Echoing a key back to a webview would
        put it in the DOM, in devtools, and in any screenshot of this window.
        """
        section = self._effective_section()
        provider_name = section.default_provider
        provider = section.providers.get(provider_name)
        variable = provider.api_key_env if provider is not None else ""
        configured = set(self._safe_section().providers)
        overrides = self._overrides()
        return {
            "error": "",
            "providers": sorted(section.providers),
            "models": [
                {
                    "name": name,
                    "base_url": row.base_url,
                    "model": row.model,
                    "models": _serialise_models(row.models),
                    "default_model": row.default_model,
                    "key_env": row.api_key_env,
                    "key_set": bool(self._environ.get(row.api_key_env, "").strip()),
                    "source": "配置文件" if name in configured else "界面添加",
                    "edited": name in overrides,
                    "current": name == provider_name,
                }
                for name, row in sorted(section.providers.items())
            ],
            "provider": provider_name,
            "hidden_models": self._hidden_rows(section),
            "base_url": provider.base_url if provider is not None else "",
            "model": provider.model if provider is not None else "",
            "api_key_variable": variable,
            "api_key_set": bool(self._environ.get(variable, "").strip()),
            "cloud_voice_key_variable": CLOUD_VOICE_KEY_ENV,
            "cloud_voice_key_set": bool(self._environ.get(CLOUD_VOICE_KEY_ENV, "").strip()),
            "overrides_active": self._override_summary(),
            "voice_auto_arm": self._prefs.flag(VOICE_AUTO_ARM, default=False),
            "auto_speak_typed": self._prefs.flag(VOICE_AUTO_SPEAK_TYPED, default=True),
            "telemetry_interval_ms": self._prefs.number(TELEMETRY_INTERVAL_MS, default=1500),
            "thinking_enabled": self.thinking_enabled(),
            "thinking_budget": self.thinking_budget(),
            "thinking_budget_bounds": list(THINKING_BUDGET_BOUNDS),
            "history_turns": self.history_turns(),
            "history_turns_bounds": list(HISTORY_TURNS_BOUNDS),
            "ai_edited": self.ai_edited(),
            "wake_greeting": self.wake_greeting(),
            "wake_greeting_default": DEFAULT_GREETING,
            "wake_greeting_max": MAX_GREETING_CHARS,
            "thinking_loader": self.thinking_loader(),
            "thinking_loader_choices": list(THINKING_LOADERS),
            "alerts": self._alerts.settings() if self._alerts is not None else {},
            **(self._wake_words.settings() if self._wake_words is not None else {}),
        }

    # -- writes --------------------------------------------------------------

    def selected_pair(self) -> tuple[str, str]:
        """The (provider, model) the window last picked, without building the menu.

        A conversation with no model of its own inherits this, so it has to be cheap
        enough to read once per turn. ``model_choices`` answers the same question and
        builds every provider, every model and every key-presence flag on the way, which
        is the right shape for the picker and the wrong shape for a request path.
        """
        section = self._effective_section()
        provider_name = section.default_provider
        provider = section.providers.get(provider_name)
        if provider is None:
            return provider_name, ""
        return provider_name, provider.model_spec(self._prefs.text(LLM_MODEL)).id

    def model_choices(self) -> dict[str, Any]:
        """Every configured provider with its models, for the chat header's pickers.

        Two levels rather than one flat list, because that is the shape of the data:
        a key belongs to a provider, and a provider offers several models. A flat
        list would repeat the provider on every row and still leave the page to
        group them anyway.

        Each row carries whether its key is actually present. A dropdown that offers
        a model which cannot answer is worse than one that hides it: the operator
        picks it, asks a question, and gets "API key 未设置" as the first thing out of
        the new choice. So the choice is offered but marked, and the reason is the
        environment variable's *name* -- never its value.
        """
        section = self._effective_section()
        provider_name = section.default_provider
        provider = section.providers.get(provider_name)
        model_id = provider.model_spec(self._prefs.text(LLM_MODEL)).id if provider else ""
        tuning = self.tuning_for(provider_name, model_id)
        return {
            "error": "",
            "providers": [
                {
                    "name": name,
                    "base_url": row.base_url,
                    "models": _serialise_models(row.models),
                    "default_model": row.default_model,
                    "key_set": bool(self._environ.get(row.api_key_env, "").strip()),
                    "key_variable": row.api_key_env,
                    "current": name == provider_name,
                }
                for name, row in sorted(section.providers.items())
            ],
            "provider": provider_name,
            "model": model_id,
            "thinking": tuning["thinking"],
            "turns": tuning["turns"],
            "thinking_levels": list(THINKING_LEVEL_ORDER),
            "turns_bounds": list(HISTORY_TURNS_BOUNDS),
        }

    # -- per-model tuning: thinking level and context size -------------------

    def tuning_for(self, provider: str, model: str) -> dict[str, Any]:
        """The thinking level and context size remembered for one provider/model.

        Read through a stored table rather than one key per setting because the pair
        is the identity: the answer to "how hard should this model think" is only
        meaningful next to the model it is about.
        """
        stored = self._prefs.get(LLM_TUNING)
        row = stored.get(_tuning_key(provider, model)) if isinstance(stored, dict) else None
        if not isinstance(row, dict):
            return {"thinking": DEFAULT_THINKING_LEVEL, "turns": self.history_turns()}
        level = str(row.get("thinking") or "").strip()
        if level not in THINKING_LEVELS:
            level = DEFAULT_THINKING_LEVEL
        try:
            turns = int(row.get("turns", self.history_turns()))
        except (TypeError, ValueError):
            turns = self.history_turns()
        low, high = HISTORY_TURNS_BOUNDS
        return {"thinking": level, "turns": max(low, min(high, turns))}

    def set_tuning(self, thinking: object = None, turns: object = None) -> dict[str, Any]:
        """Remember a thinking level and/or context size for the current model.

        Writes only what it was given: the two knobs live in the chat header next to
        each other, and a caller changing one must not silently reset the other to
        its default.
        """
        section = self._effective_section()
        provider_name = section.default_provider
        provider = section.providers.get(provider_name)
        model_id = provider.model_spec(self._prefs.text(LLM_MODEL)).id if provider else ""
        current = self.tuning_for(provider_name, model_id)
        level = current["thinking"]
        if thinking is not None:
            candidate = str(thinking or "").strip()
            if candidate not in THINKING_LEVELS:
                return {"ok": False, "error": f"未知的思考强度：{candidate}", **current}
            level = candidate
        budget = current["turns"]
        if turns is not None:
            # Typed before it is converted: the value arrives from a spinner in the
            # page, so it is ``object`` here, and mypy is right that ``int()`` on an
            # arbitrary object is not a conversion but a guess. A bool is excluded
            # explicitly -- ``True`` would otherwise become a context of one turn.
            if isinstance(turns, bool) or not isinstance(turns, (int, float, str)):
                return {"ok": False, "error": "上下文轮数必须是整数", **current}
            try:
                budget = int(turns)
            except (TypeError, ValueError):
                return {"ok": False, "error": "上下文轮数必须是整数", **current}
            low, high = HISTORY_TURNS_BOUNDS
            if not low <= budget <= high:
                return {"ok": False, "error": f"上下文轮数需在 {low}–{high} 之间", **current}
        stored = self._prefs.get(LLM_TUNING)
        table = dict(stored) if isinstance(stored, dict) else {}
        table[_tuning_key(provider_name, model_id)] = {"thinking": level, "turns": budget}
        self._prefs.set(LLM_TUNING, table)
        # The chat service reads the level per turn, so the next question is already
        # answered with the new one -- no restart, which is the whole point of a
        # dropdown next to the input box.
        self._apply_thinking(level)
        return {"ok": True, "error": "", "thinking": level, "turns": budget}

    def choose_model(self, provider_name: object, model_id: object = None) -> dict[str, Any]:
        """Point the assistant at another provider and/or model.

        Both levels in one call because the page always knows both: picking a
        provider whose saved model no longer exists has to land somewhere, and
        "somewhere" is that provider's default.
        """
        section = self._effective_section()
        name = str(provider_name or "").strip()
        provider = section.providers.get(name)
        if provider is None:
            return {"ok": False, "error": f"没有这个服务商：{name}", "provider": "", "model": ""}
        spec = provider.model_spec(str(model_id or ""))
        self._prefs.set(LLM_PROVIDER, name)
        self._prefs.set(LLM_MODEL, spec.id)
        self._apply_override()
        tuning = self.tuning_for(name, spec.id)
        self._apply_thinking(str(tuning["thinking"]))
        return {"ok": True, "error": "", "provider": name, "model": spec.id, **tuning}

    # -- editing the model list from the window ------------------------------

    def add_model(
        self, provider_name: object, model_id: object, label: object = ""
    ) -> dict[str, Any]:
        """Add a model to a provider's list, as a window edit.

        Stored as an override rather than written into config.yaml: the file is the
        operator's, and a program that rewrites it is a program that can corrupt it.
        """
        name = str(provider_name or "").strip()
        candidate = str(model_id or "").strip()
        if not _valid_model(candidate):
            return {"ok": False, "error": "模型名不能为空、不能带空格，也不能太长", "models": []}
        section = self._effective_section()
        provider = section.providers.get(name)
        if provider is None:
            return {"ok": False, "error": f"没有这个服务商：{name}", "models": []}
        specs = list(provider.models)
        if any(spec.id == candidate for spec in specs):
            return {"ok": False, "error": f"这个服务商里已经有 {candidate} 了", "models": []}
        specs.append(ModelSpec(id=candidate, label=str(label or "").strip()))
        self._write_models(name, tuple(specs), provider.default_model)
        return {
            "ok": True,
            "error": "",
            "models": _serialise_models(tuple(specs)),
            "default_model": provider.default_model,
        }

    def remove_model(self, provider_name: object, model_id: object) -> dict[str, Any]:
        """Remove a model from a provider's list, as a window edit.

        Refuses to empty a provider: a provider with no models is one the picker
        cannot show and the client factory cannot build, so the last row is a floor
        rather than a suggestion.
        """
        name = str(provider_name or "").strip()
        target = str(model_id or "").strip()
        section = self._effective_section()
        provider = section.providers.get(name)
        if provider is None:
            return {"ok": False, "error": f"没有这个服务商：{name}", "models": []}
        specs = tuple(spec for spec in provider.models if spec.id != target)
        if len(specs) == len(provider.models):
            return {"ok": False, "error": f"这个服务商里没有 {target}", "models": []}
        if not specs:
            return {
                "ok": False,
                "error": "至少要留一个模型",
                "models": _serialise_models(provider.models),
            }
        default_model = _default_of(specs, provider.default_model)
        self._write_models(name, specs, default_model)
        if self._prefs.text(LLM_MODEL) == target:
            self._prefs.set(LLM_MODEL, default_model)
            self._apply_override()
        return {
            "ok": True,
            "error": "",
            "models": _serialise_models(specs),
            "default_model": default_model,
        }

    def _write_models(
        self, provider_name: str, specs: tuple[ModelSpec, ...], default_model: str
    ) -> None:
        """Persist a provider's model list into the window's override table."""
        raw = self._prefs.get(LLM_OVERRIDES)
        table: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        row: dict[str, Any] = dict(table.get(provider_name) or {})
        row["models"] = _serialise_models(specs)
        row["default_model"] = default_model
        # The single-model field is dropped rather than left behind: two spellings of
        # the same list is how the panel and the picker end up showing different sets.
        row.pop("model", None)
        table[provider_name] = row
        self._prefs.set(LLM_OVERRIDES, table)
        self._apply_override()

    def _apply_thinking(self, level: str) -> None:
        """Push a thinking level into the preferences the chat service reads.

        The level is stored per model, but the request only understands
        ``(enabled, budget)``, so the mapping happens once, here, rather than in every
        reader.
        """
        budget = THINKING_LEVELS.get(level, THINKING_LEVELS[DEFAULT_THINKING_LEVEL])
        self._prefs.set_flag(LLM_THINKING, budget > 0)
        self._prefs.set(LLM_THINKING_BUDGET, budget)

    def speaks_typed(self) -> bool:
        """Whether a typed answer should also be read aloud.

        Read here rather than cached in the page: the toggle is in the settings
        panel, and a bridge that consulted a copy taken at mount would keep doing
        the opposite of what the operator just clicked.
        """
        return self._prefs.flag(VOICE_AUTO_SPEAK_TYPED, default=True)

    def thinking_enabled(self) -> bool:
        """Whether the next request asks the model to show its reasoning."""
        return self._prefs.flag(LLM_THINKING, default=False)

    def thinking_budget(self) -> int:
        """How many tokens of reasoning that request is allowed to spend."""
        return self._prefs.number(LLM_THINKING_BUDGET, default=DEFAULT_THINKING_BUDGET)

    def history_turns(self) -> int:
        """How many past exchanges the chat replays into each request."""
        return self._prefs.number(CHAT_HISTORY_TURNS, default=DEFAULT_HISTORY_TURNS)

    def wake_greeting(self) -> str:
        """The sentence spoken on the wake word. ``""`` means she stays quiet.

        Read per wake rather than handed to the pipeline once, so editing the field and
        pressing the wake word is enough -- no restart, and no second copy of the text
        cached somewhere that keeps saying the old sentence.
        """
        stored = self._prefs.text(WAKE_GREETING, default=DEFAULT_GREETING)
        return stored.strip()[:MAX_GREETING_CHARS]

    def thinking_loader(self) -> str:
        """Which animation the two 「思考中」 surfaces show. Read per use."""
        return thinking_loader(self._prefs.text(THINKING_LOADER, default=""))

    def ai_edited(self) -> list[str]:
        """Settings the assistant moved that no person has confirmed since.

        The condition the plan attached to letting her edit her own configuration: a
        number that changed on its own has to say so where it is shown, or the operator
        finds 「上下文轮数 = 2」 with no idea who decided that.
        """
        raw = self._prefs.get(SETTINGS_AI_EDITED)
        if not isinstance(raw, list):
            return []
        return [str(key) for key in raw if isinstance(key, str) and key in AI_VISIBLE_SETTINGS]

    def apply(self, patch: dict[str, Any], *, by: str = "operator") -> dict[str, Any]:
        """Validate and save whatever the screen sent back.

        Returns a fresh snapshot plus a per-field verdict, because a settings
        dialog that silently drops the one field it rejected is worse than one that
        refuses the whole save.

        ``by`` says who asked. The page sends ``operator`` (the default) and the
        assistant's own tool sends ``model``, which tags every key it actually moved so
        the panel can mark it. A person saving the same key clears the tag: once a human
        set it, it is no longer something she did behind their back.
        """
        problems: dict[str, str] = {}
        applied: dict[str, Any] = {}
        with self._lock:
            self._pending_target = str(patch.get("target") or patch.get("provider") or "")
            for key, value in patch.items():
                if key in ("target", "key_for"):
                    # Addressing fields, not settings: they say which row the rest
                    # of the patch edits and must not come back as "unknown field".
                    continue
                if key == "api_key":
                    key_for = str(patch.get("key_for") or "").strip()
                    outcome = self._store_api_key(value, key_for or None)
                    if outcome:
                        applied["api_key"] = outcome
                    else:
                        problems["api_key"] = "密钥为空或写入失败；界面不会回显它的值"
                    continue
                verdict = self._apply_one(key, value)
                if verdict is None:
                    applied[key] = value
                else:
                    problems[key] = verdict
            self._note_who_applied(sorted(applied), by)
            self._pending_target = ""
            self._apply_override()
        snapshot = self.snapshot()
        snapshot["applied"] = applied
        snapshot["problems"] = problems
        snapshot["error"] = "; ".join(f"{k}: {v}" for k, v in problems.items())
        return snapshot

    def _note_who_applied(self, keys: Sequence[str], by: str) -> None:
        """Tag what the assistant changed; clear the tag when a person confirmed it.

        Only the keys that actually moved are touched, so a patch the panel sends on
        every save -- including the fields nobody edited -- does not mark a setting as
        hers because a checkbox was re-sent with its own current value.
        """
        if not keys:
            return
        marked = set(self.ai_edited())
        if by == "model":
            marked.update(keys)
        else:
            marked.difference_update(keys)
        kept = sorted(key for key in marked if key in AI_VISIBLE_SETTINGS)
        if kept == self.ai_edited():
            return
        self._prefs.set(SETTINGS_AI_EDITED, kept)

    def _apply_one(self, key: str, value: Any) -> str | None:
        """Save one non-secret field. Returns a reason string, or None on success."""
        if key == "provider":
            return self._set_provider(value)
        if key == "base_url":
            return self._set_row_field(
                "base_url", value, _valid_url, "地址必须以 http:// 或 https:// 开头"
            )
        if key == "model":
            return self._set_row_field(
                "model", value, _valid_model, "模型名不能含空格且不超过 150 字符"
            )
        if key == "add_model":
            return self._add_model(value)
        if key == "remove_model":
            return self._remove_model(value)
        if key in ("hide_provider", "show_provider"):
            return self._set_provider_hidden(key, value)
        if key == "auto_speak_typed":
            self._prefs.set(VOICE_AUTO_SPEAK_TYPED, bool(value))
            return None
        if key == "telemetry_interval_ms":
            return self._set_interval(value)
        if key == "thinking_enabled":
            self._prefs.set(LLM_THINKING, bool(value))
            return None
        if key == "thinking_budget":
            return self._set_in_range(
                LLM_THINKING_BUDGET,
                value,
                THINKING_BUDGET_BOUNDS,
                "思考预算",
                "token",
            )
        if key == "history_turns":
            return self._set_in_range(
                CHAT_HISTORY_TURNS, value, HISTORY_TURNS_BOUNDS, "上下文轮数", "轮"
            )
        if key == "wake_greeting":
            return self._set_greeting(value)
        if key == "thinking_loader":
            return self._set_loader(value)
        if key in ("alerts_rules", "alerts_cooldown_minutes", "alerts_speak_critical"):
            return self._apply_alerts(key, value)
        if key == "wake_keywords":
            if self._wake_words is None:
                return "这台机器没有接唤醒词设置"
            verdict: str | None = self._wake_words.apply(value)
            return verdict
        return "未知设置项"

    def _apply_alerts(self, key: str, value: Any) -> str | None:
        """Hand the alert section to the engine that owns its validation.

        Delegated rather than re-checked here because the rules that make a threshold
        usable -- the per-rule range, the whole-patch-or-nothing, the bounds on the
        cooldown -- belong next to the code that reads them. Two copies of a range is two
        ways for the panel to say 保存成功 while the engine quietly kept the old line.
        """
        if self._alerts is None:
            return "告警中心未启用"
        verdict: str | None = self._alerts.apply_settings({key: value})
        return verdict

    def _set_loader(self, value: Any) -> str | None:
        """Store the loader kind, refusing one the pages cannot draw.

        A typo here would otherwise mean the setting reports success while both
        surfaces quietly keep the default -- the kind of write that looks applied and
        is not.
        """
        kind = str(value or "").strip()
        if kind not in THINKING_LOADERS:
            return f"没有这种思考动效：{kind or '（空）'}"
        self._prefs.set(THINKING_LOADER, kind)
        return None

    def _set_greeting(self, value: Any) -> str | None:
        """Store the wake sentence. Empty is a real answer: it means stay quiet.

        The ceiling is not politeness -- the whole sentence is spoken before the
        microphone opens again, so a long value is a long wait after the wake word
        rather than a longer greeting.
        """
        text = str(value or "").strip()[:MAX_GREETING_CHARS]
        self._prefs.set(WAKE_GREETING, text)
        return None

    def _set_in_range(
        self, key: str, value: Any, bounds: tuple[int, int], label: str, unit: str
    ) -> str | None:
        """Store one bounded integer choice, or say why the number was refused.

        Shared by the two new knobs because a range checked in three places is a range
        that drifts: the panel has to advertise the same bounds this writes.
        """
        low, high = bounds
        try:
            number = int(value)
        except (TypeError, ValueError):
            return f"{label}必须是整数{unit}数"
        if not low <= number <= high:
            return f"{label}只能在 {low}–{high} {unit}之间"
        self._prefs.set(key, number)
        return None

    def _set_provider(self, value: Any) -> str | None:
        name = str(value or "").strip()
        if name not in self._effective_section().providers:
            return f"未配置的 provider：{name!r}"
        # Per-row edits live under each provider's own key now, so switching rows
        # loses nothing: the address typed for one model stays with that model.
        self._prefs.set(LLM_PROVIDER, name)
        return None

    def _set_row_field(
        self, field: str, value: Any, check: Callable[[str], bool], why: str
    ) -> str | None:
        """Edit one field of one model row, named by ``target`` in the same patch."""
        text = str(value or "").strip()
        if not text or not check(text):
            return why
        target = self._pending_target or self._effective_section().default_provider
        if target not in self._effective_section().providers:
            return f"未配置的模型：{target!r}"
        overrides = self._overrides()
        overrides.setdefault(target, {})[field] = text
        self._prefs.set(LLM_OVERRIDES, overrides)
        return None

    def _add_model(self, value: Any) -> str | None:
        """Add model rows that do not exist in config.yaml.

        One row or a list of them: the panel lets an operator queue several providers
        before pressing 保存, because adding one at a time meant opening the dialog,
        filling four fields, saving, and reopening it -- six clicks per provider. The
        batch goes out in **one** write on purpose: three separate writes means the
        second one's failure leaves the first two stored while the screen reports a
        single save.
        """
        rows = value if isinstance(value, list) else [value]
        if not rows:
            return "没有要添加的服务商"
        extras = self._extras()
        # The effective list is not enough here: it filters the rows the operator hid,
        # and adding a second row under a hidden name would then pass this check only to
        # be shadowed by the file's row the moment somebody brings it back.
        known = {str(entry["name"]) for entry in extras}
        known |= set(self._safe_section().providers)
        known |= set(self._hidden())
        pending: list[dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                return "新增模型需要一个 {name, base_url, model} 对象"
            name = str(item.get("name") or "").strip()
            base_url = str(item.get("base_url") or "").strip()
            model = str(item.get("model") or "").strip()
            if not _valid_name(name):
                return "模型名字只能是小写字母、数字、- 和 _，1-32 位"
            if not _valid_url(base_url):
                return f"{name}：地址必须以 http:// 或 https:// 开头"
            if not _valid_model(model):
                return f"{name}：模型名不能含空格且不超过 150 字符"
            if name in known:
                return f"已经有叫 {name!r} 的模型；要改它就在那一行上改"
            known.add(name)
            pending.append(
                {"name": name, "base_url": base_url, "model": model, "key_env": key_env_for(name)}
            )
        if not self._prefs.set(LLM_EXTRAS, extras + pending):
            # Said out loud because the alternative is the bug this line was written for:
            # the write was refused, yet the panel had already been told 已保存.
            names = "、".join(str(entry["name"]) for entry in pending)
            return f"偏好文件拒绝了这次写入，{names} 没有加上"
        dropped: list[str] = []
        for item, entry in zip(rows, pending, strict=True):
            key = str(item.get("api_key") or "").strip()
            if key and self._store_api_key(key, str(entry["name"])) is None:
                dropped.append(str(entry["name"]))
        if dropped:
            return "服务商加上了，但这几家的 Key 没能写进环境变量：" + "、".join(dropped)
        return None

    def _remove_model(self, value: Any) -> str | None:
        """Delete a window-added row. Rows from config.yaml are the file's business."""
        name = str(value or "").strip()
        before = self._extras()
        extras = [entry for entry in before if entry["name"] != name]
        if len(extras) == len(before):
            return f"{name!r} 不是界面添加的模型；配置文件里的行请去 config.yaml 删"
        if not self._prefs.set(LLM_EXTRAS, extras):
            return f"偏好文件拒绝了这次写入，{name} 还留着"
        overrides = self._overrides()
        overrides.pop(name, None)
        self._prefs.set(LLM_OVERRIDES, overrides)
        if self._prefs.text(LLM_PROVIDER) == name:
            self._prefs.forget(LLM_PROVIDER)
        return None

    def _set_provider_hidden(self, key: str, value: Any) -> str | None:
        """Hide a row, or bring it back. Neither one touches ``config.yaml``.

        A person with eight providers in their file and two they use should not have to edit
        YAML to get a readable menu -- and deleting the others would be destructive in a way
        the panel has no business being. So this is a view switch, reversible from the same
        place, with the row in use refused: hiding what you are talking to is how a chat
        header ends up naming a provider no list contains.
        """
        name = str(value or "").strip()
        if not name:
            return "没有说清是哪一行服务商"
        hidden = set(self._hidden())
        if key == "hide_provider":
            if name not in self._effective_section().providers:
                return f"没有这一行服务商：{name}"
            if name == self._effective_section().default_provider:
                return f"「{name}」正在用，先切到别的服务商再藏"
            if name in hidden:
                return None
            hidden.add(name)
        else:
            if name not in hidden:
                return f"「{name}」没有被藏起来"
            hidden.discard(name)
        self._prefs.set(LLM_HIDDEN, sorted(hidden))
        return None

    def _set_interval(self, value: Any) -> str | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return "轮询间隔必须是整数毫秒"
        low, high = TELEMETRY_INTERVAL_BOUNDS
        if not low <= number <= high:
            return f"轮询间隔只能在 {low}–{high} 毫秒之间"
        self._prefs.set(TELEMETRY_INTERVAL_MS, number)
        return None

    def _store_api_key(self, value: Any, target: str | None = None) -> str | None:
        """Put the key where the client will find it, and nowhere else.

        Two destinations and no third: the live process environment (so the next
        request already works) and the Windows user environment (so a reboot keeps
        it). An empty value clears both.

        ``target`` names which key is being set. It is normally an LLM provider,
        but ``"voice_cloud"`` is also accepted: cloud cloned voices read
        ``DASHSCOPE_API_KEY``, and without a name for them the settings panel
        would have a field it could not save.
        """
        text = str(value or "")
        if target == CLOUD_VOICE_PROVIDER:
            variable = CLOUD_VOICE_KEY_ENV
        else:
            section = self._effective_section()
            name = target or section.default_provider
            provider = section.providers.get(name)
            variable = provider.api_key_env if provider is not None else ""
        if not variable:
            logger.warning("no API key variable configured; refusing to store a key")
            return None
        if not text.strip():
            self._environ.pop(variable, None)
            self._persist_env(variable, None)
            logger.info("cleared API key environment variable %s", variable)
            return "cleared"
        self._environ[variable] = text
        persisted = self._persist_env(variable, text)
        # Deliberately: the name, never the value, never the length.
        logger.info("API key stored in process environment (%s)", variable)
        return "set" if persisted else "set_for_this_run_only"

    # -- the override itself -------------------------------------------------

    def _effective_provider_name(self, section: LlmSection) -> str:
        saved = self._prefs.text(LLM_PROVIDER)
        if saved and saved in section.providers:
            return saved
        return section.default_provider

    # -- the model list: config rows + window-added rows + per-row edits -----

    def _extras(self) -> list[dict[str, Any]]:
        """Providers the window added, validated on the way out of storage.

        Re-validated at read time rather than trusted: preferences.json is a file
        the operator can open in Notepad, and a hand-edited row with a broken URL
        must degrade to "absent", not to a client pointed at nonsense.

        The rows come back in the shape they are *stored* in -- ``models`` as a list of
        ``{id, label}`` mappings, not ``ModelSpec`` objects -- for the same reason
        :meth:`_overrides` does: every caller here reads this table, appends or drops a
        row, and writes the whole thing back. A row carrying dataclasses cannot be
        serialised, so ``Preferences.set`` refuses it *before* touching anything, and
        the add is a silent no-op while the screen reports 「已保存并立即生效」.
        """
        raw = self._prefs.get(LLM_EXTRAS)
        if not isinstance(raw, list):
            return []
        kept: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            base_url = str(entry.get("base_url") or "").strip()
            specs = _stored_models(entry)
            if not (_valid_name(name) and _valid_url(base_url) and specs):
                continue
            if not all(_valid_model(spec.id) for spec in specs):
                continue
            kept.append(
                {
                    "name": name,
                    "base_url": base_url,
                    "models": _serialise_models(specs),
                    "default_model": _default_of(specs, entry.get("default_model")),
                    "key_env": str(entry.get("key_env") or key_env_for(name)),
                }
            )
        return kept

    def _overrides(self) -> dict[str, dict[str, Any]]:
        """Per-provider address/model edits made from the window.

        The rows are returned in the shape they are *stored* in -- model lists as a list
        of ``{id, label}`` mappings, not as ``ModelSpec`` objects. That is not tidiness:
        four callers read this table, mutate one field and write the whole thing back,
        and a row carrying dataclasses cannot be serialised. The write then fails inside
        :class:`~jarvis.app.preferences.Preferences`, which keeps the value in memory, and
        from that moment the running program and the file disagree -- a provider that
        shows one model on screen and four on disk.
        """
        raw = self._prefs.get(LLM_OVERRIDES)
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for name, fields in raw.items():
            if not isinstance(fields, dict):
                continue
            row: dict[str, Any] = {}
            url = str(fields.get("base_url") or "").strip()
            if _valid_url(url):
                row["base_url"] = url
            specs = _stored_models(fields)
            if specs and all(_valid_model(spec.id) for spec in specs):
                row["models"] = _serialise_models(specs)
                row["default_model"] = _default_of(specs, fields.get("default_model"))
            if row:
                out[str(name)] = row
        return out

    def _effective_section(self) -> LlmSection:
        """What the assistant actually talks to: config, plus the window's edits.

        One function on purpose. The dropdown, the snapshot and the override handed
        to :class:`~jarvis.llm.service.LlmService` must never disagree about which
        models exist, and three copies of this merge is three ways for them to.
        """
        base = self._safe_section()
        providers = dict(base.providers)
        for extra in self._extras():
            if extra["name"] in providers:
                # A config.yaml row outranks a window-added row of the same name:
                # silently shadowing the file is how an operator ends up talking to
                # an endpoint they cannot find in any document.
                logger.warning(
                    "ignoring window-added model %r: config.yaml already defines it",
                    extra["name"],
                )
                continue
            extra_specs = _stored_models(extra)
            if not extra_specs:
                # ``_extras`` already validated this row, so reaching here means the
                # stored shape changed underneath us. Dropping the row beats handing
                # the client a provider with no model to ask for.
                logger.warning("界面添加的模型 %r 没有可用的模型名，本次忽略", extra["name"])
                continue
            providers[extra["name"]] = ProviderSection(
                name=extra["name"],
                base_url=extra["base_url"],
                models=extra_specs,
                default_model=_default_of(extra_specs, extra.get("default_model")),
                api_key_env=extra["key_env"],
                cost_input_per_1m=0.0,
                cost_output_per_1m=0.0,
            )
        for name, row in self._overrides().items():
            existing = providers.get(name)
            if existing is None:
                continue
            specs: tuple[ModelSpec, ...] = _stored_models(row) or existing.models
            providers[name] = replace(
                existing,
                base_url=row.get("base_url", existing.base_url),
                models=specs,
                default_model=_default_of(specs, row.get("default_model", existing.default_model)),
            )
        saved = self._prefs.text(LLM_PROVIDER)
        default_provider = saved if saved in providers else base.default_provider
        providers = self._without_hidden(providers, keep=default_provider)
        return replace(base, providers=providers, default_provider=default_provider)

    def _without_hidden(
        self, providers: dict[str, ProviderSection], *, keep: str
    ) -> dict[str, ProviderSection]:
        """Drop the rows the operator hid, never the one in use.

        The row being answered with is exempt on purpose: a menu that hides the provider
        currently selected would leave the chat header naming something no list contains,
        and "she stopped answering" is a hard bug to trace back to a checkbox.
        """
        hidden = self._hidden()
        if not hidden:
            return providers
        kept = {name: row for name, row in providers.items() if name not in hidden or name == keep}
        dropped = sorted(set(providers) - set(kept))
        if dropped:
            logger.info("界面上藏起这些服务商：%s", "、".join(dropped))
        return kept

    def _hidden(self) -> set[str]:
        """The rows this window hides. Names only; nothing here deletes anything."""
        raw = self._prefs.get(LLM_HIDDEN)
        if not isinstance(raw, list):
            return set()
        return {str(item).strip() for item in raw if str(item).strip()}

    def hidden_names(self) -> tuple[str, ...]:
        """What this window hides. Names only; nothing here deletes anything."""
        return tuple(sorted(self._hidden()))

    def _hidden_rows(self, visible: LlmSection) -> list[dict[str, Any]]:
        """The hidden rows, so the panel can offer them back.

        Rebuilt from the same union :meth:`_effective_section` filters, because a hidden row
        is absent from what that returns -- a list of what is *not* shown cannot be read off
        the thing that stopped showing it.
        """
        hidden = self._hidden()
        if not hidden:
            return []
        configured = self._safe_section().providers
        extras = {str(entry["name"]): entry for entry in self._extras()}
        rows: list[dict[str, Any]] = []
        for name in sorted(hidden):
            row = configured.get(name)
            if row is None and name not in extras:
                # Named in the file but no longer a provider at all: stale, and said out loud
                # rather than drawn as a 找回 button that does nothing.
                logger.warning("llm.hidden 里的 %r 已经不是任何服务商，界面上不再列出", name)
                continue
            count = len(row.models) if row is not None else len(extras[name]["models"])
            rows.append(
                {
                    "name": name,
                    "source": "配置文件" if row is not None else "界面添加",
                    "models": count,
                    "current": name == visible.default_provider,
                }
            )
        return rows

    def _apply_override(self) -> None:
        """Hand the LLM service the effective section, or nothing when it is plain.

        Called at the end of every save, which is what makes "save" mean "now":
        the service drops its client cache on override, so the next request is
        built from the new address, model and key without a restart.
        """
        effective = self._effective_section()
        if effective == self._safe_section():
            self._llm.set_section_override(None)
            return
        self._llm.set_section_override(effective)

    def _override_summary(self) -> list[str]:
        labels = []
        if self._prefs.text(LLM_PROVIDER):
            labels.append("当前模型")
        labels.extend(f"{name} 的地址/模型名" for name in sorted(self._overrides()))
        labels.extend(f"新增模型 {entry['name']}" for entry in self._extras())
        return labels

    def _safe_section(self) -> LlmSection:
        """The configured section, or an empty stand-in when configuration failed.

        The settings screen must still open when the config file is broken -- that
        is precisely when somebody needs to fix the endpoint from the UI.
        """
        try:
            return self._section_provider()
        except Exception:  # any config failure must still let the panel open
            logger.exception("could not read llm configuration for the settings panel")
            return LlmSection(
                default_provider="",
                timeout_seconds=30.0,
                max_retries=2,
                retry_backoff_seconds=1.0,
                providers={},
            )


def _valid_url(text: str) -> bool:
    return len(text) <= MAX_BASE_URL_CHARS and (
        text.startswith("http://") or text.startswith("https://")
    )


def _valid_model(text: str) -> bool:
    return 0 < len(text) <= MAX_MODEL_CHARS and " " not in text


def _valid_name(text: str) -> bool:
    """A model row's name: short, slug-shaped, safe as an env-var prefix."""
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", text))


def _parse_models(raw: object, *, fallback: str = "") -> tuple[ModelSpec, ...]:
    """Parse a stored model list into specs, tolerating the older single-model shape.

    Preferences are written by this program, not by hand, so an old row is migrated
    silently -- the alternative is a service the operator added from the window
    quietly disappearing on upgrade. The strict "renamed, go fix it" treatment
    belongs to config.yaml, which a person does edit.
    """
    specs: list[ModelSpec] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str):
                text = entry.strip()
                if text:
                    specs.append(ModelSpec(id=text))
            elif isinstance(entry, dict):
                model_id = str(entry.get("id") or "").strip()
                if model_id:
                    specs.append(
                        ModelSpec(id=model_id, label=str(entry.get("label") or "").strip())
                    )
    if specs:
        return tuple(specs)
    text = str(fallback or "").strip()
    return (ModelSpec(id=text),) if text else ()


def _stored_models(fields: Mapping[str, object]) -> tuple[ModelSpec, ...]:
    """The models a stored provider row carries, older single-model shape included."""
    return _parse_models(fields.get("models"), fallback=str(fields.get("model") or ""))


def _default_of(specs: tuple[ModelSpec, ...], wanted: object) -> str:
    """Which of ``specs`` is the default: the named one when present, else the first."""
    name = str(wanted or "").strip()
    ids = [spec.id for spec in specs]
    return name if name in ids else (ids[0] if ids else "")


def _serialise_models(specs: tuple[ModelSpec, ...]) -> list[dict[str, str]]:
    """The shape stored in preferences and handed to the page."""
    return [{"id": spec.id, "label": spec.display} for spec in specs]


def _tuning_key(provider: str, model: str) -> str:
    """The tuning-table key for one provider/model pair.

    A NUL separator because neither name can contain one, so no two pairs can be
    spelled the same way -- a ``/`` separator would make ``a/b`` + ``c`` collide with
    ``a`` + ``b/c``.
    """
    return f"{provider}\x00{model}"


def key_env_for(name: str) -> str:
    """The environment variable a window-added model's key lives in."""
    return f"{name.upper().replace('-', '_')}_API_KEY"


_HWND_BROADCAST = 0xFFFF
_WM_SETTINGCHANGE = 0x001A
_SMTO_ABORTIFHUNG = 0x0002
"""Win32 broadcast plumbing, named at module scope so they read as the constants
they are rather than as three locals invented per call."""


def write_user_env_var(variable: str, value: str | None) -> bool:
    """Write/remove a user-level environment variable on Windows; ``False`` otherwise.

    The registry is the only place this application persists a key, because it is
    the place Windows itself keeps environment variables: it is not in the repo,
    not in the data root the disk cleaner scans, and not in any backup of the
    project. ``WM_SETTINGCHANGE`` is broadcast so an already-running Explorer
    picks the change up -- without it the variable exists but a double-clicked app
    launched from that Explorer still cannot see it, which is a bug we hit once
    already and it cost an hour to find.
    """
    import ctypes
    import sys

    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
        ) as key:
            if value is None:
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(key, variable)
            else:
                winreg.SetValueEx(key, variable, 0, winreg.REG_SZ, value)
    except OSError:
        logger.exception("could not persist %s to the Windows user environment", variable)
        return False

    try:
        ctypes.windll.user32.SendMessageTimeoutW(
            _HWND_BROADCAST, _WM_SETTINGCHANGE, 0, "Environment", _SMTO_ABORTIFHUNG, 5000
        )
    except Exception:  # pragma: no cover - a missing user32 must not lose the key
        logger.warning("could not broadcast the environment change; a reboot will apply it")
    return True
