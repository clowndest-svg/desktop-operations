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
from pathlib import Path
from typing import Any

from jarvis.app import local_advice
from jarvis.app.image_gen import IMAGE_KEY_ENV
from jarvis.app.preferences import (
    CHAT_HISTORY_TURNS,
    DEFAULT_DISPLAY_NAME,
    DISPLAY_NAME,
    LLM_BASE_URL,
    LLM_EXTRAS,
    LLM_HIDDEN,
    LLM_MODEL,
    LLM_OVERRIDES,
    LLM_PROVIDER,
    LLM_THINKING,
    LLM_THINKING_BUDGET,
    LLM_TUNING,
    LOCAL_CONTEXT,
    LOCAL_EAGER,
    LOCAL_IDLE_MINUTES,
    LOCAL_MODEL,
    SETTINGS_AI_EDITED,
    TELEMETRY_INTERVAL_MS,
    THINKING_LOADER,
    THINKING_LOADERS,
    VOICE_AUTO_ARM,
    VOICE_AUTO_SPEAK_TYPED,
    WAKE_GREETING,
    Preferences,
    display_name,
    thinking_loader,
)
from jarvis.app.wake_greeting import DEFAULT_GREETING, MAX_GREETING_CHARS
from jarvis.config.schema import LlmSection, ModelSpec, ProviderSection
from jarvis.llm import local_server
from jarvis.llm.local_server import CONTEXT_TIERS, IDLE_TIERS, LOCAL_PROVIDER, Tuning
from jarvis.llm.service import LlmService

logger = logging.getLogger("jarvis.app.settings_service")

MAX_NAME_CHARS = 12
"""How long a name the interface can carry: the brand sits in a slim top bar and in a
tray tooltip, and anything longer is a sentence rather than a name.
"""

CLOUD_VOICE_PROVIDER = "voice_cloud"
"""The pseudo-provider name that identifies the cloud-voice key in a patch.

Not an LLM provider -- it is a name the settings screen can pass as ``key_for``
so that one ``api_key`` field serves both kinds of key. Stored in the same place
as any other key, because a secret belongs in the environment and not in
``preferences.json``.
"""

CLOUD_VOICE_KEY_ENV = "DASHSCOPE_API_KEY"

IMAGE_KEY_TARGET = "image"
"""设置页里那格"云端生图的钥匙"的名字：作为 ``key_for`` 传进来，决定写哪个变量。"""
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
LOADER_LABELS: dict[str, str] = {
    "dots": "三个点（默认）",
    "matrix": "矩阵雨",
    "ring": "圆环",
    "bars": "竖条",
}
"""What the settings panel calls each thinking-loader kind.

Shipped in the snapshot next to the id list so the two can never disagree: the window
used to keep its own copy of this table, and a fifth kind added on the Python side
would have shown up in the menu as its bare id.
"""

