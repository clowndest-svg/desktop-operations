"""Tests for :mod:`jarvis.app.alerts` —— 告警中心的那台状态机。

这里最要紧的不是"能不能报出来"，而是**会不会乱报**。一个每次轮询都重新响一次的告警，
一天之内就会被用户彻底忽略，然后真正的事故也一起被忽略。所以持续窗口、回差、冷却、
确认这四件事每条都要有反例：光有"越线就报"的测试，删掉冷却也照样全绿。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jarvis.app.alerts import (
    DEFAULT_COOLDOWN_MINUTES,
    SEVERITY_CRITICAL,
    SEVERITY_WARN,
    AlertService,
    readings_of,
)
from jarvis.app.preferences import (
    ALERTS_COOLDOWN_MINUTES,
    ALERTS_RULES,
    ALERTS_SPEAK_CRITICAL,
    Preferences,
)


class _Clock:
    """A clock the test turns, so a 60-second window costs nothing."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def snapshot(
    *,
    cpu: float | None = 10.0,
    memory: float | None = 30.0,
    swap: float = 0.0,
    disks: tuple[tuple[str, int], ...] = (("C:", 500 * 1024**3),),
) -> Any:
    import types

    return types.SimpleNamespace(
        cpu=None if cpu is None else types.SimpleNamespace(percent=cpu, cores=12),
        memory=(
            None if memory is None else types.SimpleNamespace(percent=memory, swap_percent=swap)
        ),
        disks=tuple(
            types.SimpleNamespace(mount=mount, free_bytes=free, percent=0.0)
            for mount, free in disks
        ),
        warnings=(),
    )


def _service(
    prefs: Preferences | None = None,
    *,
    sustain: float = 60.0,
    clock: _Clock | None = None,
    on_fire: Any = None,
) -> tuple[AlertService, _Clock]:
    tick = clock or _Clock()
    return (
        AlertService(prefs, on_fire=on_fire, clock=tick, sustain_seconds=sustain),
        tick,
    )


def _fire(service: AlertService, tick: _Clock, snap: Any) -> list[Any]:
    """Feed one reading until the sustained window is behind it, or give up."""
    for _step in range(3):
        fired = list(service.evaluate(snap))
        if fired:
            return fired
        tick.advance(61)
    return []


class TestSustainedWindow:
    def test_one_hot_sample_is_not_an_incident(self) -> None:
        service, tick = _service()
        assert service.evaluate(snapshot(cpu=99.0)) == ()
        assert tick.now == 1000.0, "时钟是测试拨的，服务不许自己改"

    def test_the_same_reading_held_for_a_minute_is(self) -> None:
        service, tick = _service()
        service.evaluate(snapshot(cpu=99.0))
        tick.advance(30)
        assert service.evaluate(snapshot(cpu=99.0)) == (), "半分钟还不算"
        tick.advance(31)
        fired = service.evaluate(snapshot(cpu=99.0))
        assert [alert.code for alert in fired] == ["cpu"]
        assert fired[0].message.startswith("CPU 已经连着占用 99%")

    def test_the_window_measures_the_current_run_not_the_last_look(self) -> None:
        """越线 → 回落 → 再越线：第二次的计时要从第二次算起。"""
        service, tick = _service()
        service.evaluate(snapshot(cpu=99.0))
        tick.advance(40)
        service.evaluate(snapshot(cpu=10.0))
        tick.advance(20)
        assert service.evaluate(snapshot(cpu=99.0)) == (), "断过一次就不该接着上一次的账"
        tick.advance(61)
        assert [alert.code for alert in service.evaluate(snapshot(cpu=99.0))] == ["cpu"]


