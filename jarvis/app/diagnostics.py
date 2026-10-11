"""Read-only release diagnostics. No user config, microphone, network or model is opened."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator, Sequence
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from jarvis import __version__
from jarvis.app.chat_service import ChatService
from jarvis.config.loader import load_defaults
from jarvis.config.schema import AppConfig
from jarvis.database import SqliteStore
from jarvis.llm.types import ChatMessage, ChatResponse, GenerationOptions, StreamChunk


class _Assets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        row = dict(attrs)
        value = (
            row.get("src")
            if tag == "script"
            else row.get("href") if tag == "link" and row.get("rel") == "stylesheet" else None
        )
        if value:
            self.urls.append(value)


class _ProbeClient:
    provider_name = "self-check"
    model = "no-network"

    def complete(
        self, messages: Sequence[ChatMessage], *, options: GenerationOptions | None = None
    ) -> ChatResponse:
        del messages, options
        return ChatResponse(content="本机自检通过", model=self.model)

    def stream(
        self, messages: Sequence[ChatMessage], *, options: GenerationOptions | None = None
    ) -> Iterator[StreamChunk]:
        del messages, options
        yield StreamChunk(text="本机自检通过", model=self.model)


def runtime_report(package_root: Path | None = None, models: Path | None = None) -> dict[str, Any]:
    """Check the files/modules the executable actually carries, without starting its UI."""
    package = package_root or Path(__file__).resolve().parents[1]
    web = package / "ui" / "web"
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    try:
        AppConfig.from_mapping(load_defaults())
        add("default_config", True)
    except Exception as exc:
        add("default_config", False, str(exc))
    try:
        index = web / "index.html"
        parser = _Assets()
        parser.feed(index.read_text(encoding="utf-8"))
        missing: list[str] = []
        for url in parser.urls:
            parsed = urlsplit(url)
            target = (web / unquote(parsed.path).lstrip("/")).resolve()
            if (
                parsed.scheme
                or parsed.netloc
                or not target.is_relative_to(web.resolve())
                or not target.is_file()
            ):
                missing.append(url)
        add("desktop_bundle", bool(parser.urls) and not missing, "; ".join(missing))
    except (OSError, ValueError) as exc:
        add("desktop_bundle", False, str(exc))
    for module in ("webview", "psutil", "onnxruntime", "yaml", "numpy"):
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        add(f"module:{module}", found)
    weights = package / "vision" / "data" / "u2netp.onnx"
    add("pet_segmentation_weights", weights.is_file() and weights.stat().st_size > 1024)
    store = SqliteStore(Path(":memory:"))
    try:
        store.start()
        with store.connection() as connection:
            result = connection.execute("SELECT 1").fetchone()
        add("sqlite_in_memory", result is not None and result[0] == 1)
    except Exception as exc:
        add("sqlite_in_memory", False, str(exc))
    finally:
        store.stop()
    chat = ChatService(lambda: _ProbeClient())
    try:
        chat.start()
        reply = chat.ask("检查本机对话链路", remembered=False)
        add("chat_no_network", not reply.error and reply.answer == "本机自检通过", reply.error)
    except Exception as exc:
        add("chat_no_network", False, str(exc))
    finally:
        chat.stop()
    model_dir = models or (
        (Path(sys.executable).parent if getattr(sys, "frozen", False) else package.parent) / "模型"
    )
    model_rows: list[dict[str, object]] = []
    for path in sorted(model_dir.glob("*.gguf")):
        try:
            with path.open("rb") as handle:
                valid = handle.read(4) == b"GGUF"
            size = path.stat().st_size
            add(f"model:{path.name}", valid and size > 1024 * 1024)
            model_rows.append({"name": path.name, "bytes": size})
        except OSError as exc:
            add(f"model:{path.name}", False, str(exc))
    build: dict[str, Any] = {}
    try:
        loaded = json.loads((package / "buildinfo.json").read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            build = loaded
    except (OSError, ValueError):
        pass
    return {
        "ok": all(row["ok"] for row in checks),
        "version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "build": build,
        "checks": checks,
        "models": model_rows,
        "network_used": False,
        "microphone_opened": False,
        "user_data_opened": False,
        "not_verified": [
            "GUI interaction",
            "microphone/speaker",
            "real LLM inference",
            "Android device",
        ],
    }


def run_self_check(output: Path | None = None) -> int:
    report = runtime_report()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    if sys.stdout is not None:
        print(text)
    return 0 if report["ok"] else 1
