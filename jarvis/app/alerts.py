"""告警: what the machine is allowed to interrupt the operator for, and how often.

Why this file exists
--------------------
Before it, there was exactly one threshold in the whole program -- ``LOW_FREE_BYTES``
in :mod:`jarvis.tools.monitor` -- hardcoded, system drive only, and delivered by
appending a sentence to ``SystemSnapshot.warnings``. That channel is fine for "this
reading partially failed" and wrong for "act now": it has no severity, no memory of
having already said itself, no way to say "I saw it", and it re-arms on every poll.

So an alert here is a *state*, not a string: it opens when a reading crosses a line and
stays open until the reading comes back, and while it is open it is displayed once,
counted once, and spoken once.

The three knobs that keep it usable
-----------------------------------
* **Sustained window** (:data:`SUSTAIN_SECONDS`). A CPU spike during a build is normal;
  a machine at 95% for a minute is not. Without this, every compile is an incident.
* **Hysteresis** (``clear_margin`` on each rule). A line crossed at 90% that clears at
  90% flickers on the boundary, and a flickering alert is muted within a day.
* **Cooldown** (``cooldown_minutes``, operator-set). Once an alert has recovered, the
  same code stays quiet for that long even if it crosses again -- the operator already
  knows this machine is short on memory; saying so every minute is how the box gets
  ignored when something real happens.

What is deliberately *not* here
-------------------------------
No history that outlives the process. The list is in memory, so a restart forgets what
was acknowledged. That is a known limit rather than an oversight: the durable record of
"what happened on this machine" is the audit log and the run ledger, and bolting a
second one onto the alert box would need its own table, migration and retention rule.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from jarvis.app.preferences import (
    ALERTS_COOLDOWN_MINUTES,
    ALERTS_RULES,
    ALERTS_SPEAK_CRITICAL,
    Preferences,
)

logger = logging.getLogger("jarvis.app.alerts")

SUSTAIN_SECONDS: float = 60.0
"""How long a reading must stay past its line before the alert opens.

One minute, not one poll: the HUD asks every 1.5 s, so a single hot sample would
otherwise be an incident. Long enough to be a trend, short enough that a machine which
is genuinely stuck is reported while the operator is still at the desk.
"""

DEFAULT_COOLDOWN_MINUTES: float = 10.0
"""Silence on a code after it recovers, before it may open again."""

COOLDOWN_BOUNDS: tuple[float, float] = (1.0, 180.0)
"""What the settings panel will accept for the cooldown, in minutes."""

SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"
"""Two levels, because three is a debate. ``critical`` is the one allowed to speak."""


@dataclass(frozen=True, slots=True)
class AlertRule:
    """One line in the sand: what to watch, where it is, and what clears it."""

    code: str
    label: str
    unit: str
    default: float
    low: float
    high: float
    fires_above: bool
    clear_margin: float
    critical_margin: float
    message: str
    """A template with ``{value}``, ``{threshold}`` and ``{subject}``."""

    help: str = ""


RULES: tuple[AlertRule, ...] = (
    AlertRule(
        code="cpu",
        label="CPU 占用",
        unit="%",
        default=90.0,
        low=50.0,
        high=100.0,
        fires_above=True,
        clear_margin=5.0,
        critical_margin=5.0,
        message="CPU 已经连着占用 {value:.0f}%，超过告警线 {threshold:.0f}%",
        help="按所有核心的平均算，100% 是每个核心都排满。",
    ),
    AlertRule(
        code="memory",
        label="内存占用",
        unit="%",
        default=90.0,
        low=50.0,
        high=99.0,
        fires_above=True,
        clear_margin=5.0,
        critical_margin=5.0,
        message="内存已用 {value:.0f}%，超过告警线 {threshold:.0f}%",
        help="到线之后系统开始往交换分区搬，机器会明显变卡。",
    ),
    AlertRule(
        code="swap",
        label="交换分区占用",
        unit="%",
        default=80.0,
        low=10.0,
        high=99.0,
        fires_above=True,
        clear_margin=10.0,
        critical_margin=15.0,
        message="交换分区已用 {value:.0f}%，超过告警线 {threshold:.0f}%",
        help="交换分区吃满，下一步就是进程被系统杀掉。",
    ),
    AlertRule(
        code="disk",
        label="磁盘剩余空间",
        unit="GB",
        default=20.0,
        low=1.0,
        high=500.0,
        fires_above=False,
        clear_margin=5.0,
        critical_margin=10.0,
        message="{subject} 可用只剩 {value:.1f} GB，低于告警线 {threshold:.0f} GB",
        help="每一块盘各算一条。删东西要先扫描、勾选、确认，本工具不会自动删。",
    ),
)
"""The shipped lines. Every one of them is editable; none of them is mandatory."""

RULE_BY_CODE = {rule.code: rule for rule in RULES}


@dataclass(frozen=True, slots=True)
class Alert:
    """One open (or just-recovered) condition, ready to hand to a page."""

    code: str
    rule: str
    label: str
    severity: str
    message: str
    value: float
    threshold: float
    unit: str
    since: float
    last_seen: float
    count: float
    acknowledged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "rule": self.rule,
            "label": self.label,
            "severity": self.severity,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
            "unit": self.unit,
            "since": self.since,
            "last_seen": self.last_seen,
            "count": self.count,
            "acknowledged": self.acknowledged,
        }


@dataclass(frozen=True, slots=True)
class _Reading:
    """One rule's view of one subject (a machine, or one of its drives)."""

    rule: AlertRule
    subject: str
    value: float


