"""The CosyVoice2 worker: a process that owns the model and answers one line at a time.

This file is deliberately **standalone**. It is executed by a *different*
interpreter from the one running the assistant -- the voice virtualenv -- which
has CosyVoice and a ``transformers`` old enough for it, and which does *not*
have this project installed. So it must not import anything from ``jarvis``,
and everything it needs must be in the standard library plus torch/CosyVoice.

Why a second process at all
---------------------------
CosyVoice pins ``transformers<4.52``; the assistant's own environment holds
``transformers 5.x`` for its LLM clients. Those two cannot coexist in one
interpreter, and no amount of lazy importing fixes a version *ceiling*. A
subprocess is not a workaround here -- it is the only shape that works, and it
has the side benefit that a 4 GB model crash takes down a disposable process
instead of the assistant.

Protocol
--------
One JSON object per line in each direction, on stdin/stdout. Nothing else may
be written to stdout: a stray ``print`` from torch or CosyVoice would corrupt
the stream, which is why ``_guard_stdout`` redirects the real stdout to stderr
for the duration and keeps the protocol on file descriptor 3 semantics
implemented in user space (see ``_send``).

Requests::

    {"id": 1, "op": "load",       "model_dir": "...", "fp16": true}
    {"id": 2, "op": "synthesize", "text": "...", "voice": "...", "speed": 1.0,
     "stream": true, "reference": {"path": "...", "prompt_text": "..."}}
    {"id": 3, "op": "speakers"}
    {"id": 4, "op": "shutdown"}
    {"op": "cancel"}                       <- no id; aborts the running request

Responses carry the same ``id``::

    {"id": 2, "ok": true, "chunks": 3, "sample_rate": 24000}
    {"id": 2, "ok": false, "error": "ValueError: ..."}

While a ``synthesize`` runs, each generated chunk is announced *before* the
response, so the caller can start playing immediately instead of waiting for
the whole utterance::

    {"notify": "chunk", "id": 2, "pcm": "<base64 s16le>", "final": false}

The reference clip is registered as a named speaker the first time it is used
(``add_zero_shot_spk``). That matters more than it looks: building the prompt
front end runs a flow encoder and two ONNX networks over the clip, which on the
measured machine costs ~15 of the ~16 seconds before the first chunk. Registering
it once turns "every sentence pays for the reference audio" into "only the first
one does".
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import IO, Any

SAMPLE_RATE = 24_000
"""CosyVoice2 emits 24 kHz s16le PCM. Reported to the caller, never assumed."""

OUTPUT_MARKER = "XY-PROTOCOL"
"""Every protocol line starts with this, so the caller can ignore noise.