class TestHysteresisAndCooldown:
    def test_it_does_not_close_the_moment_the_line_is_touched_again(self) -> None:
        """90% 开、89.9% 关 = 在边界上闪。闪的告警会被用户当成噪声。"""
        service, tick = _service()
        assert _fire(service, tick, snapshot(cpu=95.0)), "先让它开着"
        tick.advance(5)
        assert [alert.code for alert in service.evaluate(snapshot(cpu=87.0))] == ["cpu"]
        tick.advance(5)
        assert service.evaluate(snapshot(cpu=80.0)) == (), "掉出回差带才该收"

    def test_a_recovered_alert_stays_quiet_for_the_cooldown(self) -> None:
        service, tick = _service()
        _fire(service, tick, snapshot(cpu=95.0))
        service.evaluate(snapshot(cpu=10.0))
        tick.advance(30)
        assert service.evaluate(snapshot(cpu=99.0)) == (), "刚恢复就再报一次是骚扰"
        tick.advance(60 * DEFAULT_COOLDOWN_MINUTES)
        assert [alert.code for alert in _fire(service, tick, snapshot(cpu=99.0))] == ["cpu"]

    def test_the_cooldown_is_the_operators_to_set(self, tmp_path: Path) -> None:
        prefs = Preferences(tmp_path / "preferences.json")
        prefs.set(ALERTS_COOLDOWN_MINUTES, 1.0)
        service, tick = _service(prefs)
        _fire(service, tick, snapshot(cpu=95.0))
        service.evaluate(snapshot(cpu=10.0))
        assert [alert.code for alert in _fire(service, tick, snapshot(cpu=99.0))] == ["cpu"]


class TestSubjects:
    def test_every_drive_gets_its_own_alert(self) -> None:
        """旧实现只看系统盘，D 盘满了没人说 —— 现在每块盘一条，各自能确认。"""
        service, tick = _service()
        small = 2 * 1024**3
        three = snapshot(disks=(("C:", small), ("D:", small), ("E:", 900 * 1024**3)))
        fired = _fire(service, tick, three)
        assert [alert.code for alert in fired] == ["disk:C:", "disk:D:"]
        assert "E:" not in " ".join(alert.code for alert in service.active())

    def test_acknowledging_one_drive_leaves_the_other_visible(self) -> None:
        service, tick = _service()
        small = 2 * 1024**3
        _fire(service, tick, snapshot(disks=(("C:", small), ("D:", small))))
        assert service.acknowledge("disk:C:") is True
        assert [alert.code for alert in service.active()] == ["disk:D:"]
        assert service.acknowledge("没有这个东西") is False

    def test_a_drive_that_stops_being_read_closes_its_alert(self) -> None:
        """盘拔了 / 读不到了：继续挂着一条关于看不见的东西的告警是假话。"""
        service, tick = _service()
        _fire(service, tick, snapshot(disks=(("C:", 2 * 1024**3),)))
        tick.advance(5)
        assert service.evaluate(snapshot(disks=())) == ()
        assert [alert.code for alert in service.history()] == ["disk:C:"]

    def test_a_machine_with_no_swap_never_alerts_about_swap(self) -> None:
        service, tick = _service()
        assert _fire(service, tick, snapshot(swap=0.0, memory=99.0)) != []
        assert "swap" not in [alert.code for alert in service.active()]


