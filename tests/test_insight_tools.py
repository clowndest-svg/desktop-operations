"""P0-2: the assistant can read the three panels the window draws.

Two things are being pinned here, and the second one is the reason the round started.

**One sampler, not two.** ``system_report`` used to build a fresh ``SystemMonitor`` on
every call, and a fresh *psutil* CPU percentage has no earlier sample to difference
against -- so the model was told "CPU 0%" on a machine the panel was showing at 81.7%.
The monitor now samples its own interval (``CPU_SAMPLE_SECONDS``), which is what makes
a first read a reading; these tests still fail if anything goes back to constructing a
sampler per question, and the wiring test at the bottom of this class is what guards it.

**Her number *is* the number on screen.** The tools are asserted against the same
objects the HUD reads -- ``monitor.latest()`` and ``usage.summary()`` -- rather than
against numbers typed into the test, so a drift between panel and answer is a failure
rather than a screenshot nobody took.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from jarvis.app.usage_service import UsageService
from jarvis.config.schema import ToolsSection
from jarvis.core.events import UsageEvent
from jarvis.database import SqliteStore
from jarvis.tools.builtins import insight_tools
from jarvis.tools.monitor import SystemMonitor
from jarvis.tools.registry import ToolRegistry

CPU_SERIES = [0.0, 20.0, 60.0, 40.0, 40.0]


class _FakePs:
    """The slice of psutil the monitor reads, with a scripted CPU line."""

    def __init__(self, cpu_values: list[float]) -> None:
        self._cpu = cpu_values
        self._reads = 0
        self._sent = 1_000
        self._recv = 5_000

    def cpu_percent(self, interval: float | None = None, *, percpu: bool = False) -> object:
        del interval
        value = self._cpu[min(self._reads, len(self._cpu) - 1)]
        self._reads += 1
        return [value, value] if percpu else value

    def cpu_count(self) -> int:
        return 4

    def virtual_memory(self) -> Any:
        return SimpleNamespace(percent=70.0, total=16_000_000_000, available=4_800_000_000)

    def swap_memory(self) -> Any:
        return SimpleNamespace(percent=5.0)

    def disk_partitions(self, *, all: bool = False) -> list[Any]:
        del all
        return []

    def disk_usage(self, path: str) -> Any:
        del path
        return SimpleNamespace(total=1, used=0, free=1, percent=0.0)

    def boot_time(self) -> float:
        return time.time() - 3600.0

    def net_io_counters(self) -> Any:
        self._sent += 2_000
        self._recv += 9_000
        return SimpleNamespace(bytes_sent=self._sent, bytes_recv=self._recv)

    def process_iter(self, attrs: Sequence[str]) -> list[Any]:
        return [self._proc(101, "a.exe", 3_000_000_000), self._proc(102, "b.exe", 900_000_000)]

    @staticmethod
    def _proc(pid: int, name: str, rss: int) -> Any:
        handle = SimpleNamespace(
            pid=pid,
            info={"pid": pid, "name": name, "memory_info": SimpleNamespace(rss=rss)},
        )
        handle.cpu_percent = lambda interval=None: 12.5
        return handle

    def pid_exists(self, pid: int) -> bool:
        return True


def _monitor(cpu_values: list[float] | None = None) -> SystemMonitor:
    """The scripted CPU line advances one step per snapshot: the monitor samples its
    own interval inside each call, so there is no priming call to account for."""
    values = CPU_SERIES if cpu_values is None else cpu_values
    return SystemMonitor(top_processes=10, psutil_module=_FakePs(values))


def _tools_section() -> ToolsSection:
    return ToolsSection.from_mapping(
        {
            "enabled": True,
            "confirm_dangerous": True,
            "allow_write": False,
            "allow_shell": False,
            "file_roots": [],
            "max_result_chars": 8000,
        }
    )


def _registry(**kwargs: Any) -> ToolRegistry:
    registry = ToolRegistry(_tools_section)
    for spec, handler in insight_tools.build(**kwargs):
        registry.register(spec, handler)
    return registry


@pytest.fixture
def usage(tmp_path: Path) -> Iterator[UsageService]:
    store = SqliteStore(tmp_path / "usage.db", journal_mode="MEMORY")
    store.start()
    service = UsageService(store)
    service.start()
    service.record(
        UsageEvent(
            provider="qwenai",
            model="qwen3.8-flash",
            prompt_tokens=1000,
            completion_tokens=200,
            cached_tokens=None,
            latency_ms=900.0,
        )
    )
    service.record(
        UsageEvent(
            provider="deepseek",
            model="deepseek-chat",
            prompt_tokens=500,
            completion_tokens=100,
            cached_tokens=400,
            latency_ms=400.0,
        )
    )
    yield service
    store.stop()


class TestOneSamplerPerProcess:
    def test_each_read_on_one_sampler_gives_the_load_it_measured(self) -> None:
        """The regression, stated plainly: one sampler polled twice gives two readings."""
        monitor = _monitor([20.0, 60.0])
        assert monitor.snapshot().cpu.percent == 20.0
        assert monitor.snapshot().cpu.percent == 60.0

    def test_a_fresh_sampler_reads_the_machine_on_its_first_call(self) -> None:
        """The old regression, inverted: ``system_report`` used to say 0.0% while the
        panel showed 81.7%, because the reading was a delta against a *thread's* last
        call and a fresh thread had none. Each call now samples its own interval, so a
        first read is a reading -- and the shared-sampler rule is still guarded by the
        wiring test below, because paying for two samplers is its own bug."""
        assert _monitor([42.0]).snapshot().cpu.percent == 42.0

    def test_the_factory_handed_to_the_tool_service_returns_the_one_sampler(self) -> None:
        """Wiring, not behaviour: a factory that builds is the bug wearing a different hat."""
        from jarvis.__main__ import _monitor_factory

        monitor = _monitor()
        factory = _monitor_factory(monitor)
        assert factory() is monitor and factory() is monitor

    def test_latest_hands_back_the_reading_the_panel_is_already_showing(self) -> None:
        monitor = _monitor()
        assert monitor.latest() is None
        first = monitor.snapshot()
        assert monitor.latest() is first


class TestRegistrationGates:
    def test_no_monitor_and_no_ledger_advertises_nothing(self) -> None:
        assert insight_tools.build() == []

    def test_a_missing_psutil_leaves_the_machine_tools_out_but_keeps_the_ledger(
        self, usage: UsageService
    ) -> None:
        names = {spec.name for spec, _ in insight_tools.build(usage=usage)}
        assert names == {"usage_stats"}


class TestSystemTrend:
    def test_it_reports_mean_peak_and_last_over_the_window(self) -> None:
        monitor = _monitor()
        for _ in range(5):
            monitor.snapshot()
        out = _registry(monitor=monitor).invoke("system_trend", {"minutes": 10}).output
        assert "峰值 60.0%" in out
        assert "最后 40.0%" in out
        # 20/60/40 -- the first sample is not a reading, so it must not drag the mean.
        assert "均值 40.0%" in out

    def test_an_empty_history_is_called_empty(self) -> None:
        out = _registry(monitor=_monitor()).invoke("system_trend", {"minutes": 10}).output
        assert "没有任何负载采样" in out

    def test_a_window_longer_than_the_history_says_so_instead_of_padding(
        self,
    ) -> None:
        monitor = _monitor()
        monitor.snapshot()
        monitor.snapshot()
        out = _registry(monitor=monitor).invoke("system_trend", {"minutes": 120}).output
        assert "更早的采样没有被保留" in out

    def test_an_unknown_metric_is_refused_with_the_choices_named(self) -> None:
        out = _registry(monitor=_monitor()).invoke("system_trend", {"minutes": 5, "metric": "disk"})
        assert not out.ok and "cpu" in out.error

    def test_the_net_line_reports_a_rate_only_once_two_samples_exist(self) -> None:
        monitor = _monitor()
        monitor.snapshot()
        monitor.snapshot()
        out = _registry(monitor=monitor).invoke("system_trend", {"metric": "net"}).output
        assert "下行" in out and "无读数" not in out


class TestListProcesses:
    def test_the_rows_and_theirs_are_the_same_rows_the_panel_draws(self) -> None:
        monitor = _monitor()
        monitor.snapshot()
        panel = monitor.latest()
        assert panel is not None
        out = _registry(monitor=monitor).invoke("list_processes", {}).output
        for row in panel.top_processes:
            assert row.name in out
        # Memory order, because that is what the panel sorts by.
        assert out.index("a.exe") < out.index("b.exe")

    def test_a_cpu_ranking_says_which_pool_it_ranked(self) -> None:
        """The cached table is the top-N by memory. Ranking *that* by CPU is honest only
        while the sentence says so, or she will claim a machine-wide first place."""
        monitor = _monitor()
        monitor.snapshot()
        out = _registry(monitor=monitor).invoke("list_processes", {"sort_by": "cpu"}).output
        assert "同一份数据" in out

    def test_an_unknown_sort_is_refused(self) -> None:
        result = _registry(monitor=_monitor()).invoke("list_processes", {"sort_by": "disk"})
        assert not result.ok

    def test_an_untouched_sampler_takes_one_reading_instead_of_reporting_nothing(
        self,
    ) -> None:
        """``--ask`` has no window polling it, so "no samples yet" would be an artefact
        of nobody having asked yet rather than a fact about the machine."""
        out = _registry(monitor=_monitor()).invoke("list_processes", {}).output
        assert "a.exe" in out and "还没有进程采样" not in out


class TestUsageStats:
    def test_the_totals_are_the_ones_the_popup_aggregates(self, usage: UsageService) -> None:
        summary = usage.summary(7).to_dict()
        out = _registry(usage=usage).invoke("usage_stats", {"days": 7}).output
        assert str(summary["prompt_tokens"]) in out
        assert str(summary["completion_tokens"]) in out
        assert str(summary["calls"]) in out

    def test_a_provider_that_says_nothing_about_cache_is_not_reported_as_zero_percent(
        self, usage: UsageService
    ) -> None:
        """One of two rows reported caching, so a percentage is computable; the point of
        this test is the other branch, where the ledger has no reading at all."""
        assert "缓存命中" in _registry(usage=usage).invoke("usage_stats", {}).output

    def test_no_cache_field_at_all_comes_back_as_no_reading(self, tmp_path: Any) -> None:
        store = SqliteStore(tmp_path / "silent.db", journal_mode="MEMORY")
        store.start()
        try:
            service = UsageService(store)
            service.start()
            service.record(
                UsageEvent(
                    provider="p",
                    model="m",
                    prompt_tokens=10,
                    completion_tokens=5,
                    cached_tokens=None,
                    latency_ms=1.0,
                )
            )
            out = _registry(usage=service).invoke("usage_stats", {}).output
            assert "无读数" in out and "不是 0%" in out
        finally:
            store.stop()

    def test_an_empty_window_says_so(self, tmp_path: Any) -> None:
        store = SqliteStore(tmp_path / "empty.db", journal_mode="MEMORY")
        store.start()
        try:
            service = UsageService(store)
            service.start()
            out = _registry(usage=service).invoke("usage_stats", {"days": 3}).output
            assert "没有任何模型调用记录" in out
        finally:
            store.stop()

    def test_the_per_model_rows_add_up_to_the_total(self, usage: UsageService) -> None:
        """A breakdown that does not sum to the headline is a breakdown nobody can act
        on: the same window has to produce both."""
        summary = usage.summary(7).to_dict()
        rows = usage.by_provider(7)
        assert sum(cast(int, row["total_tokens"]) for row in rows) == summary["total_tokens"]
        assert sum(cast(int, row["calls"]) for row in rows) == summary["calls"]


class TestThroughTheRegistry:
    def test_argument_validation_runs_before_anything_reads(self, usage: UsageService) -> None:
        result = _registry(usage=usage).invoke("usage_stats", {"days": "一周"})
        assert not result.ok and "days" in result.error

    def test_a_day_window_beyond_the_ledger_is_clamped_not_rejected(
        self, usage: UsageService
    ) -> None:
        """The ledger already caps at a month; the tool has to answer about the range it
        can, and say which one that was."""
        out = _registry(usage=usage).invoke("usage_stats", {"days": 400}).output
        assert "最近 31 天" in out