The worker already redirects its own stdout, but a native library writing to
file descriptor 1 from C does not go through Python's ``sys.stdout`` and cannot
be redirected. A marker makes the reader immune to that class of garbage
instead of hoping it never happens.
"""

MAX_REFERENCE_MS = 60_000
"""Above this a reference clip is not a voice sample, it is a recording session."""

_PEAK = 32767.0


def _log(message: str) -> None:
    """Diagnostics go to stderr; stdout belongs to the protocol."""
    sys.stderr.write(f"[worker] {message}\n")
    sys.stderr.flush()


def _start_trace_dump() -> None:
    """Dump every thread's stack periodically when ``XY_WORKER_TRACE`` is set.

    A worker that stops answering is the hardest failure to read from outside:
    the process is alive, the pipes are quiet, and every explanation ("it is
    loading", "it is deadlocked", "it never got the request") looks identical
    from the parent. ``faulthandler`` is stdlib and prints the *Python* frames,
    which is enough to tell those three apart.

    Off unless asked for, because the dump lands in the middle of the worker's
    stderr -- where the diagnostics the parent quotes in its error messages are
    being collected -- and because a 30-second load should not produce noise in
    the normal case that makes the abnormal one easy to skim.
    """
    if not os.environ.get("XY_WORKER_TRACE"):
        return
    import faulthandler

    every = float(os.environ.get("XY_WORKER_TRACE", "5") or "5")
    faulthandler.dump_traceback_later(every, repeat=True, file=sys.stderr)
    _log(f"trace dump armed (every {every:g}s)")


def _preload() -> None:
    """Import the heavy native stack **before** any thread blocks on stdin.

    This is a workaround for a deadlock that was measured on the target machine,
    not a guess. The receiver below reads stdin from its own thread, and if
    ``torch`` has not been imported yet when that thread parks in ``readline``,
    the import never finishes:

    ====================================  ==================
    thread blocked on stdin, then import  ``import torch``
    ====================================  ==================
    no thread                             9.5 s, completes
    ``sys.stdin`` iteration               hangs indefinitely
    ``sys.stdin.readline()``              hangs indefinitely
    ``os.read(0, ...)``                   hangs indefinitely
    a thread doing ``time.sleep(300)``    9.7 s, completes
    ====================================  ==================

    The old code never hit it because the model was loaded in-process; this
    worker loads it *on request*, which is exactly after the receiver is already
    parked. The process stayed healthy, held no VRAM, wrote nothing to stderr
    and answered nothing -- indistinguishable from a very slow load.

    Buying the import up front costs the same seconds it would have cost on the
    first request, and it removes the window in which the two can collide. The
    remaining lazy imports (the text frontend, ONNX providers) are loaded by
    :meth:`_Engine.load` and were measured to complete even with the receiver
    blocked, because their native trees are already in by then.

    ``--no-preload`` exists so the deadlock can be re-demonstrated on a machine
    where somebody doubts it.
    """
    started = time.time()
    try:
        import torch  # noqa: F401 - imported for its side effect
        from cosyvoice.cli.cosyvoice import CosyVoice2  # noqa: F401
    except Exception as exc:  # pylint: disable=broad-except
        # Not fatal, and deliberately so: reporting it here would turn "the
        # optional stack is missing" into a worker that dies before it can
        # explain itself. The load request will report the same problem with a
        # proper error, and the parent shows that.
        _log(f"preload failed ({type(exc).__name__}: {exc}); will retry on load")
        return
    _log(f"preloaded the voice stack in {time.time() - started:.1f}s")


class _Protocol:
    """The one place that writes to stdout, always one complete line at a time."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> None:
        line = OUTPUT_MARKER + json.dumps(payload, ensure_ascii=False)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


def _pcm_from_tensor(tensor: object) -> bytes:
    """Float tensor in [-1, 1] -> s16le bytes.

    ``clip`` before scaling, because a model that overshoots by a fraction would
    otherwise wrap around to the opposite sign -- which is not a quiet artefact,
    it is a loud click in the middle of a word.
    """
    import numpy as np

    squeezed = tensor.squeeze(0) if hasattr(tensor, "squeeze") else tensor
    samples = squeezed.cpu().numpy() if hasattr(squeezed, "cpu") else squeezed
    flat = np.asarray(samples, dtype="<f4").reshape(-1)
    clipped = np.clip(flat, -1.0, 1.0)
    return bytes((clipped * _PEAK).astype("<i2").tobytes())


def _reference_path(payload: object) -> Path:
    """Pull ``reference.path`` out of an untyped payload, or say what is wrong."""
    if not isinstance(payload, dict):
        raise ValueError("synthesize needs a reference object for a cloned voice")
    raw = payload.get("path")
    if not isinstance(raw, str) or not raw:
        raise ValueError("the reference has no path")
    path = Path(raw)
    if not path.is_file():
        raise FileNotFoundError(f"the reference clip is gone: {path}")
    return path


def _reference_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    text = payload.get("prompt_text")
    return text.strip() if isinstance(text, str) else ""


def _clip_duration_ms(path: Path) -> int:
    """Duration of a WAV, read without decoding it. ``-1`` when it cannot be read.

    Refusing a clip that is too long is worth doing *here* rather than trusting
    the caller: the cost of the mistake is a prompt front end that takes thirty
    seconds per sentence, and the person who recorded it has no way to see that.

    The parsing is done by hand rather than with :mod:`wave`, which rejects any
    ``wFormatTag`` that is not PCM -- including the **IEEE float** (format 3)
    files that ship beside CosyVoice and that ``torchaudio`` reads happily. That
    mismatch made a perfectly loadable reference clip fail the *length check*
    with ``unknown format: 3``, which reads as "your recording is broken" when
    nothing was wrong with it.

    ``-1`` means "could not tell", and the caller treats it as "do not refuse".
    A guard that cannot measure something must not become a refusal: the model
    itself is the authority on whether it can read the clip, and it will say so
    with its own message.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(4096)
    except OSError:
        return -1
    if len(header) < 12 or header[:4] not in (b"RIFF", b"RF64") or header[8:12] != b"WAVE":
        return -1
    # Walk the chunk list for ``fmt `` and ``data``; neither is guaranteed to be
    # first, and a ``LIST``/``fact`` chunk before them is normal.
    offset = 12
    rate = 0
    byte_rate = 0
    data_bytes = -1
    while offset + 8 <= len(header):
        chunk_id = header[offset : offset + 4]
        size = int.from_bytes(header[offset + 4 : offset + 8], "little")
        body = offset + 8
        if chunk_id == b"fmt " and size >= 16 and body + 16 <= len(header):
            rate = int.from_bytes(header[body + 4 : body + 8], "little")
            byte_rate = int.from_bytes(header[body + 8 : body + 12], "little")
        elif chunk_id == b"data":
            data_bytes = size
            break
        if size < 0:
            return -1
        offset = body + size + (size & 1)
    if rate <= 0 or data_bytes < 0:
        return -1
    if byte_rate <= 0:
        # Malformed, or a codec that never fills it in; the caller only wants a
        # magnitude, so fall back to counting the frames per second as 2 bytes
        # per sample -- close enough to answer "is this longer than a minute".
        byte_rate = rate * 2
    return int(data_bytes / byte_rate * 1000)


class _Engine:
    """The model, plus the bookkeeping that keeps a cloned voice cheap to reuse."""

    def __init__(self, fp16: bool | None) -> None:
        self._fp16 = fp16
        self._model: Any = None
        self._lock = threading.Lock()
        self._registered: dict[str, str] = {}
        """reference path -> the speaker id its prompt was stored under."""

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def set_fp16(self, fp16: bool | None) -> None:
        """Override the precision the next :meth:`load` will use.

        Arrives with the load request rather than at construction because the
        worker is pooled: one process outlives many engine objects, and the
        first caller to actually need the model decides how to load it.
        """
        self._fp16 = fp16

    def load(self, model_dir: str) -> dict[str, Any]:
        """Bring the weights in. Slow (tens of seconds) and done once.

        ``fp16`` follows the device rather than a config flag: half precision is
        a large win on a GPU and a *loss* on a CPU, where it is emulated. The
        caller may force it, but the default is the one that is not a mistake.
        """
        with self._lock:
            if self._model is not None:
                return self._describe()
            import torch
            from cosyvoice.cli.cosyvoice import CosyVoice2

            on_cuda = torch.cuda.is_available()
            use_fp16 = on_cuda if self._fp16 is None else self._fp16
            if use_fp16 and not on_cuda:
                _log("fp16 was asked for with no CUDA device; falling back to fp32")
                use_fp16 = False
            started = time.time()
            self._model = CosyVoice2(
                model_dir,
                load_jit=False,
                load_trt=False,
                load_vllm=False,
                fp16=use_fp16,
            )
            _log(f"model loaded in {time.time() - started:.1f}s (fp16={use_fp16})")
            described = self._describe()
            described["load_seconds"] = round(time.time() - started, 2)
            described["device"] = "cuda" if on_cuda else "cpu"
            return described

    def _describe(self) -> dict[str, Any]:
        if self._model is None:
            return {"loaded": False}
        built_in = list(self._model.list_available_spks())
        return {
            "loaded": True,
            "sample_rate": self._model.sample_rate,
            "builtin_speakers": len(built_in),
            "registered": len(self._registered),
        }

    def speakers(self) -> dict[str, Any]:
        described = self._describe()
        described["registered_paths"] = sorted(self._registered)
        return described

    def _speaker_id(self, path: Path, prompt_text: str) -> str:
        """Register ``path`` as a named speaker once, and return the id.

        The id is a digest of the clip path and *not* of the audio. Two reasons:
        hashing the bytes would mean reading the file on every synthesis only to
        discover the same answer, and the app's cloned voices already have an
        immutable id -- ``clone:<hex>`` -- so the path is the stable key.
        """
        key = str(path)
        cached = self._registered.get(key)
        if cached is not None:
            return cached
        if not prompt_text:
            raise ValueError("a cloned voice needs the transcript of its reference clip")
        duration = _clip_duration_ms(path)
        # ``-1`` is "could not measure", and that must not become a refusal: see
        # `_clip_duration_ms`. The model is the authority on what it can read,
        # and its own error is more useful than this guard's guess.
        if duration > MAX_REFERENCE_MS:
            raise ValueError(
                f"the reference clip looks like {duration // 1000}s; "
                f"{MAX_REFERENCE_MS // 1000}s is the ceiling"
            )
        spk_id = "r" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        started = time.time()
        self._model.add_zero_shot_spk(prompt_text, str(path), spk_id)
        self._registered[key] = spk_id
        _log(f"registered {path.name} as {spk_id} in {time.time() - started:.1f}s")
        return spk_id

    def synthesize(
        self,
        text: str,
        voice: str,
        speed: float,
        stream: bool,
        reference: object,
        cancel: threading.Event,
    ) -> Any:
        """Yield ``(pcm, is_final)`` for one utterance.

        A generator so the caller can forward each chunk as it is produced. The
        model's own generator yields one item *per normalised sentence*, so a
        long line arrives in several pieces and "is this the last" needs a single
        item of lookahead rather than ``len()`` -- which would raise, because it
        is a generator.
        """
        if self._model is None:
            raise RuntimeError("the model is not loaded; send a load request first")
        if not text.strip():
            return
        rate = 1.0 if speed <= 0 else float(speed)

        if reference is None:
            generated = self._model.inference_sft(text, voice, stream=stream, speed=rate)
        else:
            path = _reference_path(reference)
            spk_id = self._speaker_id(path, _reference_text(reference))
            # ``prompt_text`` and ``prompt_wav`` are ignored once the speaker is
            # registered, but they are still positionally required.
            generated = self._model.inference_zero_shot(
                text, "", str(path), zero_shot_spk_id=spk_id, stream=stream, speed=rate
            )

        pending: Any = None
        for item in generated:
            if cancel.is_set():
                _log("cancelled between chunks")
                return
            if pending is not None:
                yield _pcm_from_tensor(pending["tts_speech"]), False
            pending = item
        if pending is not None:
            yield _pcm_from_tensor(pending["tts_speech"]), True


def _guard_stdout() -> IO[str]:
    """Move the real stdout to stderr and hand back a private protocol stream.

    Imports that happen lazily (torch, onnxruntime, CosyVoice) print banners on
    first use. Those arrive *after* the process claims stdout is a protocol
    channel, so redirecting at startup would not have been enough -- the swap
    happens before anything is imported, and the protocol stream is a duplicate
    of the original descriptor taken first.
    """
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    return protocol


def _handle(
    request: dict[str, Any], engine: _Engine, protocol: _Protocol, cancel: threading.Event
) -> bool:
    """Run one request. Returns False when the worker should stop."""
    request_id = request.get("id")
    op = request.get("op")
    if op == "shutdown":
        return False
    if op == "load":
        model_dir = request.get("model_dir")
        if not isinstance(model_dir, str) or not model_dir:
            protocol.send({"id": request_id, "ok": False, "error": "load needs a model_dir"})
            return True
        fp16 = request.get("fp16")
        if isinstance(fp16, bool):
            engine.set_fp16(fp16)
        described = engine.load(model_dir)
        protocol.send({"id": request_id, "ok": True, **described})
        return True
    if op == "speakers":
        protocol.send({"id": request_id, "ok": True, **engine.speakers()})
        return True
    if op == "synthesize":
        text = request.get("text")
        voice = request.get("voice")
        speed = request.get("speed")
        chunks = 0
        for pcm, is_final in engine.synthesize(
            text if isinstance(text, str) else "",
            voice if isinstance(voice, str) else "",
            1.0 if not isinstance(speed, (int, float)) else float(speed),
            bool(request.get("stream", True)),
            request.get("reference"),
            cancel,
        ):
            chunks += 1
            # ``final`` is carried on the chunk so the caller need not count:
            # the worker knows which one the model finished on, and the caller
            # cannot, because it never sees the model's own generator.
            protocol.send(
                {
                    "notify": "chunk",
                    "id": request_id,
                    "pcm": base64.b64encode(pcm).decode("ascii"),
                    "final": is_final,
                }
            )
        protocol.send({"id": request_id, "ok": True, "chunks": chunks, "sample_rate": SAMPLE_RATE})
        return True
    protocol.send({"id": request_id, "ok": False, "error": f"unknown op: {op!r}"})
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CosyVoice2 worker speaking line-JSON on stdin/stdout"
    )
    parser.add_argument(
        "--warm", action="store_true", help="load the model before the first request"
    )
    parser.add_argument("--model-dir", default="", help="weights to load when --warm is given")
    parser.add_argument("--fp16", action="store_true", help="force half precision")
    parser.add_argument(
        "--no-preload",
        action="store_true",
        help="skip the up-front import of the voice stack (reproduces the stdin deadlock)",
    )
    args = parser.parse_args(argv)

    protocol = _Protocol(_guard_stdout())
    engine = _Engine(fp16=True if args.fp16 else None)
    cancel = threading.Event()
    _start_trace_dump()
    # Before the receiver thread exists, because the two cannot coexist: see
    # `_preload`.
    if not args.no_preload and not (args.warm and args.model_dir):
        _preload()

    if args.warm and args.model_dir:
        engine.load(args.model_dir)

    work: list[dict[str, Any]] = []
    done = threading.Event()
    stdin_closed = threading.Event()

    def reader() -> None:
        """Own stdin so that ``cancel`` is seen *while* a synthesis is running.

        Reading stdin from the main loop would mean the only way to interrupt a
        thirty-second utterance is to wait for it to end -- which is exactly the
        case barge-in exists for.
        """
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except ValueError:
                    _log(f"ignoring unparseable line: {line[:120]!r}")
                    continue
                if not isinstance(request, dict):
                    continue
                if request.get("op") == "cancel":
                    cancel.set()
                    continue
                work.append(request)
        finally:
            # End of input means the parent is gone. Without this the loop below
            # would spin forever holding several gigabytes of VRAM, because
            # nothing else ever tells it to stop -- a plain `shutdown` message is
            # the polite path, and a parent that crashed never sends one.
            stdin_closed.set()

    threading.Thread(target=reader, name="worker-stdin", daemon=True).start()

    while not done.is_set():
        if not work:
            if stdin_closed.is_set():
                _log("stdin closed; exiting")
                break
            time.sleep(0.01)
            continue
        request = work.pop(0)
        cancel.clear()
        try:
            if not _handle(request, engine, protocol, cancel):
                done.set()
        except Exception as exc:  # pylint: disable=broad-except
            # A failed request is answered, not fatal: the caller keeps its
            # process and learns what went wrong, and the next sentence does not
            # pay for a restart of a 4 GB model.
            _log(traceback.format_exc())
            protocol.send(
                {
                    "id": request.get("id"),
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        cancel.clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
