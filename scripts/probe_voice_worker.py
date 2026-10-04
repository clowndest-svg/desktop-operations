"""Drive the real CosyVoice worker over its own protocol and show what happened.

Why this exists: the sidecar's load path can fail in ways that are
indistinguishable from outside -- a crash, a deadlock, and a very slow load all
look like "the process is alive and the pipe is quiet". This runs the *real*
worker (the materialised copy, in its own cwd -- see `materialised_worker`, and
do **not** run `jarvis/tts/cosyvoice_worker.py` from the source tree, where
`jarvis/tts/types.py` shadows the stdlib `types` module) and prints, in order:
the load reply, the process exit code with its meaning, and the tail of stderr.

Two details that were learned the hard way and are load-bearing here:

* The worker is launched with three pipes and no console. A heredoc that puts it
  on an interactive tty makes CPython's new REPL try to size a console it does
  not have and pour out `OSError: [WinError 123]` instead of running.
* Windows reports a native crash as a huge exit code. `_explain` names them,
  which is the only way to tell "crashed" from "still loading".

Usage::

    JARVIS_HOME=... python scripts/probe_voice_worker.py           # CPU only
    JARVIS_HOME=... python scripts/probe_voice_worker.py --gpu     # use the GPU
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TRANSCRIPT = "希望你以后能够做的比我还好呦。"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--gpu",
        action="store_true",
        help="use the GPU instead of forcing CPU (the default isolates the link)",
    )
    parser.add_argument(
        "--model-dir",
        default="",
        help="weights to load; defaults to the CosyVoice2 snapshot under JARVIS_HOME",
    )
    args = parser.parse_args(argv)

    from jarvis.config.paths import AppPaths
    from jarvis.tts.sidecar import _child_env, materialised_worker, sidecar_python

    home = AppPaths.resolve().data_dir
    model = Path(args.model_dir) if args.model_dir else _default_model(home)
    python = sidecar_python()
    worker = materialised_worker(home)
    print("home   :", home)
    print("worker :", worker)
    print("python :", python)
    print("model  :", model, "exists:", model.is_dir())
    if python is None or worker is None:
        print("the voice virtualenv or worker script is missing; nothing to run")
        return 1

    env = _child_env(home)
    if not args.gpu:
        env["CUDA_VISIBLE_DEVICES"] = ""  # CPU only, to isolate "does the link work"

    clip = _default_clip(home)
    print("clip   :", clip, "exists:", clip.is_file())

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    started = time.time()
    process = subprocess.Popen(
        [str(python), "-u", str(worker)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(worker.parent),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
    )

    def send(payload: dict[str, object]) -> bool:
        """Write one request. False when the worker has already gone."""
        if process.poll() is not None:
            print(f"  worker already exited with {process.returncode}")
            return False
        if process.stdin is None:
            return False
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            print(f"  write failed: {exc}")
            return False
        return True

    send({"id": 1, "op": "load", "model_dir": str(model), "fp16": False})

    reply = None
    assert process.stdout is not None
    for line in process.stdout:
        print(f"  +{time.time() - started:6.1f}s  {line.rstrip()[:160]}")
        if "XY-PROTOCOL" in line and '"id": 1' in line:
            reply = line
            break
    print("load reply:", reply)
    print("poll after load:", process.poll())

    if reply is not None and '"ok": true' in reply and clip.is_file():
        send(
            {
                "id": 2,
                "op": "synthesize",
                "text": "你好，我是小夜。",
                "voice": "",
                "speed": 1.0,
                "stream": True,
                "reference": {"path": str(clip), "prompt_text": TRANSCRIPT},
            }
        )
        chunks = 0
        pcm = bytearray()
        for line in process.stdout:
            print(f"  +{time.time() - started:6.1f}s  {line.rstrip()[:120]}")
            if "XY-PROTOCOL" not in line:
                continue
            message = json.loads(line.split("XY-PROTOCOL", 1)[1])
            if message.get("notify") == "chunk":
                chunks += 1
                pcm.extend(base64.b64decode(message["pcm"]))
            if message.get("id") == 2 and "ok" in message:
                break
        print(f"synthesize: {chunks} chunk(s), {len(pcm) // 2} samples")

    send({"id": 3, "op": "shutdown"})
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
    code = process.returncode
    print("exit code:", code, _explain(code))

    from_worker = _drain(process.stderr)
    tail = [line for line in from_worker.splitlines() if line.strip()][-25:]
    print("\n=== worker stderr (last 25 lines) ===")
    for line in tail:
        print(line[:220])
    return 0


def _default_model(home: Path) -> Path:
    """The CosyVoice2 snapshot under the model cache, if it is there."""
    cache = home / "models" / "modelscope" / "models"
    return cache / "iic--CosyVoice2-0.5B" / "snapshots" / "master"


def _default_clip(home: Path) -> Path:
    """CosyVoice ships a reference clip; it is what its own examples clone."""
    return home / "voice-runtime" / "CosyVoice" / "asset" / "zero_shot_prompt.wav"


def _explain(code: int | None) -> str:
    """Name a Windows exit code. A native crash is otherwise an opaque number.

    ``3221225477`` is the one that matters in practice: an access violation, the
    way a bad native call dies. It produces no Python traceback, so without this
    it reads as "the worker vanished for no reason".
    """
    if code is None:
        return ""
    known = {
        -1073741819: "(0xC0000005 access violation / segfault)",
        -1073740791: "(0xC0000409 stack buffer overrun / fail-fast)",
        -1073741510: "(0xC000013A Ctrl-C)",
        -1073741571: "(0xC00000FD stack overflow)",
    }
    if code in known:
        return known[code]
    if code < 0:
        return f"(unsigned: {code & 0xFFFFFFFF:#010x})"
    return ""


def _drain(stream: object) -> str:
    """Read whatever is left on a pipe, tolerating a closed handle."""
    if stream is None:
        return ""
    try:
        return stream.read()  # type: ignore[attr-defined]
    except (OSError, ValueError) as exc:
        return f"<stderr unreadable: {exc}>"


if __name__ == "__main__":
    raise SystemExit(main())