TIMEOUT_BOUNDS = (5.0, 600.0)
"""Per-provider request timeout bounds, in seconds.

Five is under any real answer and over a mistyped zero; six hundred is long enough for
a big local model to load weights off a slow disk and short enough that a hung endpoint
still ends. The section-wide ``llm.timeout_seconds`` keeps its own (>= 1) rule.
"""
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
        self._ask_brief: Callable[[str], str] | None = None
        """How to ask one short question of the model the user is actually using.
        Wired late by ``set_advice_ask`` because the chat service does not exist yet
        when this one is constructed. Without it the advice button still answers --
        from the rule engine, and it says so."""
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
        # Including the bundled model's dials. Without this the first read of
        # ``model_server_status`` answers from the module default, so a buyer who set
        # 8K yesterday saw 「上下文 4096」 on the row until they opened the panel --
        # measured in the packaged build, not theorised.
        self.tune_local()

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
        # 一行只问一次：自带那一行的模型名现在由「选哪个离线模型」决定，三个字段各问一遍
        # 就成了三次读，中途换选择器会让同一行同时出现两个名字（r79 定下的那条规矩）。
        shown = {name: _follows_picker(name, row) for name, row in section.providers.items()}
        provider = shown.get(provider_name)
        pair_model = provider.model_spec(self._prefs.text(LLM_MODEL)).id if provider else ""
        # 这三格显示的是**当前这一对模型真实在用的值**，不是某个全局副本：以前它们读全局，
        # 而每次请求读的是每一对自己那一行 —— 面板写着 2 轮、发出去却是 10 轮（r85 实测）。
        pair = self.tuning_for(provider_name, pair_model)
        variable = provider.api_key_env if provider is not None else ""
        configured = set(self._safe_section().providers)
        overrides = self._overrides()
        return {
            "error": "",
            "llm_timeout_seconds": section.timeout_seconds,
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
                    "key_optional": row.key_optional,
                    "timeout_seconds": row.timeout_seconds,
                    "source": "配置文件" if name in configured else "界面添加",
                    "edited": name in overrides,
                    "current": name == provider_name,
                    # 界面据此把「加模型 / 删」这两枚按钮按下去：能让用户点的按钮点了必须有效，
                    # 而这一行的模型表由「选哪个离线模型」决定，在这里加完也会被盖掉。
                    "models_locked": name == LOCAL_PROVIDER,
                }
                for name, row in sorted(shown.items())
            ],
            "provider": provider_name,
            "hidden_models": self._hidden_rows(section),
            "base_url": provider.base_url if provider is not None else "",
            "model": provider.model if provider is not None else "",
            "api_key_variable": variable,
            "api_key_set": bool(self._environ.get(variable, "").strip()),
            "cloud_voice_key_variable": CLOUD_VOICE_KEY_ENV,
            "cloud_voice_key_set": bool(self._environ.get(CLOUD_VOICE_KEY_ENV, "").strip()),
            "image_key_set": bool(self._environ.get(IMAGE_KEY_ENV, "").strip()),
            "image_key_variable": IMAGE_KEY_ENV,
            "overrides_active": self._override_summary(),
            "voice_auto_arm": self._prefs.flag(VOICE_AUTO_ARM, default=False),
            "auto_speak_typed": self._prefs.flag(VOICE_AUTO_SPEAK_TYPED, default=True),
            "telemetry_interval_ms": self._prefs.number(TELEMETRY_INTERVAL_MS, default=1500),
            "thinking_enabled": pair["thinking_enabled"],
            "thinking_budget": pair["thinking_tokens"],
            "thinking_budget_bounds": list(THINKING_BUDGET_BOUNDS),
            # 随包那个 llama.cpp 不认「思考预算」这个字段（r90 实测：32/128/512/2048
            # 想出来的字数一样，都是 ~850；同一句拿 max_tokens 当标尺是 93/382/853）。
            # 所以这一格在本地那一家是"只关得掉、调不了深浅"，面板必须这么说，
            # 不能继续写"能和回答里的数对上，所以不是摆设"。
            "thinking_budget_honored": not (
                provider_name == local_server.LOCAL_PROVIDER
                and local_server.selected_card().thinking
            ),
            "history_turns": pair["turns"],
            "history_turns_bounds": list(HISTORY_TURNS_BOUNDS),
            "tuning_pair": f"{provider_name} · {pair_model}" if pair_model else provider_name,
            "tuning_pinned": pair["stored"],
            "ai_edited": self.ai_edited(),
            "wake_greeting": self.wake_greeting(),
            "wake_greeting_default": DEFAULT_GREETING,
            "wake_greeting_max": MAX_GREETING_CHARS,
            "thinking_loader": self.thinking_loader(),
            "thinking_loader_choices": list(THINKING_LOADERS),
            # The labels travel with the ids on purpose: a loader kind the window has
            # never heard of must render as a *labelled* choice, not as a raw id the
            # operator cannot tell from a bug.
            "thinking_loader_options": [
                {"id": kind, "label": LOADER_LABELS.get(kind, f"其它：{kind}")}
                for kind in THINKING_LOADERS
            ],
            "display_name": self.display_name(),
            "display_name_default": DEFAULT_DISPLAY_NAME,
            "display_name_max": MAX_NAME_CHARS,
            **self.local_section(),
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
        original = section.providers.get(provider_name)
        if original is None:
            return provider_name, ""
        provider = _follows_picker(provider_name, original)
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
        shown = {name: _follows_picker(name, row) for name, row in section.providers.items()}
        provider = shown.get(provider_name)
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
                    "key_optional": row.key_optional,
                    "timeout_seconds": row.timeout_seconds,
                    "current": name == provider_name,
                }
                for name, row in sorted(shown.items())
            ],
            "provider": provider_name,
            "model": model_id,
            "thinking": tuning["thinking"],
            # 面板里填过精确预算时，档位下拉多一项「自定义 8000」—— 否则那个 select
            # 会挑不到任何 option，屏上看着像"没设过"。
            "thinking_custom_budget": tuning["thinking_budget"],
            "thinking_levels": (
                [*THINKING_LEVEL_ORDER, "custom"]
                if tuning["thinking"] == "custom"
                else list(THINKING_LEVEL_ORDER)
            ),
            "turns": tuning["turns"],
            "tools": tuning["tools"],
            "turns_bounds": list(HISTORY_TURNS_BOUNDS),
        }

    # -- per-model tuning: thinking level and context size -------------------

    def tuning_for(self, provider: str, model: str) -> dict[str, Any]:
        """The thinking level, context size and tool table remembered for one pair.

        Read through a stored table rather than one key per setting because the pair
        is the identity: the answer to "how hard should this model think" is only
        meaningful next to the model it is about.

        This is also **the** answer the settings panel shows: the three fields in
        设置 → 思考与上下文 used to read globals that the per-pair row always shadowed,
        so the panel displayed one number while the request carried another (r85).
        ``stored`` says whether this pair has a row of its own, and
        ``thinking_budget`` carries an exact token count typed in the panel -- the
        header's four levels are presets, not the only values there are.
        """
        stored = self._prefs.get(LLM_TUNING)
        row = stored.get(_tuning_key(provider, model)) if isinstance(stored, dict) else None
        if not isinstance(row, dict):
            # 没单独设过的模型：跟最后一次改动（``_write_tuning`` 每次都把这两个全局
            # 镜像写一遍），出厂那份是「不思考 / 2048」。以前这里固定回 "medium"，
            # 于是面板显示"未勾选"而每条请求都带着 4096 的思考预算。
            tokens = self.thinking_budget()
            enabled = self.thinking_enabled()
            label = _nearest_level(tokens) if enabled else "off"
            return {
                "thinking": label,
                "level": label,
                "thinking_budget": None,
                "thinking_enabled": enabled,
                "thinking_tokens": tokens,
                "turns": self.history_turns(),
                "tools": True,
                "stored": False,
            }
        level = str(row.get("thinking") or "").strip()
        if level not in THINKING_LEVELS:
            level = DEFAULT_THINKING_LEVEL
        budget = row.get("thinking_budget")
        if isinstance(budget, bool) or not isinstance(budget, int):
            budget = None
        else:
            low, high = THINKING_BUDGET_BOUNDS
            budget = budget if low <= budget <= high else None
        try:
            turns = int(row.get("turns", self.history_turns()))
        except (TypeError, ValueError):
            turns = self.history_turns()
        low_turns, high_turns = HISTORY_TURNS_BOUNDS
        # Absent means "never said", which is on: a request that carries the tool table
        # is what every model was answering with before this knob existed, and a row
        # written by an older version must not quietly take her hands away.
        tools = row.get("tools")
        enabled, tokens = _thinking_tokens(level, budget)
        return {
            "thinking": _thinking_label(level, budget),
            "level": level,
            "thinking_budget": budget,
            "thinking_enabled": enabled,
            "thinking_tokens": tokens,
            "turns": max(low_turns, min(high_turns, turns)),
            "tools": tools if isinstance(tools, bool) else True,
            "stored": True,
        }

    def set_tuning(
        self,
        thinking: object = None,
        turns: object = None,
        tools: object = None,
    ) -> dict[str, Any]:
        """Remember a thinking level, context size and/or tool table for the current model.

        Writes only what it was given: the knobs live in the chat header next to
        each other, and a caller changing one must not silently reset the other to
        its default.
        """
        section = self._effective_section()
        return self._write_tuning(
            section.default_provider,
            self._current_model(section),
            tools,
            thinking,
            turns,
        )

    def _current_model(self, section: LlmSection) -> str:
        """The model id the chat is on. For the bundled row that is the picker's, not the YAML's.

        ``set_tuning`` writes the 思考档/上下文 under this name; if it disagreed with the
        name actually sent, the knob would be filed where nothing ever reads it.
        """
        provider = section.providers.get(section.default_provider)
        if provider is None:
            return ""
        provider = _follows_picker(section.default_provider, provider)
        return provider.model_spec(self._prefs.text(LLM_MODEL)).id

    def _current_pair(self) -> tuple[str, str]:
        """The provider/model the panel's 思考与上下文 fields are about."""
        section = self._effective_section()
        return section.default_provider, self._current_model(section)

    def _set_thinking_enabled(self, value: object) -> str | None:
        """The panel's checkbox -- the same value as the header's 思考 dropdown.

        It used to write a global switch that every per-pair row silently shadowed: the
        panel said 未勾选 while the request carried a 4096-token thinking budget (r85).
        """
        provider, model = self._current_pair()
        if bool(value):
            kept = self.tuning_for(provider, model)["thinking_budget"]
            level = _nearest_level(kept) if kept else "medium"
            answer = self._write_tuning(provider, model, thinking=level, thinking_budget=kept)
        else:
            answer = self._write_tuning(provider, model, thinking="off")
        return None if answer["ok"] else str(answer["error"])

    def _set_thinking_budget(self, value: object) -> str | None:
        """The panel's number: this pair's exact budget (the level is only its name)."""
        provider, model = self._current_pair()
        answer = self._write_tuning(provider, model, thinking_budget=value)
        return None if answer["ok"] else str(answer["error"])

    def _set_history_turns(self, value: object) -> str | None:
        """The panel's turn count: this pair's row, and the global default follows it."""
        provider, model = self._current_pair()
        answer = self._write_tuning(provider, model, turns=value)
        return None if answer["ok"] else str(answer["error"])

    def remember_tools(self, provider: str, model: str, enabled: bool) -> dict[str, Any]:
        """Take the tool table away from (or back to) one named pair.

        Explicit pair rather than "the current one", because the caller is the chat
        service reacting to a model that just refused a prompt: the pair that overflowed
        is not necessarily the one the window is showing -- a pinned tab answers from its
        own model.
        """
        return self._write_tuning(provider, model, enabled)

    def _write_tuning(
        self,
        provider: str,
        model: str,
        tools: object = None,
        thinking: object = None,
        turns: object = None,
        thinking_budget: object = None,
    ) -> dict[str, Any]:
        """One pair's knobs, written back in the shape they are stored in.

        ``thinking`` takes a level name or ``"custom"`` (which leaves an exact budget
        alone); ``thinking_budget`` writes that exact number and labels it with the
        nearest preset. Picking a preset **clears** an exact budget -- otherwise the
        dropdown would say 中度思考 while the request still carried 8000.
        """
        current = self.tuning_for(provider, model)
        level = str(current["level"])
        budget: int | None = current["thinking_budget"]
        if thinking is not None:
            candidate = str(thinking or "").strip()
            if candidate != "custom" and candidate not in THINKING_LEVELS:
                return {"ok": False, "error": f"未知的思考强度：{candidate}", **current}
            if candidate != "custom":
                level = candidate
                # 「不思考」是关掉，不是换个数：留着预算，重新打开时还用它。
                if candidate != "off":
                    budget = None
        if thinking_budget is not None:
            if isinstance(thinking_budget, bool) or not isinstance(
                thinking_budget, (int, float, str)
            ):
                return {"ok": False, "error": "思考预算必须是整数", **current}
            try:
                wanted = int(thinking_budget)
            except (TypeError, ValueError):
                return {"ok": False, "error": "思考预算必须是整数", **current}
            low_budget, high_budget = THINKING_BUDGET_BOUNDS
            if not low_budget <= wanted <= high_budget:
                return {
                    "ok": False,
                    "error": f"思考预算需在 {low_budget}–{high_budget} 之间",
                    **current,
                }
            budget = wanted
            level = _nearest_level(wanted)
        count = current["turns"]
        if turns is not None:
            # Typed before it is converted: the value arrives from a spinner in the
            # page, so it is ``object`` here, and mypy is right that ``int()`` on an
            # arbitrary object is not a conversion but a guess. A bool is excluded
            # explicitly -- ``True`` would otherwise become a context of one turn.
            if isinstance(turns, bool) or not isinstance(turns, (int, float, str)):
                return {"ok": False, "error": "上下文轮数必须是整数", **current}
            try:
                count = int(turns)
            except (TypeError, ValueError):
                return {"ok": False, "error": "上下文轮数必须是整数", **current}
            low, high = HISTORY_TURNS_BOUNDS
            if not low <= count <= high:
                return {"ok": False, "error": f"上下文轮数需在 {low}–{high} 之间", **current}
        carry = current["tools"]
        if tools is not None:
            if not isinstance(tools, bool):
                return {"ok": False, "error": "带工具只能是要么带、要么不带", **current}
            carry = tools
        stored = self._prefs.get(LLM_TUNING)
        table = dict(stored) if isinstance(stored, dict) else {}
        row: dict[str, Any] = {"thinking": level, "turns": count, "tools": carry}
        if budget is not None:
            row["thinking_budget"] = budget
        table[_tuning_key(provider, model)] = row
        self._prefs.set(LLM_TUNING, table)
        # The chat service reads this per turn, so the next question is already answered
        # with the new value -- no restart, which is the whole point of a dropdown next
        # to the input box. The globals are mirrored too: they are what a model nobody
        # has touched falls back to, and a panel that stopped speaking for the whole
        # window after the first edit would be its own kind of lie.
        enabled, tokens = _thinking_tokens(level, budget)
        self._prefs.set_flag(LLM_THINKING, enabled)
        self._prefs.set(LLM_THINKING_BUDGET, tokens)
        self._prefs.set(CHAT_HISTORY_TURNS, count)
        return {
            "ok": True,
            "error": "",
            "provider": provider,
            "model": model,
            "thinking": _thinking_label(level, budget),
            "level": level,
            "thinking_budget": budget,
            "thinking_enabled": enabled,
            "thinking_tokens": tokens,
            "turns": count,
            "tools": carry,
            "stored": True,
        }

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
        spec = _follows_picker(name, provider).model_spec(str(model_id or ""))
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
        if name == LOCAL_PROVIDER:
            return {"ok": False, "error": _LOCAL_ROW_IS_READONLY, "models": []}
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
        if name == LOCAL_PROVIDER:
            return {"ok": False, "error": _LOCAL_ROW_IS_READONLY, "models": []}
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
        if key == "key_optional":
            return self._set_row_key_optional(value)
        if key == "timeout_seconds":
            return self._set_row_timeout(value)
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
            return self._set_thinking_enabled(value)
        if key == "thinking_budget":
            return self._set_thinking_budget(value)
        if key == "history_turns":
            return self._set_history_turns(value)
        if key == "wake_greeting":
            return self._set_greeting(value)
        if key == "thinking_loader":
            return self._set_loader(value)
        if key == "display_name":
            return self._set_display_name(value)
        if key == "local_context":
            problem = self._set_local_choice(
                LOCAL_CONTEXT, value, CONTEXT_TIERS, "上下文窗口", "token"
            )
            if problem is None:
                self._note_local_window()
            return problem
        if key == "local_idle_minutes":
            problem = self._set_local_choice(
                LOCAL_IDLE_MINUTES, value, IDLE_TIERS, "空闲自动关", "分钟"
            )
            if problem is None:
                local_server.shared_server().rearm_idle()
            return problem
        if key == "local_eager":
            self._prefs.set(LOCAL_EAGER, bool(value))
            self.tune_local()
            return None
        if key == "local_model":
            return self._set_local_model(value)
        if key in ("alerts_rules", "alerts_cooldown_minutes", "alerts_speak_critical"):
            return self._apply_alerts(key, value)
        if key == "wake_keywords":
            if self._wake_words is None:
                return "这台机器没有接唤醒词设置"
            verdict: str | None = self._wake_words.apply(value)
            return verdict
        return "未知设置项"

    # -- the bundled model's dials ---------------------------------------------

    def tune_local(self) -> Tuning:
        """Push the dialed window / idle wait / eager flag into the llm layer.

        :mod:`jarvis.llm.local_server` may not read preferences (the layer guard says
        so), so every reader of the bundled model goes through here first -- otherwise a
        dial moved in the panel would be ignored by the next spawn.
        """
        context = self._choice(LOCAL_CONTEXT, CONTEXT_TIERS, local_server.DEFAULT_CONTEXT)
        minutes = self._choice(LOCAL_IDLE_MINUTES, IDLE_TIERS, local_server.DEFAULT_IDLE_MINUTES)
        dial = Tuning(
            context=context,
            idle_seconds=float(minutes) * 60.0,
            eager=self._prefs.flag(LOCAL_EAGER, default=False),
            model=self._prefs.text(LOCAL_MODEL, default=local_server.MODEL_AUTO),
        )
        local_server.configure(dial)
        return dial

    def _choice(self, key: str, tiers: tuple[int, ...], fallback: int) -> int:
        """A stored tier, or the shipped one when what is stored is not a tier."""
        value = int(self._prefs.number(key, default=fallback))
        return value if value in tiers else fallback

    def local_section(self) -> dict[str, object]:
        """The bundled model's dials, with the measured cost of each tier attached."""
        dial = self.tune_local()
        card = local_server.card_for(local_server.model_file())
        return {
            "local_context": dial.context,
            "local_model": dial.model,
            "local_model_label": card.label,
            "local_models": local_server.bundled_catalog(),
            "local_context_options": [
                {
                    "id": tier,
                    "label": f"{tier // 1024}K",
                    "note": self._tier_note(tier),
                    # The gate for *this* tier **of the model actually picked**: the dropdown
                    # must not keep quoting 7B numbers when the 0.6B is loaded, nor the
                    # numbers of whatever was saved last.
                    "needs_free_mb": local_server.gate_mb(card, tier),
                }
                for tier in CONTEXT_TIERS
            ],
            "local_idle_minutes": int(dial.idle_seconds // 60),
            "local_idle_options": [
                {
                    "id": minutes,
                    "label": "不自动关（一直占内存）" if minutes == 0 else f"{minutes} 分钟",
                }
                for minutes in IDLE_TIERS
            ],
            "local_eager": dial.eager,
            "local_needs_free_mb": dial.needed_free_mb,
            "local_free_mb": local_server.free_memory_mb(),
            "local_tool_table_tokens": local_server.TOOL_TABLE_TOKENS,
            "local_pack_tokens": local_server.PACK_TABLE_TOKENS,
        }

    def _tier_note(self, tier: int) -> str:
        """What this window costs *on the model that is picked*, and what it lets her do.

        The numbers are per-model measurements (``MODEL_CARDS``); an unmeasured file says
        so instead of borrowing the 7B's figures.
        """
        card = local_server.card_for(local_server.model_file())
        tools = local_server.TOOL_TABLE_TOKENS
        pack = local_server.PACK_TABLE_TOKENS
        gpu, cpu = card.resident(tier)
        if not card.measured:
            cost = f"这个文件没实测过，按大小估要约 {card.gate_base_mb / 1024:.1f} GB 空闲内存"
        else:
            cost = f"本机常驻约 {gpu / 1024:.1f} GB（没显卡的机器约 {cpu / 1024:.1f} GB）"
        if tier < pack:
            return f"{cost}；连 {pack} token 的最小工具包都装不下，这一档她只回答、不动手"
        if tier < tools:
            return (
                f"{cost}；装不下 {tools} token 的完整工具清单，"
                f"但装得下按问题挑出来的那一小包（最多 {pack} token）。"
                "r88 起她先只带用得上那几个，问本机、查版本、联网、开程序都能动手"
            )
        if not card.acts_verified:
            return (
                f"{cost}；装得下那份 {tools} token 的工具清单，"
                "但**这台机器上还没见过它真调成一次工具**"
            )
        return f"{cost}；装得下那份 {tools} token 的工具清单，实测见过它一边答一边动手"

    def _set_local_choice(
        self, key: str, value: Any, tiers: tuple[int, ...], what: str, unit: str
    ) -> str | None:
        """Store one of the bundled model's enumerated dials, refusing a value off the list."""
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return f"{what}得是个数字"
        if number not in tiers:
            options = "、".join(str(tier) for tier in tiers)
            return f"{what}没有「{number} {unit}」这一档，可选：{options}"
        self._prefs.set(key, number)
        self.tune_local()
        return None

    def _set_local_model(self, value: Any) -> str | None:
        """Pick which weights file she loads. Only the chosen one ever reaches memory."""
        name = str(value or "").strip()
        if name not in (local_server.MODEL_AUTO, local_server.MODEL_NONE):
            rows = {str(row["file"]): row for row in local_server.bundled_catalog()}
            row = rows.get(name)
            if row is None:
                options = "、".join(sorted(rows)) or "（包里没有权重文件）"
                return f"没有「{name}」这个离线模型，可选：{options}"
            if not row["present"]:
                return f"「{name}」的文件不在「{local_server.MODEL_DIRNAME}」目录里，选不了"
        self._prefs.set(LOCAL_MODEL, name)
        self.tune_local()
        server = local_server.shared_server()
        if server.phase is local_server.ModelPhase.RUNNING:
            logger.info("离线模型换选了，但还在按上一个跑：停掉再加载才换上新的")
        return None

    def _note_local_window(self) -> None:
        """A window moved while the model is loaded cannot resize it -- say so, don't lie."""
        server = local_server.shared_server()
        if server.phase is not local_server.ModelPhase.RUNNING:
            return
        logger.info("自带模型还在按旧的上下文窗口跑，停掉再加载才换成新的")

    def set_advice_ask(self, ask: Callable[[str], str]) -> None:
        """Wire the one-shot question the advice button asks."""
        self._ask_brief = ask

    def recommend_local(self) -> dict[str, object]:
        """One look at this machine, one answer: which model, which window, how long idle.

        The model gets first say because that is what the button promises. When it cannot
        be reached -- no key, the offline model refused by this very machine -- the rule
        engine answers instead and ``source`` says which one it was. A button that goes
        quiet is worse than a button that admits it is a calculator.
        """
        machine = local_advice.read_machine()
        catalog = local_server.bundled_catalog()
        fallback = local_advice.rule_advice(machine, catalog)
        if self._ask_brief is None:
            return {**fallback.as_row(), "note": "没接到模型，这一套是本地规则算的"}
        try:
            advice = local_advice.ai_advice(machine, catalog, self._ask_brief)
        except Exception as err:  # 任何失败都回落，但要留一行日志
            logger.info("AI 建议没拿到（%s），这一套是本地规则算的", err)
            return {**fallback.as_row(), "note": f"模型那一路没走通（{err}），这是本地规则算的"}
        return {**advice.as_row(), "note": ""}

    def apply_local_advice(self, payload: Mapping[str, object]) -> str | None:
        """Apply an advice card the page sent back. Re-validated here, never trusted.

        The round trip goes through the DOM, and the enumerated dials are exactly the
        thing that must not accept a value off the list -- so this walks the same
        setters the settings rows use.
        """
        problems: list[str] = []
        model = str(payload.get("model") or "")
        if model:
            problem = self._set_local_model(model)
            if problem:
                problems.append(problem)
        if "context" in payload:
            # 走界面上那三格同一个校验入口：建议卡是从 DOM 绕回来的，
            # 不在档位里的值必须被拒，不能因为"是建议"就放行。
            problem = self._set_local_choice(
                LOCAL_CONTEXT, payload["context"], CONTEXT_TIERS, "上下文窗口", "token"
            )
            if problem:
                problems.append(problem)
            else:
                self._note_local_window()
        if "idle_minutes" in payload:
            problem = self._set_local_choice(
                LOCAL_IDLE_MINUTES, payload["idle_minutes"], IDLE_TIERS, "空闲自动关", "分钟"
            )
            if problem:
                problems.append(problem)
            else:
                local_server.shared_server().rearm_idle()
        if "eager" in payload:
            self._prefs.set(LOCAL_EAGER, bool(payload["eager"]))
            self.tune_local()
        return "；".join(problems) if problems else None

    def display_name(self) -> str:
        """What the interface calls her."""
        return display_name(self._prefs.text(DISPLAY_NAME, default=""))

    def _set_display_name(self, value: Any) -> str | None:
        """Store the name. Empty is a real answer: it means the shipped one."""
        text = str(value or "").strip()[:MAX_NAME_CHARS]
        self._prefs.set(DISPLAY_NAME, text)
        return None

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

    def _set_row_timeout(self, value: Any) -> str | None:
        """One row's own request timeout; ``None``/blank means "follow the global one".

        Stored as ``None`` on purpose rather than deleted: the override table is the
        only place a window edit lives, and "the operator looked at this and chose the
        global value" has to survive a restart the same way any other edit does.
        """
        target = self._pending_target or self._effective_section().default_provider
        if target not in self._effective_section().providers:
            return f"未配置的模型：{target!r}"
        timeout: float | None = None
        text = str(value or "").strip()
        if text:
            try:
                timeout = float(text)
            except ValueError:
                return "超时秒数得是个数字"
            low, high = TIMEOUT_BOUNDS
            if not low <= timeout <= high:
                return f"超时秒数要在 {low:g}–{high:g} 之间"
        overrides = self._overrides()
        overrides.setdefault(target, {})["timeout_seconds"] = timeout
        if not self._prefs.set(LLM_OVERRIDES, overrides):
            return "偏好文件拒绝了这次写入，这一行的超时没有保存"
        return None

    def _set_row_key_optional(self, value: Any) -> str | None:
        """Say whether one row's endpoint needs a key at all.

        The direction that matters is switching it **on**: a row that arrived from
        ``config.yaml`` with a key variable nobody on this machine has set is otherwise
        unusable as a local endpoint, because the client refuses the request before it
        opens a socket. Switching it off costs nothing -- a key that is present is still
        sent.
        """
        target = self._pending_target or self._effective_section().default_provider
        if target not in self._effective_section().providers:
            return f"未配置的模型：{target!r}"
        overrides = self._overrides()
        overrides.setdefault(target, {})["key_optional"] = bool(value)
        if not self._prefs.set(LLM_OVERRIDES, overrides):
            return "偏好文件拒绝了这次写入，这一行的免 Key 标记没有保存"
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
                return "新增模型需要一个 {name, base_url, models} 对象"
            name = str(item.get("name") or "").strip()
            base_url = str(item.get("base_url") or "").strip()
            if not _valid_name(name):
                return "模型名字只能是小写字母、数字、- 和 _，1-32 位"
            if not _valid_url(base_url):
                return f"{name}：地址必须以 http:// 或 https:// 开头"
            specs = _draft_models(item)
            if not specs:
                return f"{name}：至少写一个模型名"
            bad = next((spec.id for spec in specs if not _valid_model(spec.id)), "")
            if bad:
                return f"{name}：模型名 {bad[:30]!r} 不能含空格且不超过 150 字符"
            if name in known:
                return f"已经有叫 {name!r} 的模型；要改它就在那一行上改"
            known.add(name)
            timeout, why = _draft_timeout(item.get("timeout_seconds"))
            if why:
                return f"{name}：{why}"
            pending.append(
                {
                    "name": name,
                    "base_url": base_url,
                    # The list shape the rest of this file reads back. One endpoint usually
                    # serves a family of models, and the 看看它有哪些模型 button has just
                    # listed them all -- making the operator save once per model was the
                    # dialog telling them to reopen it for the thing they can already see.
                    "models": _serialise_models(specs),
                    "default_model": specs[0].id,
                    "key_env": key_env_for(name),
                    "timeout_seconds": timeout,
                    # A local server (Ollama / LM Studio / llama.cpp) has no credential,
                    # and the panel now says so explicitly instead of leaving the field
                    # blank and letting the client refuse at request time.
                    "key_optional": bool(item.get("key_optional")),
                }
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
        elif target == IMAGE_KEY_TARGET:
            # 云端生图的钥匙。和上面那条同一个落点：进程环境变量 + 用户级环境变量，
            # 配置文件里一个字节都不留，界面也永不回显它的值。
            variable = IMAGE_KEY_ENV
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
                    "key_optional": bool(entry.get("key_optional", False)),
                    "timeout_seconds": entry.get("timeout_seconds"),
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
            if isinstance(fields.get("key_optional"), bool):
                row["key_optional"] = fields["key_optional"]
            if "timeout_seconds" in fields:
                timeout = fields["timeout_seconds"]
                if timeout is None:
                    row["timeout_seconds"] = None
                elif (
                    isinstance(timeout, (int, float))
                    and not isinstance(timeout, bool)
                    and TIMEOUT_BOUNDS[0] <= float(timeout) <= TIMEOUT_BOUNDS[1]
                ):
                    row["timeout_seconds"] = float(timeout)
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
                key_optional=bool(extra.get("key_optional", False)),
                timeout_seconds=extra.get("timeout_seconds"),
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
                key_optional=row.get("key_optional", existing.key_optional),
                timeout_seconds=row.get("timeout_seconds", existing.timeout_seconds),
                default_model=_default_of(specs, row.get("default_model", existing.default_model)),
            )
        providers = self._with_local_endpoint(providers)
        saved = self._prefs.text(LLM_PROVIDER)
        if saved not in providers:
            saved = ""
        default_provider = saved or self._default_name(providers, base.default_provider)
        providers = self._without_hidden(providers, keep=default_provider)
        return replace(base, providers=providers, default_provider=default_provider)

    def _with_local_endpoint(
        self, providers: dict[str, ProviderSection]
    ) -> dict[str, ProviderSection]:
        """Point the bundled provider at the port llama.cpp actually answered on.

        The YAML carries a fixed port so a hand-written config still makes sense, but a
        second llama.cpp on the machine (the standalone ``start.ps1`` deployment uses
        28080, this one prefers 28181) means the real port is only known once the child
        is up. Reading settings must never start a process -- this is a read path -- so
        it reports the port the server is listening on *now*, or the preferred one.
        """
        local = providers.get(local_server.LOCAL_PROVIDER)
        if local is None or local.base_url == local_server.endpoint():
            return providers
        fresh = replace(local, base_url=local_server.endpoint())
        return {**providers, local_server.LOCAL_PROVIDER: fresh}

    def _default_name(self, providers: dict[str, ProviderSection], configured: str) -> str:
        """The bundled model is the default **when it can actually run here**.

        A buyer without the 4.36 GB weights, or with 3 GB of free RAM, must not get an
        assistant that cannot answer -- so the fallback is the YAML's cloud default, and
        the settings row says which of the two is in effect and why.
        """
        if local_server.LOCAL_PROVIDER not in providers or not local_server.plan().can_run:
            return configured
        if configured == local_server.LOCAL_PROVIDER:
            return configured
        return local_server.LOCAL_PROVIDER

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


def _draft_timeout(value: Any) -> tuple[float | None, str]:
    """One add-patch row's own timeout: ``(seconds or None, refusal reason)``.

    Blank means "follow the global one", which is a legal answer, not a missing field --
    but a number outside :data:`TIMEOUT_BOUNDS` is a typo and must be refused where it is
    typed, not silently rounded into something the operator did not ask for.
    """
    text = str(value or "").strip()
    if not text:
        return None, ""
    try:
        timeout = float(text)
    except ValueError:
        return None, "超时秒数得是个数字"
    low, high = TIMEOUT_BOUNDS
    if not low <= timeout <= high:
        return None, f"超时秒数要在 {low:g}–{high:g} 之间"
    return timeout, ""


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


def _draft_models(fields: Mapping[str, object]) -> tuple[ModelSpec, ...]:
    """The models one **add** row carries: ``models`` as a list, or the older ``model``.

    Duplicates are folded rather than refused: the list usually came from clicking the
    endpoint's own 模型清单, and clicking one twice is a slip of the finger, not a
    disagreement about what should be stored. Order is kept, because the first one is
    the 起始模型 the row will answer with until somebody picks another.
    """
    raw = fields.get("models")
    specs: list[ModelSpec] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, Mapping):
                ident = str(entry.get("id") or "").strip()
                label = str(entry.get("label") or "").strip()
            else:
                ident, label = str(entry or "").strip(), ""
            if ident:
                specs.append(ModelSpec(id=ident, label=label))
    else:
        single = str(fields.get("model") or "").strip()
        if single:
            specs.append(ModelSpec(id=single, label=""))
    kept: list[ModelSpec] = []
    seen: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            continue
        seen.add(spec.id)
        kept.append(spec)
    return tuple(kept)