class TestSeverityAndVoice:
    def test_critical_is_a_distance_past_the_line_not_a_different_rule(self) -> None:
        service, tick = _service()
        fired = _fire(service, tick, snapshot(cpu=92.0))
        assert fired[0].severity == SEVERITY_WARN
        service.evaluate(snapshot(cpu=10.0))
        tick.advance(60 * DEFAULT_COOLDOWN_MINUTES + 1)
        again = _fire(service, tick, snapshot(cpu=99.0))
        assert again[0].severity == SEVERITY_CRITICAL

    def test_only_a_new_critical_reaches_the_voice(self) -> None:
        spoken: list[str] = []
        service, tick = _service(on_fire=lambda alert: spoken.append(alert.message))
        _fire(service, tick, snapshot(cpu=92.0))
        assert spoken == [], "warn 不许开口"
        service.evaluate(snapshot(cpu=10.0))
        tick.advance(60 * DEFAULT_COOLDOWN_MINUTES + 1)
        _fire(service, tick, snapshot(cpu=99.0))
        assert len(spoken) == 1 and "99%" in spoken[0]
        tick.advance(1)
        service.evaluate(snapshot(cpu=99.0))
        assert len(spoken) == 1, "同一条告警每轮播一次是闹钟，不是提醒"

    def test_the_voice_can_be_turned_off_without_turning_the_box_off(self, tmp_path: Path) -> None:
        prefs = Preferences(tmp_path / "preferences.json")
        prefs.set(ALERTS_SPEAK_CRITICAL, False)
        spoken: list[str] = []
        service, tick = _service(prefs, on_fire=lambda alert: spoken.append(alert.message))
        _fire(service, tick, snapshot(cpu=99.0))
        assert spoken == []
        assert [alert.code for alert in service.active()] == ["cpu"]

    def test_a_voice_that_throws_does_not_lose_the_alert(self) -> None:
        def broken(_alert: Any) -> None:
            raise RuntimeError("没有可用的播报通道")

        service, tick = _service(on_fire=broken)
        _fire(service, tick, snapshot(cpu=99.0))
        assert [alert.code for alert in service.active()] == ["cpu"]


class TestSettings:
    def test_defaults_apply_until_someone_writes_over_them(self, tmp_path: Path) -> None:
        prefs = Preferences(tmp_path / "preferences.json")
        service, _tick = _service(prefs)
        rules = {row["code"]: row for row in service.settings()["rules"]}
        assert rules["disk"]["threshold"] == 20.0
        assert rules["cpu"]["enabled"] is True

        outcome = service.apply_settings({"alerts_rules": {"disk": {"threshold": 80}}})
        assert outcome is None
        after = {row["code"]: row for row in service.settings()["rules"]}
        assert after["disk"]["threshold"] == 80.0

    def test_a_threshold_can_switch_a_rule_off(self, tmp_path: Path) -> None:
        prefs = Preferences(tmp_path / "preferences.json")
        service, tick = _service(prefs)
        assert service.apply_settings({"alerts_rules": {"cpu": {"enabled": False}}}) is None
        assert _fire(service, tick, snapshot(cpu=99.0)) == []

    def test_a_bad_row_refuses_the_whole_patch(self, tmp_path: Path) -> None:
        """四条里有一条是错的，不能"其余三条照存" —— 面板会显示用户输入的那套，
        引擎用的是另一套，而两边都说保存成功。"""
        prefs = Preferences(tmp_path / "preferences.json")
        service, _tick = _service(prefs)

        outcome = service.apply_settings(
            {"alerts_rules": {"cpu": {"threshold": 60}, "memory": {"threshold": 200}}}
        )

        assert outcome is not None and "内存" in outcome
        stored = prefs.get(ALERTS_RULES) or {}
        assert "cpu" not in stored

    def test_out_of_range_and_unknown_are_both_refused(self) -> None:
        service, _tick = _service()
        assert "磁盘" in str(service.apply_settings({"alerts_rules": {"disk": {"threshold": 0}}}))
        assert "没有这条告警" in str(service.apply_settings({"alerts_rules": {"gpu": {}}}))
        assert "分钟" in str(service.apply_settings({"alerts_cooldown_minutes": "很多"}))
        assert service.apply_settings({"alerts_cooldown_minutes": 900}) is not None

    def test_the_stored_shape_survives_a_restart(self, tmp_path: Path) -> None:
        """写进去的必须读得回来 —— 这条同时钉住"值是能序列化的那种"。"""
        prefs = Preferences(tmp_path / "preferences.json")
        service, _tick = _service(prefs)
        patch = {"alerts_rules": {"cpu": {"threshold": 70, "enabled": True}}}
        assert service.apply_settings(patch) is None

        reopened = AlertService(Preferences(tmp_path / "preferences.json"))
        rules = {row["code"]: row for row in reopened.settings()["rules"]}
        assert rules["cpu"]["threshold"] == 70.0


