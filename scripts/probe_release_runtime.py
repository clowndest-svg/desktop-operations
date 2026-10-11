"""Run the published llama.cpp binary with a synthetic, loopback-only smoke question."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from release_manifest import write_json


def probe(app: Path, report: Path) -> dict[str, Any]:
    runtime = app / "_internal/jarvis/vendor/llama/llama-server.exe"
    weights = sorted((app / "模型").glob("*.gguf"), key=lambda path: path.stat().st_size)
    if not runtime.is_file() or not weights:
        raise ValueError("交付目录缺少本地模型运行时或权重")
    with socket.socket() as address:
        address.bind(("127.0.0.1", 0))
        port = address.getsockname()[1]
    report.parent.mkdir(parents=True, exist_ok=True)
    log = report.with_suffix(".log")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    process: subprocess.Popen[bytes] | None = None
    started = time.monotonic()
    with log.open("wb") as output:
        try:
            process = subprocess.Popen(
                [
                    str(runtime),
                    "-m",
                    str(weights[0]),
                    "-c",
                    "2048",
                    "-np",
                    "1",
                    "-t",
                    "2",
                    "-ngl",
                    "0",
                    "--jinja",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--alias",
                    "release-check",
                ],
                cwd=runtime.parent,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            endpoint = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 120
            while True:
                if process.poll() is not None:
                    raise ValueError(f"本地运行时提前退出，详情 {log}")
                try:
                    with opener.open(endpoint + "/health", timeout=2) as answer:
                        if answer.status == 200:
                            break
                except (OSError, urllib.error.URLError):
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("本地模型 120 秒内未就绪")
                time.sleep(0.5)
            ready = time.monotonic()
            payload = {
                "model": "release-check",
                "messages": [{"role": "user", "content": "Please reply only with OK."}],
                "max_tokens": 32,
                "temperature": 0,
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            request = urllib.request.Request(
                endpoint + "/v1/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(request, timeout=90) as answer:
                response = json.load(answer)
            text = str(
                response.get("choices", [{}])[0].get("message", {}).get("content") or ""
            ).strip()
            result = {
                "ok": bool(text),
                "runtime": str(runtime),
                "weights": weights[0].name,
                "load_seconds": round(ready - started, 2),
                "reply_seconds": round(time.monotonic() - ready, 2),
                "answer": text,
                "usage": response.get("usage"),
                "api_key_used": False,
                "external_network_used": False,
                "user_data_used": False,
                "microphone_opened": False,
                "scope": "published llama.cpp CPU inference only; not GUI/voice/GPU validation",
            }
            write_json(report, result)
            return result
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = probe(args.app_dir.resolve(), args.report.resolve())
    except Exception as error:
        result = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        write_json(args.report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