def readings_of(snapshot: Any) -> list[_Reading]:
    """Turn a telemetry snapshot into (rule, subject, value) triples.

    Structural on purpose: this reads the attributes of
    :class:`jarvis.tools.monitor.SystemSnapshot` without importing it, so the alert
    engine can be driven from a ``SimpleNamespace`` in a test and the layer rule stays
    a fact rather than a favour.
    """
    found: list[_Reading] = []
    memory = getattr(snapshot, "memory", None)
    if memory is not None:
        percent = getattr(memory, "percent", None)
        if isinstance(percent, (int, float)):
            found.append(_Reading(RULE_BY_CODE["memory"], "", float(percent)))
        swap = getattr(memory, "swap_percent", None)
        if isinstance(swap, (int, float)) and swap > 0:
            # A machine with no swap configured reads 0 forever; alerting on "0 < 80"
            # would be an alert about a feature that does not exist.
            found.append(_Reading(RULE_BY_CODE["swap"], "", float(swap)))
    cpu = getattr(snapshot, "cpu", None)
    if cpu is not None:
        percent = getattr(cpu, "percent", None)
        if isinstance(percent, (int, float)):
            found.append(_Reading(RULE_BY_CODE["cpu"], "", float(percent)))
    for disk in getattr(snapshot, "disks", ()) or ():
        free = getattr(disk, "free_bytes", None)
        if isinstance(free, (int, float)):
            gigabytes = float(free) / 1024**3
            mount = str(getattr(disk, "mount", "?"))
            found.append(_Reading(RULE_BY_CODE["disk"], mount, gigabytes))
    return found