class TestAlertsThatAreNotTelemetry:
    def test_a_failed_job_is_an_alert_immediately(self) -> None:
        """任务失败只写日志 = 没人会去开的文件。它得进同一个框。"""
        service, tick = _service()
        alert = service.note("job:清理缓存", "定时任务", "清理缓存 这一轮失败了：TimeoutError")
        assert alert.severity == SEVERITY_WARN
        assert [item.code for item in service.active()] == ["job:清理缓存"]
        assert tick.now == 1000.0, "没有持续窗口这回事：它已经发生了"

    def test_the_same_job_failing_again_updates_instead_of_shouting_twice(self) -> None:
        spoken: list[str] = []
        service, _tick = _service(on_fire=lambda alert: spoken.append(alert.message))
        service.note("job:x", "定时任务", "第一次失败", severity=SEVERITY_CRITICAL)
        service.note("job:x", "定时任务", "还是失败", severity=SEVERITY_CRITICAL)
        assert len(spoken) == 1
        assert [alert.message for alert in service.active()] == ["还是失败"]

    def test_an_acknowledged_job_alert_stays_quiet_until_the_cooldown_passes(self) -> None:
        """确认 = 这条我认了；同一件事在冷却里再失败一次，不该再弹一次。"""
        service, tick = _service()
        service.note("job:x", "定时任务", "失败了", severity=SEVERITY_CRITICAL)
        assert service.acknowledge("job:x") is True
        assert service.active() == (), "事件型告警确认完就该关掉"
        quiet = service.note("job:x", "定时任务", "又失败了", severity=SEVERITY_CRITICAL)
        assert service.active() == () and quiet.acknowledged is True
        tick.advance(60 * DEFAULT_COOLDOWN_MINUTES + 1)
        service.note("job:x", "定时任务", "第三次失败", severity=SEVERITY_CRITICAL)
        assert [alert.message for alert in service.active()] == ["第三次失败"]


class TestReadingTheSnapshot:
    def test_a_missing_metric_produces_no_reading_rather_than_a_crash(self) -> None:
        """读不到的那一项是 warnings 的活，不是告警的活 —— 别把"没读到"报成"满了"。"""
        service, tick = _service()
        assert _fire(service, tick, snapshot(cpu=None, memory=None, disks=())) == []
        assert readings_of(snapshot(cpu=None, memory=None, disks=())) == []

    def test_a_garbage_snapshot_is_survivable(self) -> None:
        assert readings_of(object()) == []


@pytest.fixture()
def prefs(tmp_path: Path) -> Preferences:
    return Preferences(tmp_path / "preferences.json")


class TestNoPreferencesObject:
    def test_running_without_a_store_falls_back_to_the_shipped_lines(self) -> None:
        service, tick = _service(None)
        assert service.settings()["cooldown_minutes"] == DEFAULT_COOLDOWN_MINUTES
        assert [alert.code for alert in _fire(service, tick, snapshot(cpu=99.0))] == ["cpu"]

    def test_a_hand_edited_junk_value_does_not_break_the_line(self, prefs: Preferences) -> None:
        prefs.set(ALERTS_RULES, {"cpu": {"threshold": "很多", "enabled": "yes"}})
        service, tick = _service(prefs)
        rules = {row["code"]: row for row in service.settings()["rules"]}
        assert rules["cpu"]["threshold"] == 90.0, "坏值退到默认，不是退到 0"
        assert [alert.code for alert in _fire(service, tick, snapshot(cpu=99.0))] == ["cpu"]


def _monitor() -> Any:
    """A real SystemMonitor over a fake psutil: the tool reads it the way the window does."""
    from jarvis.tools.monitor import SystemMonitor
    from tests.test_insight_tools import _FakePs

    ps: Any = _FakePs([0.0, 0.0])
    ps.process_iter = lambda attrs: []
    return SystemMonitor(top_processes=5, psutil_module=ps)


