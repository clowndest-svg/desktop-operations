"""给手机连的电脑端点（自检用，不是正式入口）。

正式的「手机接入」在桌面壳里（托盘 / 顶栏），但那要占着用户的实例锁和真 key。
这里用同一套代码（`PairingVault` + `MobileGateway` + `HudBridge`）起一个端点，
好让手机上真点一次配对、真发一次 RPC。

哪些是真的、哪些是替身，全写在回答里，不留"看起来像真的"的余地：
  * 真：TLS 监听、配对码换 token、方法白名单、遥测（psutil 真读数）、提醒（真的
    ReminderService 解析"10 分钟后"）、磁盘服务、参数校验、401。
  * 替身：模型回答（这台机器的 shell 里没有 API key）、记忆两条、动作台账一条。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._fakes import FakeLlmClient  # noqa: E402

from jarvis.app.chat_service import ChatService  # noqa: E402
from jarvis.app.disk_service import DiskService  # noqa: E402
from jarvis.app.reminder_service import ReminderService  # noqa: E402
from jarvis.app.system_service import SystemService  # noqa: E402
from jarvis.app.transcript_service import TranscriptService  # noqa: E402
from jarvis.config.schema import MobileSection  # noqa: E402
from jarvis.database.store import SqliteStore  # noqa: E402
from jarvis.tools.disk_cleaner import DiskCleaner  # noqa: E402
from jarvis.tools.monitor import SystemMonitor  # noqa: E402
from jarvis.ui.desktop import HudBridge, build_mobile_gateway  # noqa: E402

SCRATCH = Path(__file__).resolve().parents[1] / "build" / "mobile-endpoint"
PORT = 8738  # 8737 被正在跑的 r22 占着


class _Scheduler:
    """提醒要的那个调度器切片：内存版，够验"手机写的提醒电脑能不能排上"。"""

    def __init__(self) -> None:
        self.jobs: dict[str, Any] = {}

    def add_job(self, spec: Any) -> Any:
        self.jobs[spec.job_id] = spec
        return spec

    def remove_job(self, job_id: str) -> bool:
        return self.jobs.pop(job_id, None) is not None

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        import dataclasses

        spec = self.jobs.get(job_id)
        if spec is None:
            return False
        self.jobs[job_id] = dataclasses.replace(spec, enabled=enabled)
        return True

    def list_jobs(self) -> list[Any]:
        return list(self.jobs.values())

    def next_run_time(self, job_id: str) -> str:
        spec = self.jobs.get(job_id)
        return str(getattr(spec, "expression", "")) if spec else ""


class _MemoryStub:
    def list_memories(self) -> list[dict[str, Any]]:
        return [
            {"id": 1, "kind": "preference", "text": "（替身）她回答喜欢短一点"},
            {"id": 2, "kind": "fact", "text": "（替身）这条不是真记忆库里的"},
        ]


class _LedgerEntry:
    """台账里的一条。桥面读的是 `call.to_dict()`，不是字典。"""

    def to_dict(self) -> dict[str, Any]:
        return {"at": time.time(), "tool": "system_report", "ok": True, "detail": "（替身）"}


class _LedgerStub:
    def recent(self) -> list[_LedgerEntry]:
        return [_LedgerEntry()]


def build() -> Any:
    sampler = SystemMonitor(top_processes=10)
    system = SystemService(lambda: sampler)
    system.start()
    # 会话本是真的：手机上「会话」那一页读的就是它，没有它那一页只会说"未启用"。
    store = SqliteStore(SCRATCH / "endpoint.db")
    store.start()
    transcript = TranscriptService(store)
    transcript.start()
    chat = ChatService(
        lambda: FakeLlmClient("（链路自检的桩回答，不是模型：这台机器的 shell 里没有 API key）"),
        transcript=transcript,
    )
    chat.start()
    return HudBridge(
        system,
        DiskService(lambda: DiskCleaner(audit_log=SCRATCH / "audit.jsonl", sources={})),
        chat=chat,
        reminders=ReminderService(lambda: _Scheduler()),
        memory=_MemoryStub(),
        tools=_LedgerStub(),
    )


def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for stale in (
        list(SCRATCH.glob("*.pem")) + list(SCRATCH.glob("*.json")) + list(SCRATCH.glob("*.db"))
    ):
        stale.unlink()
    bridge = build()
    section = MobileSection(
        enabled=False,
        bind_host="0.0.0.0",
        port=PORT,
        pair_minutes=10,
        max_pair_attempts=8,
        max_devices=4,
    )
    gateway = build_mobile_gateway(bridge, section, SCRATCH)
    assert gateway is not None
    if not gateway.enable():
        print(f"起不来：{gateway.reason}", flush=True)
        return 1
    state = gateway.state()
    print(f"URL {state['url']}", flush=True)
    print(f"PIN {state['fingerprint']}", flush=True)
    control = SCRATCH / "RECODE"
    last = ""
    try:
        while True:
            if control.exists() or not last:
                control.unlink(missing_ok=True)
                last = str(gateway.show_pair_code()["code"])
                print(f"CODE {last}", flush=True)
            time.sleep(1.0)
    except KeyboardInterrupt:
        gateway.disable()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