def _default_of(specs: tuple[ModelSpec, ...], wanted: object) -> str:
    """Which of ``specs`` is the default: the named one when present, else the first."""
    name = str(wanted or "").strip()
    ids = [spec.id for spec in specs]
    return name if name in ids else (ids[0] if ids else "")


_LOCAL_ROW_IS_READONLY = "自带模型这一行用哪个模型，由上面「选哪个离线模型」决定，这里不加也不删"


def _follows_picker(name: str, row: ProviderSection) -> ProviderSection:
    """自带模型那一行不再自己决定用哪个模型 —— 它显示的就是「选哪个离线模型」选中的那个。

    之前这一行永远写着 defaults.yaml 里的 qwen2.5-7b，而实际加载的是 Qwen3-4B：同一个设置
    出现两处、各说一套，用户照着那一行去找模型就找不到（r84 实拍）。
    """
    if name != LOCAL_PROVIDER:
        return row
    card = local_server.selected_card()
    spec = ModelSpec(id=card.alias, label=f"自带·{card.label}（离线）")
    return replace(row, models=(spec,), default_model=card.alias)


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


def _nearest_level(budget: int) -> str:
    """Which preset best describes an exact budget (used only to label it)."""
    if budget <= 0:
        return "off"
    presets = {name: tokens for name, tokens in THINKING_LEVELS.items() if tokens > 0}
    return min(presets, key=lambda name: abs(presets[name] - budget))


def _thinking_label(level: str, budget: int | None) -> str:
    """What the picker shows: the level, or ``custom`` when the number is not a preset."""
    if level == "off" or budget is None:
        return level
    return level if THINKING_LEVELS.get(level) == budget else "custom"


def _thinking_tokens(level: str, budget: int | None) -> tuple[bool, int]:
    """``(enabled, tokens)`` -- what one pair's row means on the wire.

    An exact budget wins over the level's preset; ``off`` means no thinking at all, and
    the number that travels with it is only what to put back when it is switched on
    again.
    """
    if level == "off":
        return False, budget if budget is not None else DEFAULT_THINKING_BUDGET
    if budget is not None:
        return True, budget
    return True, THINKING_LEVELS.get(level, DEFAULT_THINKING_BUDGET)


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
    from jarvis import is_buyer

    if is_buyer():
        from jarvis.config.credentials import write_credential

        try:
            write_credential(Path(os.environ["JARVIS_HOME"]), variable, value)
            return True
        except (OSError, ValueError, KeyError):
            logger.exception("could not save buyer credential %s", variable)
            return False

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
