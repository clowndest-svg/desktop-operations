"""Create synthetic history in a NEW isolated home; never modify an existing home."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.app.transcript_service import TranscriptService
from jarvis.database import SqliteStore


def seed(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=False)
    (home / "database").mkdir()
    store = SqliteStore(home / "database/jarvis.db")
    store.start()
    try:
        transcript = TranscriptService(store)
        transcript.start()
        key = transcript.new_session("QA 完整长对话", "qa-history-long")
        for n in range(262):
            text = f"验收消息 {n:04}"
            if n == 0:
                text += "：预算 100%_O'Reilly Python，只在正文出现"
            transcript.append(key, "user" if n % 2 == 0 else "assistant", text)
        for n in range(65):
            key = transcript.new_session(f"QA 项目 {n:02}", f"qa-history-{n:02}")
            transcript.append(key, "user", f"独立验收问题 {n}")
            transcript.append(key, "assistant", f"独立验收回答 {n}")
    finally:
        store.stop()
    (home / "history-qa.json").write_text(json.dumps({"synthetic": True}), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    seed(parser.parse_args().home)