class TestSheCanReadHerOwnAlerts:
    """``system_report`` has to carry what the box shows, through the whole chain.

    These go ``ToolService`` → ``build_builtin_tools`` → the handler, because the failure
    this feature invites is a service handed to one layer and dropped at the next: every
    unit test stays green while the model simply cannot see the panel. That is the same
    hole ``tests/test_builtin_tool_surface.py`` exists to catch, and a direct call into
    ``system_tools.build`` would not catch it.
    """

    @staticmethod
    def _service(tmp_path: Path, **injected: Any) -> Any:
        from jarvis.config.schema import ToolsSection
        from jarvis.tools.disk_cleaner import DiskCleaner
        from jarvis.tools.service import ToolService

        section = ToolsSection.from_mapping(
            {
                "enabled": True,
                "confirm_dangerous": True,
                "allow_write": False,
                "allow_shell": False,
                "file_roots": [str(tmp_path)],
                "max_result_chars": 4000,
            }
        )
        service = ToolService(
            lambda: section,
            monitor_factory=_monitor,
            cleaner_factory=lambda: DiskCleaner(tmp_path / "audit.jsonl", sources={}),
            **injected,
        )
        service.start()  # registration is where a dropped forward would show up
        return service

    @staticmethod
    def _text(service: Any) -> str:
        result = service.invoke("system_report")
        assert result.ok, f"工具自己失败了：{result.error}"
        return str(result.output)

    def test_an_open_alert_appears_in_her_report(self, tmp_path: Path) -> None:
        from jarvis.app.alerts import SEVERITY_CRITICAL

        centre = AlertService(sustain_seconds=0.0)
        centre.note(
            "disk:C:",
            "磁盘剩余空间 C:",
            "C: 可用只剩 8.0 GB，低于告警线 20 GB",
            severity=SEVERITY_CRITICAL,
        )
        service = self._service(tmp_path, alerts=centre)

        text = self._text(service)

        assert "磁盘剩余空间" in text or "8.0 GB" in text
        assert "critical" in text, "严重度和框里说的一样"

    def test_no_alert_centre_is_silent_about_alerts(self, tmp_path: Path) -> None:
        """Not wired must not read as "all clear".

        A line like 「没有告警」 from a process that has no alert centre is a claim about
        the machine, and she would hand it to the operator as reassurance.
        """
        service = self._service(tmp_path)

        text = self._text(service)

        assert "告警" not in text, f"没接中心却报告了告警状态：{text!r}"

    def test_a_quiet_centre_says_so_out_loud(self, tmp_path: Path) -> None:
        service = self._service(tmp_path, alerts=AlertService())

        text = self._text(service)

        assert "此刻没有还开着的告警" in text

    def test_an_acknowledged_alert_leaves_her_report_with_the_box(self, tmp_path: Path) -> None:
        """One source of truth: 「知道了」 clears it from the screen and from her at once."""
        centre = AlertService()
        centre.note("job:7", "定时任务", "「每晚整理下载」这一轮失败了：磁盘写入被拒绝")
        service = self._service(tmp_path, alerts=centre)
        assert "每晚整理下载" in self._text(service)

        assert centre.acknowledge("job:7") is True

        assert "每晚整理下载" not in self._text(service)

    def test_she_is_told_how_it_clears_without_being_given_the_button(self, tmp_path: Path) -> None:
        """The report explains the human-only half instead of letting her guess.

        She must not conclude that acknowledging is a step she can take: 知道了 is how a
        person stops an interruption, and a model that could press it could mute the
        operator's own machine out from under them.
        """
        centre = AlertService()
        centre.note("disk:C:", "磁盘剩余空间 C:", "C: 可用只剩 8.0 GB，低于告警线 20 GB")
        service = self._service(tmp_path, alerts=centre)

        text = self._text(service)

        assert "只有人能点" in text
        assert service.registry is not None
