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
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from jarvis.app.preferences import (
    LLM_BASE_URL,
    LLM_EXTRAS,
    LLM_MODEL,
    LLM_OVERRIDES,
    LLM_PROVIDER,
    TELEMETRY_INTERVAL_MS,
    VOICE_AUTO_ARM,
    VOICE_AUTO_SPEAK_TYPED,
    Preferences,
)
from jarvis.config.schema import LlmSection, ProviderSection
from jarvis.llm.service import LlmService

logger = logging.getLogger("jarvis.app.settings_service")

MAX_BASE_URL_CHARS = 300
MAX_MODEL_CHARS = 150
TELEMETRY_INTERVAL_BOUNDS = (500, 60_000)
"""Floor and ceiling for the HUD's poll interval, in milliseconds.

The ceiling matters more than the floor: the settings screen is reachable by
anyone who can see the window, and "poll every 10 ms" is a way to accidentally
take a laptop down with you.
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
        """Fold the old single-row overrides into the per-provider map, once.

        Earlier versions stored one address and one model name for whichever
        provider was selected, and *deleted them on provider switch* -- editing one
        model row and then looking at another lost the edit. Rows that survived get
        moved under the provider they belonged to; nothing is guessed beyond that.
        """
        url = self._prefs.text(LLM_BASE_URL)
        model = self._prefs.text(LLM_MODEL)
        if not url and not model:
            return
        target = self._prefs.text(LLM_PROVIDER) or self._safe_section().default_provider
        overrides = self._overrides()
        row = overrides.setdefault(target, {})
        if url and _valid_url(url):
            row.setdefault("base_url", url)
        if model and _valid_model(model):
            row.setdefault("model", model)
        self._prefs.set(LLM_OVERRIDES, overrides)
        self._prefs.forget(LLM_BASE_URL)
        self._prefs.forget(LLM_MODEL)
        logger.info("folded the old global llm overrides into provider %r", target)

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
                    "key_env": row.api_key_env,
                    "key_set": bool(self._environ.get(row.api_key_env, "").strip()),
                    "source": "配置文件" if name in configured else "界面添加",
                    "edited": name in overrides,
                    "current": name == provider_name,
                }
                for name, row in sorted(section.providers.items())
            ],
            "provider": provider_name,
            "base_url": provider.base_url if provider is not None else "",
            "model": provider.model if provider is not None else "",
            "api_key_variable": variable,
            "api_key_set": bool(self._environ.get(variable, "").strip()),
            "overrides_active": self._override_summary(),
            "voice_auto_arm": self._prefs.flag(VOICE_AUTO_ARM, default=False),
            "auto_speak_typed": self._prefs.flag(VOICE_AUTO_SPEAK_TYPED, default=True),
            "telemetry_interval_ms": self._prefs.number(TELEMETRY_INTERVAL_MS, default=1500),
        }

    # -- writes --------------------------------------------------------------

    def model_choices(self) -> dict[str, Any]:
        """Every configured model, for the picker in the chat header.

        Each row carries whether its key is actually present. A dropdown that
        offers a model which cannot answer is worse than one that hides it: the
        operator picks it, asks a question, and gets "API key 未设置" as the first
        thing out of the new choice. So the choice is offered but marked, and the
        reason is the environment variable's *name* -- never its value.
        """
        section = self._effective_section()
        current = section.default_provider
        choices = [
            {
                "provider": name,
                "model": provider.model,
                "key_set": bool(self._environ.get(provider.api_key_env, "").strip()),
                "key_variable": provider.api_key_env,
                "current": name == current,
            }
            for name, provider in sorted(section.providers.items())
        ]
        return {"error": "", "current": current, "choices": choices}

    def choose_model(self, provider_name: object) -> dict[str, Any]:
        """Point the assistant at another configured model.

        Deliberately the same write path as the settings screen's provider field
        rather than a second, chat-local override: two places that each think they
        select the model is how "I switched to the cheap one and it still used the
        expensive one" gets reported as a billing bug.

        Answers with the *model list*, not the settings snapshot, because that is
        what the picker needs to redraw itself -- and on a refusal it needs the
        unchanged list plus the reason, or the dropdown is left showing a model that
        is not answering the next question.
        """
        name = str(provider_name or "").strip()
        listing = self.model_choices()
        known = {str(entry["provider"]) for entry in listing["choices"]}
        if not name or name not in known:
            listing["error"] = f"未配置的模型：{name or '（空）'}"
            return listing
        outcome = self.apply({"provider": name})
        problem = outcome.get("problems")
        if isinstance(problem, dict) and problem.get("provider"):
            listing["error"] = str(problem["provider"])
            return listing
        return self.model_choices()

    def speaks_typed(self) -> bool:
        """Whether a typed answer should also be read aloud.

        Read here rather than cached in the page: the toggle is in the settings
        panel, and a bridge that consulted a copy taken at mount would keep doing
        the opposite of what the operator just clicked.
        """
        return self._prefs.flag(VOICE_AUTO_SPEAK_TYPED, default=True)

    def apply(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Validate and save whatever the screen sent back.

        Returns a fresh snapshot plus a per-field verdict, because a settings
        dialog that silently drops the one field it rejected is worse than one that
        refuses the whole save.
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
            self._pending_target = ""
            self._apply_override()
        snapshot = self.snapshot()
        snapshot["applied"] = applied
        snapshot["problems"] = problems
        snapshot["error"] = "; ".join(f"{k}: {v}" for k, v in problems.items())
        return snapshot

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
        if key == "auto_speak_typed":
            self._prefs.set(VOICE_AUTO_SPEAK_TYPED, bool(value))
            return None
        if key == "telemetry_interval_ms":
            return self._set_interval(value)
        return "未知设置项"

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
        """Add a model row that does not exist in config.yaml."""
        if not isinstance(value, dict):
            return "新增模型需要一个 {name, base_url, model} 对象"
        name = str(value.get("name") or "").strip()
        base_url = str(value.get("base_url") or "").strip()
        model = str(value.get("model") or "").strip()
        if not _valid_name(name):
            return "模型名字只能是小写字母、数字、- 和 _，1-32 位"
        if not _valid_url(base_url):
            return "地址必须以 http:// 或 https:// 开头"
        if not _valid_model(model):
            return "模型名不能含空格且不超过 150 字符"
        if name in self._effective_section().providers:
            return f"已经有叫 {name!r} 的模型；要改它就在那一行上改"
        extras = self._extras()
        extras.append(
            {"name": name, "base_url": base_url, "model": model, "key_env": key_env_for(name)}
        )
        self._prefs.set(LLM_EXTRAS, extras)
        key = str(value.get("api_key") or "").strip()
        if key:
            self._store_api_key(key, name)
        return None

    def _remove_model(self, value: Any) -> str | None:
        """Delete a window-added row. Rows from config.yaml are the file's business."""
        name = str(value or "").strip()
        before = self._extras()
        extras = [entry for entry in before if entry["name"] != name]
        if len(extras) == len(before):
            return f"{name!r} 不是界面添加的模型；配置文件里的行请去 config.yaml 删"
        self._prefs.set(LLM_EXTRAS, extras)
        overrides = self._overrides()
        overrides.pop(name, None)
        self._prefs.set(LLM_OVERRIDES, overrides)
        if self._prefs.text(LLM_PROVIDER) == name:
            self._prefs.forget(LLM_PROVIDER)
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
        """
        text = str(value or "")
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

    def _extras(self) -> list[dict[str, str]]:
        """Models the window added, validated on the way out of storage.

        Re-validated at read time rather than trusted: preferences.json is a file
        the operator can open in Notepad, and a hand-edited row with a broken URL
        must degrade to "absent", not to a client pointed at nonsense.
        """
        raw = self._prefs.get(LLM_EXTRAS)
        if not isinstance(raw, list):
            return []
        kept: list[dict[str, str]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            base_url = str(entry.get("base_url") or "").strip()
            model = str(entry.get("model") or "").strip()
            if not (_valid_name(name) and _valid_url(base_url) and _valid_model(model)):
                continue
            kept.append(
                {
                    "name": name,
                    "base_url": base_url,
                    "model": model,
                    "key_env": str(entry.get("key_env") or key_env_for(name)),
                }
            )
        return kept

    def _overrides(self) -> dict[str, dict[str, str]]:
        """Per-provider address/model edits made from the window."""
        raw = self._prefs.get(LLM_OVERRIDES)
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for name, fields in raw.items():
            if not isinstance(fields, dict):
                continue
            row: dict[str, str] = {}
            url = str(fields.get("base_url") or "").strip()
            model = str(fields.get("model") or "").strip()
            if _valid_url(url):
                row["base_url"] = url
            if _valid_model(model):
                row["model"] = model
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
            providers[extra["name"]] = ProviderSection(
                name=extra["name"],
                base_url=extra["base_url"],
                model=extra["model"],
                api_key_env=extra["key_env"],
                cost_input_per_1m=0.0,
                cost_output_per_1m=0.0,
            )
        for name, row in self._overrides().items():
            existing = providers.get(name)
            if existing is None:
                continue
            providers[name] = replace(
                existing,
                base_url=row.get("base_url", existing.base_url),
                model=row.get("model", existing.model),
            )
        saved = self._prefs.text(LLM_PROVIDER)
        default_provider = saved if saved in providers else base.default_provider
        return replace(base, providers=providers, default_provider=default_provider)

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