class AlertService:
    """Opens, holds, clears and mutes alerts. Thread-safe; the HUD polls, tools push."""

    def __init__(
        self,
        preferences: Preferences | None = None,
        *,
        on_fire: Callable[[Alert], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sustain_seconds: float = SUSTAIN_SECONDS,
    ) -> None:
        """Create the centre.

        Args:
            preferences: Where thresholds, the on/off switches and the cooldown live.
                ``None`` runs entirely on the shipped defaults, which is what the
                console and the tests want.
            on_fire: Called once per *newly opened* alert, on the thread that noticed
                it. The desktop hangs the spoken warning off this, so the rule that
                decides "may we interrupt with a voice" lives with the thing that
                decides whether an alert is new -- not in the announcer, which has no
                way to know it already said this one.
            clock: Injectable so the sustained window and the cooldown are testable
                without sleeping.
            sustain_seconds: How long a breach must persist before it is an alert.
        """
        self._prefs = preferences
        self.on_fire = on_fire
        """Called once per *newly opened* critical alert, on the thread that noticed it.

        Public and assignable because the thing that can speak -- the announcer, and the
        tray behind it -- is built after this service: the composition root cannot know
        the voice before the window exists. Same bargain as ``TurnRegistry.on_update``.
        """
        self._clock = clock
        self._sustain = max(0.0, float(sustain_seconds))
        self._lock = threading.Lock()
        self._open: dict[str, Alert] = {}
        self._rising: dict[str, float] = {}
        self._closed_at: dict[str, float] = {}
        self._manual: set[str] = set()
        """Keys opened by :meth:`note`. They have no reading to recover from, so
        acknowledging them is what closes them -- and the sweep must leave them alone."""
        self._history: list[Alert] = []
        """Recovered alerts, newest last, capped -- the box shows these greyed for a while."""

    # -- the operator's knobs ------------------------------------------------

    def settings(self) -> dict[str, Any]:
        """Everything the settings panel needs to draw this section."""
        stored = self._stored_rules()
        return {
            "rules": [
                {
                    "code": rule.code,
                    "label": rule.label,
                    "unit": rule.unit,
                    "help": rule.help,
                    "low": rule.low,
                    "high": rule.high,
                    "direction": "above" if rule.fires_above else "below",
                    "enabled": bool(stored.get(rule.code, {}).get("enabled", True)),
                    "threshold": self._threshold(rule),
                    "default_threshold": rule.default,
                }
                for rule in RULES
            ],
            "cooldown_minutes": self._cooldown_minutes(),
            "cooldown_bounds": [COOLDOWN_BOUNDS[0], COOLDOWN_BOUNDS[1]],
            "speak_critical": self._speak_critical(),
            "sustain_seconds": self._sustain,
        }

    def apply_settings(self, patch: Mapping[str, Any]) -> str | None:
        """Validate and store one patch. Returns a refusal message, or ``None``.

        Everything is checked before anything is written: a patch with one bad row out
        of four must not half-apply, because the panel then shows what the operator
        typed while the engine still uses what it had.
        """
        stored = self._stored_rules()
        next_rules: dict[str, Any] = {code: dict(fields) for code, fields in stored.items()}
        rows = patch.get("alerts_rules")
        if rows is not None:
            if not isinstance(rows, Mapping):
                return "告警设置必须是 {编码: {enabled, threshold}} 的形状"
            for code, fields in rows.items():
                rule = RULE_BY_CODE.get(str(code))
                if rule is None:
                    return f"没有这条告警：{code}"
                if not isinstance(fields, Mapping):
                    return f"{rule.label} 的设置读不出来"
                row = next_rules.setdefault(str(code), {})
                if "enabled" in fields:
                    row["enabled"] = bool(fields["enabled"])
                if "threshold" in fields:
                    try:
                        value = float(str(fields["threshold"]).strip())
                    except (TypeError, ValueError):
                        return f"{rule.label} 的阈值得是个数字"
                    if not rule.low <= value <= rule.high:
                        return f"{rule.label} 的阈值只能在 {rule.low:g}~{rule.high:g} 之间"
                    row["threshold"] = value
        minutes = patch.get("alerts_cooldown_minutes")
        if minutes is not None:
            try:
                value = float(str(minutes).strip())
            except (TypeError, ValueError):
                return "重复提醒间隔得是个数字（分钟）"
            if not COOLDOWN_BOUNDS[0] <= value <= COOLDOWN_BOUNDS[1]:
                return f"重复提醒间隔只能在 {COOLDOWN_BOUNDS[0]:g}~{COOLDOWN_BOUNDS[1]:g} 分钟之间"
        if self._prefs is not None:
            if rows is not None:
                self._prefs.set(ALERTS_RULES, next_rules)
            if minutes is not None:
                self._prefs.set(ALERTS_COOLDOWN_MINUTES, float(value))
            if "alerts_speak_critical" in patch:
                self._prefs.set(ALERTS_SPEAK_CRITICAL, bool(patch["alerts_speak_critical"]))
        return None

    # -- the engine ----------------------------------------------------------

    def evaluate(self, snapshot: Any) -> tuple[Alert, ...]:
        """Re-read every rule against one snapshot. Returns what is active now."""
        now = self._clock()
        fired: list[Alert] = []
        with self._lock:
            cooldown = self._cooldown_seconds_locked()
            values: dict[str, float] = {}
            for reading in readings_of(snapshot):
                rule = reading.rule
                if not self._enabled(rule):
                    continue
                threshold = self._threshold(rule)
                key = _key(reading)
                # Recorded whether or not it is breaching: recovery is decided from this
                # round's reading, and an alert that only ever stores the value that
                # opened it can never observe itself getting better.
                values[key] = reading.value
                first = self._breaching(reading, threshold, now, cooldown)
                if first is None:
                    continue
                opened, alert = self._open_or_update(key, reading, threshold, first, now)
                if opened and alert is not None:
                    fired.append(alert)
            self._sweep(values, now)
            active = tuple(
                sorted(
                    (alert for alert in self._open.values() if not alert.acknowledged),
                    key=lambda item: item.since,
                )
            )
        for alert in fired:
            self._announce(alert)
        return active

    def _breaching(
        self, reading: _Reading, threshold: float, now: float, cooldown: float
    ) -> float | None:
        """When this reading started breaching, or ``None`` if it is not an alert yet.

        The returned timestamp is the first sample of the current run, so the sustained
        window is measured against reality rather than against "the last time we looked".
        """
        rule = reading.rule
        key = _key(reading)
        crossed = reading.value >= threshold if rule.fires_above else reading.value <= threshold
        if not crossed:
            self._rising.pop(key, None)
            return None
        first = self._rising.setdefault(key, now)
        if now - first < self._sustain:
            return None
        if now - self._closed_at.get(key, 0.0) < cooldown and key not in self._open:
            return None
        return first

    def _open_or_update(
        self,
        key: str,
        reading: _Reading,
        threshold: float,
        first: float,
        now: float,
    ) -> tuple[bool, Alert | None]:
        """Open the alert, or update the one already open. ``(opened, alert)``."""
        rule = reading.rule
        message = rule.message.format(
            value=reading.value, threshold=threshold, subject=reading.subject
        )
        severity = _severity(rule, reading.value, threshold)
        existing = self._open.get(key)
        if existing is None:
            alert = Alert(
                code=key,
                rule=rule.code,
                label=_label(rule, reading.subject),
                severity=severity,
                message=message,
                value=reading.value,
                threshold=threshold,
                unit=rule.unit,
                since=first,
                last_seen=now,
                count=reading.value,
            )
            self._open[key] = alert
            return True, alert
        updated = replace(
            existing, message=message, value=reading.value, severity=severity, last_seen=now
        )
        self._open[key] = updated
        return False, None

    def _sweep(self, values: Mapping[str, float], now: float) -> None:
        """Close anything that recovered, or whose subject disappeared.

        Only telemetry rules are swept: an alert opened by :meth:`note` has no reading
        to recover, so it is closed by acknowledging it instead. Sweeping those would
        make a failed job vanish on the next poll, which is a very short alarm.
        """
        for key in list(self._open):
            if key in self._manual:
                continue
            alert = self._open[key]
            rule = RULE_BY_CODE.get(alert.rule)
            if rule is None:  # pragma: no cover - a rule deleted between polls
                self._close(key, now)
                continue
            if key not in values:
                # No reading for this subject this round: the drive is gone (or was
                # never readable). Holding the alert open would be a claim about a
                # disk we are not looking at.
                self._close(key, now)
                continue
            value = values[key]
            margin = rule.clear_margin
            threshold = alert.threshold
            recovered = (
                value <= threshold - margin if rule.fires_above else value >= threshold + margin
            )
            if recovered:
                self._close(key, now)

    def _close(self, key: str, now: float) -> None:
        alert = self._open.pop(key, None)
        if alert is None:
            return
        self._rising.pop(key, None)
        self._closed_at[key] = now
        self._history = [*self._history, replace(alert, last_seen=now)][-_HISTORY_CAP:]
        logger.info("告警恢复：%s", alert.message)

    # -- what the pages ask for ---------------------------------------------

    def active(self) -> tuple[Alert, ...]:
        """What the box shows: open, and not yet acknowledged. Any thread.

        An acknowledged alert stays open inside the engine -- it has not recovered, and
        the cooldown clock must not start until it does. What "知道了" buys is silence on
        screen, not a claim that the disk got bigger.
        """
        with self._lock:
            return tuple(
                sorted(
                    (alert for alert in self._open.values() if not alert.acknowledged),
                    key=lambda item: item.since,
                )
            )

    def history(self) -> tuple[Alert, ...]:
        """Recovered alerts, newest first. In memory only -- see the module docstring."""
        with self._lock:
            return tuple(reversed(self._history))

    def acknowledge(self, code: str) -> bool:
        """Say "I have seen this".

        A telemetry alert stays open -- the disk is still full, and the cooldown clock
        should not start until it is not -- so it leaves the box rather than the engine.
        An event alert (a failed job) has no reading that can come back, so
        acknowledging it is what closes it.
        """
        now = self._clock()
        with self._lock:
            alert = self._open.get(str(code))
            if alert is None:
                return False
            if str(code) in self._manual:
                self._close(str(code), now)
            else:
                self._open[str(code)] = replace(alert, acknowledged=True)
        logger.info("告警已确认：%s", alert.message)
        return True

    def acknowledge_all(self) -> int:
        codes = [alert.code for alert in self.active()]
        return sum(1 for code in codes if self.acknowledge(code))

    def note(self, code: str, label: str, message: str, *, severity: str = SEVERITY_WARN) -> Alert:
        """Open an alert that no telemetry rule produced.

        This is how a failed scheduled job becomes something the operator sees instead
        of a line in a log file they will never open. It bypasses the sustained window
        on purpose: a job that raised has already happened, and there is nothing to
        confirm. The cooldown still applies, so a job that fails every minute does not
        speak every minute -- it updates in place, and after an acknowledgement it stays
        quiet until the cooldown runs out.
        """
        now = self._clock()
        with self._lock:
            existing = self._open.get(code)
            if existing is not None:
                updated = replace(existing, message=message, last_seen=now, severity=severity)
                self._open[code] = updated
                return updated
            if now - self._closed_at.get(code, 0.0) < self._cooldown_seconds_locked():
                logger.info("告警 %s 还在冷却里，只记不发：%s", code, message)
                return Alert(
                    code=code,
                    rule=code.split(":", 1)[0],
                    label=label,
                    severity=severity,
                    message=message,
                    value=0.0,
                    threshold=0.0,
                    unit="",
                    since=now,
                    last_seen=now,
                    count=0.0,
                    acknowledged=True,
                )
            alert = Alert(
                code=code,
                rule=code.split(":", 1)[0],
                label=label,
                severity=severity,
                message=message,
                value=0.0,
                threshold=0.0,
                unit="",
                since=now,
                last_seen=now,
                count=0.0,
            )
            self._open[code] = alert
            self._manual.add(code)
        self._announce(alert)
        return alert

    # -- internals -----------------------------------------------------------

    def _announce(self, alert: Alert) -> None:
        """Hand a newly opened alert to the shell, if the operator asked to be told."""
        handler = self.on_fire
        if handler is None or not self._speak_critical():
            return
        if alert.severity != SEVERITY_CRITICAL:
            return
        try:
            handler(alert)
        except Exception:  # a voice that fails must not lose the alert on screen
            logger.exception("语音提醒没发出去；告警还在界面上")

    def _stored_rules(self) -> dict[str, Mapping[str, Any]]:
        if self._prefs is None:
            return {}
        raw = self._prefs.get(ALERTS_RULES)
        if not isinstance(raw, Mapping):
            return {}
        return {str(code): fields for code, fields in raw.items() if isinstance(fields, Mapping)}

    def _threshold(self, rule: AlertRule) -> float:
        fields = self._stored_rules().get(rule.code) or {}
        try:
            value = float(fields.get("threshold", rule.default))
        except (TypeError, ValueError):
            return rule.default
        return value if rule.low <= value <= rule.high else rule.default

    def _enabled(self, rule: AlertRule) -> bool:
        fields = self._stored_rules().get(rule.code) or {}
        return bool(fields.get("enabled", True))

    def _cooldown_minutes(self) -> float:
        if self._prefs is None:
            return DEFAULT_COOLDOWN_MINUTES
        # ``real``, not ``number``: the latter promises an int and drops a float on the
        # floor, which would make "1 minute" stored as 1.0 read back as the default 10.
        raw = self._prefs.real(ALERTS_COOLDOWN_MINUTES, default=DEFAULT_COOLDOWN_MINUTES)
        return raw if COOLDOWN_BOUNDS[0] <= raw <= COOLDOWN_BOUNDS[1] else DEFAULT_COOLDOWN_MINUTES

    def _cooldown_seconds_locked(self) -> float:
        return self._cooldown_minutes() * 60.0

    def _speak_critical(self) -> bool:
        if self._prefs is None:
            return True
        return bool(self._prefs.flag(ALERTS_SPEAK_CRITICAL, default=True))


_HISTORY_CAP = 40
"""How many recovered alerts stay listed. Bounded so a long uptime cannot grow it."""


def _key(reading: _Reading) -> str:
    return reading.rule.code if not reading.subject else f"{reading.rule.code}:{reading.subject}"


def _label(rule: AlertRule, subject: str) -> str:
    return f"{rule.label} {subject}" if subject else rule.label


def _severity(rule: AlertRule, value: float, threshold: float) -> str:
    """How far past the line we are. The margin is in the rule's own unit."""
    if rule.fires_above:
        return SEVERITY_CRITICAL if value >= threshold + rule.critical_margin else SEVERITY_WARN
    return SEVERITY_CRITICAL if value <= threshold - rule.critical_margin else SEVERITY_WARN


__all__ = [
    "COOLDOWN_BOUNDS",
    "DEFAULT_COOLDOWN_MINUTES",
    "RULES",
    "SEVERITY_CRITICAL",
    "SEVERITY_WARN",
    "SUSTAIN_SECONDS",
    "Alert",
    "AlertRule",
    "AlertService",
    "readings_of",
]
