"""System instrumentation: the read-only half of the built-in tool set.

psutil is a third-party SDK, so by the layering rules it lives here in L2 and
never surfaces above the application layer. The UI consumes a snapshot dict
through :class:`jarvis.app.system_service.SystemService`, not this module.

Everything here is *read-only* on purpose. Mutating the machine (deleting files,
killing processes) is a different, far more dangerous concern and does not belong
in a monitoring sampler.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from jarvis.core.exceptions import ToolError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

logger = logging.getLogger("jarvis.tools.monitor")

_DEFAULT_TOP_PROCESSES = 8


class Pslike(Protocol):
    """The slice of ``psutil`` this module uses, so tests can drive it directly."""

    def cpu_count(self) -> int: ...

    def cpu_percent(self, interval: float | None = None, *, percpu: bool = ...) -> Any: ...

    def virtual_memory(self) -> Any: ...

    def swap_memory(self) -> Any: ...

    def disk_partitions(self, *, all: bool = ...) -> list[Any]: ...

    def disk_usage(self, path: str) -> Any: ...

    def boot_time(self) -> float: ...

    def process_iter(self, attrs: Sequence[str]) -> list[Any]: ...

    def pid_exists(self, pid: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class CpuReading:
    percent: float
    cores: int
    per_core: tuple[float, ...] = ()
    frequencies_mhz: float | None = None


@dataclass(frozen=True, slots=True)
class MemoryReading:
    percent: float
    total_bytes: int
    used_bytes: int
    available_bytes: int
    swap_percent: float = 0.0


@dataclass(frozen=True, slots=True)
class DiskReading:
    mount: str
    fstype: str
    percent: float
    total_bytes: int
    used_bytes: int
    free_bytes: int


@dataclass(frozen=True, slots=True)
class ProcessReading:
    pid: int
    name: str
    cpu_percent: float
    memory_bytes: int


@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    """One point-in-time reading of the machine, safe to hand to the UI."""

    taken_at: float
    uptime_seconds: float
    cpu: CpuReading
    memory: MemoryReading
    disks: tuple[DiskReading, ...] = ()
    top_processes: tuple[ProcessReading, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    """Per-metric failures that were skipped rather than failing the snapshot.

    A partition we cannot stat or a process that exits mid-scan is normal on
    Windows; dropping them silently would make the dashboard look healthy when
    it is only partially read.
    """

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form for the desktop bridge."""
        return asdict(self)


def _import_psutil() -> Pslike:
    try:
        import psutil
    except ImportError as exc:  # pragma: no cover - environment specific
        raise ToolError(
            "system monitoring requires the 'psutil' package",
            details={"missing_package": "psutil"},
        ) from exc
    return psutil  # type: ignore[no-any-return]


class SystemMonitor:
    """Samples CPU / memory / disks / top processes on demand."""

    def __init__(
        self, *, top_processes: int = _DEFAULT_TOP_PROCESSES, psutil_module: Pslike | None = None
    ) -> None:
        self._ps = psutil_module or _import_psutil()
        self._top = max(0, top_processes)
        # psutil computes cpu_percent from the delta since its previous call and
        # returns 0.0 when it has no earlier sample. Priming here means the
        # *second* snapshot onwards is meaningful; the first one still reads ~0
        # because no time has elapsed to measure. A polling UI recovers
        # immediately, but do not show that first number as a real load.
        self._ps.cpu_percent(interval=None)

    def snapshot(self) -> SystemSnapshot:
        """Read every metric, collecting per-metric failures as warnings."""
        warnings: list[str] = []
        cpu = self._read_cpu(warnings)
        memory = self._read_memory(warnings)
        disks = self._read_disks(warnings)
        processes = self._read_top_processes(warnings)
        return SystemSnapshot(
            taken_at=time.time(),
            uptime_seconds=self._read_uptime(warnings),
            cpu=cpu,
            memory=memory,
            disks=disks,
            top_processes=processes,
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------------
    # Samplers
    # ------------------------------------------------------------------

    def _read_cpu(self, warnings: list[str]) -> CpuReading:
        try:
            percent = float(self._ps.cpu_percent(interval=None))
            per_core = tuple(
                float(value) for value in self._ps.cpu_percent(interval=None, percpu=True)
            )
            cores = int(self._ps.cpu_count() or 0)
        except Exception as exc:  # pragma: no cover - platform specific
            warnings.append(f"cpu: {type(exc).__name__}: {exc}")
            return CpuReading(percent=0.0, cores=0)
        return CpuReading(percent=percent, cores=cores, per_core=per_core)

    def _read_memory(self, warnings: list[str]) -> MemoryReading:
        try:
            vm = self._ps.virtual_memory()
            swap = self._ps.swap_memory()
        except Exception as exc:  # pragma: no cover - platform specific
            warnings.append(f"memory: {type(exc).__name__}: {exc}")
            return MemoryReading(percent=0.0, total_bytes=0, used_bytes=0, available_bytes=0)
        return MemoryReading(
            percent=float(vm.percent),
            total_bytes=int(vm.total),
            used_bytes=int(vm.total) - int(vm.available),
            available_bytes=int(vm.available),
            swap_percent=float(getattr(swap, "percent", 0.0)),
        )

    def _read_disks(self, warnings: list[str]) -> tuple[DiskReading, ...]:
        readings: list[DiskReading] = []
        try:
            partitions = list(self._ps.disk_partitions(all=False))
        except Exception as exc:  # pragma: no cover - platform specific
            warnings.append(f"disks: {type(exc).__name__}: {exc}")
            return ()
        for part in partitions:
            try:
                usage = self._ps.disk_usage(part.mountpoint)
            except (PermissionError, OSError) as exc:
                # An empty card reader or a protected drive is routine on Windows;
                # skip that mount, but say so rather than showing a shorter list.
                warnings.append(f"disk {part.mountpoint}: {type(exc).__name__}")
                continue
            readings.append(
                DiskReading(
                    mount=str(part.mountpoint),
                    fstype=str(getattr(part, "fstype", "") or ""),
                    percent=float(usage.percent),
                    total_bytes=int(usage.total),
                    used_bytes=int(usage.used),
                    free_bytes=int(usage.free),
                )
            )
        return tuple(readings)

    def _read_top_processes(self, warnings: list[str]) -> tuple[ProcessReading, ...]:
        if self._top == 0:
            return ()
        rows: list[ProcessReading] = []
        try:
            procs = list(self._ps.process_iter(["pid", "name", "cpu_percent", "memory_info"]))
        except Exception as exc:  # pragma: no cover - platform specific
            warnings.append(f"processes: {type(exc).__name__}: {exc}")
            return ()
        for proc in procs:
            info = getattr(proc, "info", {}) or {}
            memory = info.get("memory_info")
            rows.append(
                ProcessReading(
                    pid=int(info.get("pid") or 0),
                    name=str(info.get("name") or "?"),
                    cpu_percent=float(info.get("cpu_percent") or 0.0),
                    memory_bytes=int(getattr(memory, "rss", 0) or 0),
                )
            )
        rows.sort(key=lambda row: row.memory_bytes, reverse=True)
        return tuple(rows[: self._top])

    def _read_uptime(self, warnings: list[str]) -> float:
        try:
            return max(0.0, time.time() - float(self._ps.boot_time()))
        except Exception as exc:  # pragma: no cover - platform specific
            warnings.append(f"uptime: {type(exc).__name__}: {exc}")
            return 0.0
