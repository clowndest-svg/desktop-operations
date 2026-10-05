"""A speech engine that lives in another process.

CosyVoice cannot be imported here. The assistant's environment carries
``transformers 5.x`` because its LLM clients need it, and CosyVoice pins
``transformers<4.52`` -- a version *ceiling*, which no lazy import can dodge. So
cloned voices are synthesised by a second interpreter (the voice virtualenv,
which has its own CosyVoice and torch) driven over line-delimited JSON. See
:mod:`jarvis.tts.cosyvoice_worker` for the other end of that pipe.

The class below is the whole reason this is written as a drop-in:
:class:`CosyVoiceSidecar` satisfies :class:`~jarvis.tts.types.SpeechSynthesizer`,
so ``voice_call``, ``VoicePicker`` and the pipeline keep calling ``synthesize``
and never learn that the model is somewhere else.

Three things here are not obvious and were each measured:

* **The model stays warm.** Loading the weights takes ~31 s and 2.4 GB of VRAM.
  A worker per utterance would make every sentence cost half a minute, so the
  process is *pooled* -- one per voice data root, shared by every engine object,
  reaped at interpreter exit. ``close()`` therefore does not kill it, and says so.
* **A cross-process lock guards the pair.** Two assistant instances (the desktop
  and a packaged copy, say) would otherwise each start a worker, and two copies
  of a 4 GB model on a 4 GB card is an out-of-memory crash, not a slowdown. The
  lock makes the second instance wait for the first instead.
* **Cancelling means telling the worker.** The caller's ``should_stop`` predicate
  is polled on a side thread and turned into a ``cancel`` message, because
  ignoring the next chunk would leave the GPU generating a paragraph nobody will
  hear. This is what makes barge-in work at all on this engine.
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Final, cast

from jarvis.config.paths import AppPaths
from jarvis.core.exceptions import TtsError
from jarvis.tts.types import FORMAT_PCM_S16LE, AudioChunk

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Iterator

logger = logging.getLogger("jarvis.tts.sidecar")

PROTOCOL_MARKER: Final[str] = "XY-PROTOCOL"
"""Matches the worker. Lines without it are library chatter, not protocol."""

REQUEST_TIMEOUT: Final[float] = 900.0
"""Fifteen minutes. A long utterance on a weak GPU is genuinely slow, and a
timeout that fires mid-sentence is worse than waiting: it costs the whole
utterance *and* leaves the operator with no audio at all."""

CANCEL_GRACE: Final[float] = 20.0
"""How long the worker gets to acknowledge a cancel before it is killed.

Generating a chunk cannot be interrupted inside torch, so the worker notices the
cancel at the next chunk boundary; on the measured machine that is up to a
minute. This bound is for the case where it stops answering entirely."""

EXIT_GRACE: Final[float] = 5.0
"""How long a worker gets to exit after its pipes are closed.

Short on purpose and separate from :data:`CANCEL_GRACE`: a graceful stop is
asked for by *sending* ``shutdown`` first, so by the time this bound is reached
the process has already been told what to do and is only being slow about it.
Waiting twenty seconds here instead would make a crash recovery -- and every
test -- take twenty seconds for nothing."""

VENV_DIRNAME: Final[str] = "voice-venv"
"""The virtualenv holding torch + CosyVoice, inside the voice data root."""

WORKER_MODULE: Final[str] = "jarvis.tts.cosyvoice_worker"

_CANCEL_POLL: Final[float] = 0.05
_DRAIN_INTERVAL: Final[float] = 0.005
_MODEL_SUBDIR: Final[str] = "modelscope"

_CACHE_ENV: Final[tuple[tuple[str, str], ...]] = (
    ("MODELSCOPE_CACHE", "modelscope"),
    ("HF_HOME", "huggingface"),
    ("TORCH_HOME", "torch"),
)
"""Where the worker looks for weights. Mirrors :func:`jarvis.config.paths.
export_model_cache_env` but is applied to the child only: the parent must not
have its own caches moved by the act of asking a question about voices."""


def _voice_home(home: Path | None) -> Path:
    """The data root holding the voice virtualenv, the weights and the lock."""
    return AppPaths.resolve().data_dir if home is None else home


def worker_script() -> Path | None:
    """This repository's worker file, or ``None`` if it cannot be located.

    This is the *source*, not what gets executed -- see :func:`materialised_worker`
    for why those are two different paths. Resolved through the import system
    rather than by joining ``__file__``: a packaged build may keep the module in
    an archive, and the subprocess needs a real file it can read.
    """
    try:
        spec = importlib.util.find_spec(WORKER_MODULE)
    except (ImportError, ValueError):  # pragma: no cover - a broken install
        return None
    origin = getattr(spec, "origin", None)
    if isinstance(origin, str) and origin:
        path = Path(origin)
        if path.is_file():
            return path
    # A frozen build has no file to point at: the module lives inside the archive,
    # and ``find_spec().origin`` is empty for anything bundled in the PYZ. The spec
    # therefore ships a **copy of the source** next to the package -- without this
    # second look the packaged app answers 「找不到离线语音的工作进程脚本」 and the
    # whole recording-to-voice feature is unreachable for anyone who double-clicks
    # an icon rather than runs the source.
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        shipped = bundle / "jarvis" / "tts" / Path(WORKER_MODULE.split(".")[-1] + ".py")
        if shipped.is_file():
            return shipped
    return None


def materialised_worker(home: Path, source: Path | None = None) -> Path | None:
    """Copy the worker somewhere neutral and return that path.

    The worker must **not** be executed from inside the package, and this was a
    real crash rather than a precaution. Python puts a script's own directory on
    ``sys.path[0]``; ``jarvis/tts/`` contains ``types.py``, which then shadows
    the standard-library ``types`` module, so the worker's very first
    ``import argparse`` dies with::

        ImportError: cannot import name 'GenericAlias' from 'types'
        (consider renaming 'jarvis\\tts\\types.py')

    Nothing about that failure points at the launcher, and it only appears when
    the real worker runs -- a stand-in living in a temp directory cannot
    reproduce it. Copying the file to the voice data root fixes the cause and
    buys three more things: the *other* interpreter never needs this repository
    on its ``sys.path``, the worker source sits next to the data it serves, and a
    stale copy is detectable because the content is compared before writing.
    """
    origin = source or worker_script()
    if origin is None:
        return None
    target_dir = home / "cache" / "voice-worker"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / origin.name
    try:
        body = origin.read_bytes()
        if not target.is_file() or target.read_bytes() != body:
            target.write_bytes(body)
    except OSError:  # pragma: no cover - an unwritable cache is worth a warning
        logger.warning("could not refresh the worker copy at %s", target, exc_info=True)
        if not target.is_file():
            return None
    return target


def sidecar_python(home: Path | None = None) -> Path | None:
    """The voice virtualenv's interpreter, or ``None`` when it was never built."""
    root = _voice_home(home) / VENV_DIRNAME
    for candidate in (root / "Scripts" / "python.exe", root / "bin" / "python"):
        if candidate.is_file():
            return candidate
    return None


def _is_cosyvoice_source(candidate: Path) -> bool:
    """Whether a directory looks like the CosyVoice checkout itself."""
    return (candidate / "cosyvoice" / "cli" / "cosyvoice.py").is_file()


def bundled_runtime(home: Path) -> Path | None:
    """A CosyVoice checkout shipped **with this project**, if one was bundled.

    The per-machine install lives in the voice data root, but a deployment that
    wants the offline voice to work out of the box can carry the checkout under
    ``vendor/`` inside the package instead. Both are accepted; this one wins when
    present because it is the one that was tested with this exact source tree.
    """
    package_root = Path(__file__).resolve().parent.parent
    candidates = (
        package_root / "vendor" / "CosyVoice",
        package_root / "_vendor" / "CosyVoice",
    )
    for candidate in candidates:
        if _is_cosyvoice_source(candidate):
            return candidate
    return None


def _runtime_candidates(home: Path) -> tuple[Path, ...]:
    bundled = bundled_runtime(home)
    installed = home / "voice-runtime" / "CosyVoice"
    return (bundled, installed) if bundled is not None else (installed,)


def voice_runtime(home: Path) -> Path | None:
    """The CosyVoice checkout this machine will actually use, bundled or installed."""
    for candidate in _runtime_candidates(home):
        if _is_cosyvoice_source(candidate):
            return candidate
    return None


def cloning_ready(home: Path | None = None) -> tuple[bool, str]:
    """Whether a recorded voice can be spoken on this machine, and why not if not.

    Asked by the picker on every listing, so it only checks for files -- nothing
    is imported and no process is started. The answer is deliberately strict: a
    virtualenv without a CosyVoice checkout would start, then fail on the first
    sentence, and saying so *before* somebody records a voice is the difference
    between a limitation and a bug.
    """
    root = _voice_home(home)
    if sidecar_python(root) is None:
        return False, f"还没装离线语音环境（{root / VENV_DIRNAME}）"
    if worker_script() is None:
        return False, "找不到离线语音的工作进程脚本"
    runtime = voice_runtime(root)
    if runtime is None:
        return False, "离线语音运行时不完整（缺 CosyVoice）"
    if not (runtime / "third_party" / "Matcha-TTS").is_dir():
        return False, "离线语音运行时不完整（缺 CosyVoice/third_party/Matcha-TTS）"
    return True, ""


def _child_env(home: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for the worker: cache redirects, import paths, no buffering.

    ``PYTHONPATH`` is prepended rather than replaced because the CosyVoice
    checkout is not installed -- it is a source tree, exactly as its own README
    expects. A *bundled* checkout produces an empty entry, which is dropped: an
    empty path element means "the current directory", and this worker must never
    pick up whatever happens to be beside it.
    """
    env = dict(os.environ if base is None else base)
    runtime = voice_runtime(home)
    parts: list[str] = []
    if runtime is not None:
        parts = [str(runtime), str(runtime / "third_party" / "Matcha-TTS")]
    existing = env.get("PYTHONPATH", "")
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    # torch defaults to one thread per core: on a 12-core laptop one clone synthesis
    # showed up as 167% CPU and the rest of the machine starved. Four threads still
    # saturate the model's parallelism without eating the desktop.
    threads = str(min(4, os.cpu_count() or 4))
    env.setdefault("OMP_NUM_THREADS", threads)
    env.setdefault("MKL_NUM_THREADS", threads)
    models = home / "models"
    for name, subdir in _CACHE_ENV:
        env.setdefault(name, str(models / subdir))
    # Unbuffered and UTF-8: a buffered protocol stream looks exactly like a
    # worker that has hung, and this assistant's text is Chinese.
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # ``PYTORCH_CUDA_ALLOC_CONF`` is deliberately *not* set here, and this note
    # is the reason it is worth mentioning at all. The obvious cure for the
    # "``CUDA out of memory. Tried to allocate 18.00 MiB ... 1.83 GiB is free``"
    # failure is ``expandable_segments:True`` -- and on Windows PyTorch answers
    # it with ``expandable_segments not supported on this platform`` and carries
    # on as if nothing was said. So the setting would look like a fix, be
    # quietly ignored, and mislead whoever reads this next. Measured on the
    # target machine, the failure was capacity, not fragmentation: the model
    # needs ~3.1 GB and the desktop (browsers, WeChat, an Android emulator)
    # already holds ~1.2 GB of the 4 GB card.
    return env


def _flock(handle: BinaryIO, *, exclusive: bool) -> None:
    """POSIX half of the file lock, behind one typed function.

    ``fcntl`` does not exist on Windows, and the type checker analysing *this*
    platform therefore has no stubs for it -- nor for its constants. Reaching it
    through ``importlib`` and treating the module as untyped keeps the narrow
    exception confined to a function that is only ever called on POSIX, instead
    of scattering ``type: ignore`` over the locking logic itself.
    """
    module = cast("Any", importlib.import_module("fcntl"))
    flags = module.LOCK_EX if exclusive else module.LOCK_UN
    if exclusive:
        flags |= module.LOCK_NB
    module.flock(handle.fileno(), flags)


class _FileLock:
    """A cross-process lock held for the duration of one request.

    ``msvcrt`` on Windows and ``fcntl`` elsewhere, both non-blocking so that
    waiting is ours to time out. A lock file is used instead of a named mutex
    because the two instances that collide may be packaged differently, and a
    file next to the data they share is the one rendezvous point they are
    guaranteed to agree on.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: BinaryIO | None = None

    def acquire(self, timeout: float) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately not a `with`: the handle has to stay open, and therefore
        # locked, across the whole request.
        handle = open(self._path, "a+b")  # noqa: SIM115
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._lock(handle)
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    return False
                time.sleep(0.05)
                continue
            self._handle = handle
            return True

    @staticmethod
    def _lock(handle: BinaryIO) -> None:
        if os.name == "nt":  # pragma: no cover - platform branch
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - platform branch
            _flock(handle, exclusive=True)

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover - platform branch
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - platform branch
                _flock(handle, exclusive=False)
        except OSError:  # pragma: no cover - releasing a lock we may not hold
            logger.debug("releasing the voice sidecar lock failed", exc_info=True)
        finally:
            handle.close()


class _Worker:
    """The child process, the protocol, and the lock that serialises callers."""

    def __init__(
        self, *, python: Path, script: Path, env: dict[str, str], workdir: Path, lock: Path
    ) -> None:
        self._python = python
        self._script = script
        self._env = env
        self._workdir = workdir
        self._file_lock = _FileLock(lock)
        self._process: subprocess.Popen[str] | None = None
        self._last_use = time.monotonic()
        self._lines: deque[str] = deque()
        self._lines_ready = threading.Event()
        self._reader_done = threading.Event()
        self._diagnostics: deque[str] = deque(maxlen=40)
        self._lock = threading.RLock()
        self._next_id = 1

    # -- process -----------------------------------------------------------

    def idle_for(self, now: float) -> float:
        """Seconds since this worker last took a request."""
        return now - self._last_use

    @property
    def alive(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def _spawn(self) -> None:
        if self.alive:
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        logger.info("starting the voice worker: %s", self._script.name)
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._lines.clear()
        self._reader_done.clear()
        self._lines_ready.clear()
        self._process = subprocess.Popen(
            [str(self._python), "-u", str(self._script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # ``self._script`` is the materialised copy, and this cwd is the voice
            # data root. Both matter: the worker must not run from inside the
            # package, where ``jarvis/tts/types.py`` shadows the standard library
            # and kills it before it reads a single request. That failure is
            # recorded on `materialised_worker`.
            cwd=str(self._workdir),
            env=self._env,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        threading.Thread(target=self._read_stdout, name="sidecar-out", daemon=True).start()
        # stderr must be drained even though it is only kept for diagnosis: a
        # full pipe blocks the worker mid-write, which presents as a hang.
        threading.Thread(target=self._read_stderr, name="sidecar-err", daemon=True).start()

    def _read_stdout(self) -> None:
        stream = self._process.stdout if self._process is not None else None
        if stream is not None:
            for line in stream:
                self._lines.append(line.rstrip("\n"))
                self._lines_ready.set()
        self._reader_done.set()
        self._lines_ready.set()

    def _read_stderr(self) -> None:
        stream = self._process.stderr if self._process is not None else None
        if stream is None:
            return
        for line in stream:
            text = line.rstrip("\n")
            if text:
                self._diagnostics.append(text)

    def _take_line(self, timeout: float) -> str | None:
        """Next line from the worker, or ``None`` when it exited or timed out."""
        deadline = time.monotonic() + timeout
        while True:
            if self._lines:
                return self._lines.popleft()
            if self._reader_done.is_set():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._lines_ready.clear()
            self._lines_ready.wait(timeout=min(remaining, 0.1))

    def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise TtsError("离线语音进程没有起来")
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._stop()
            raise TtsError(f"离线语音进程已经不在了：{exc}") from exc

    def cancel(self) -> None:
        """Ask the worker to abandon the utterance it is generating.

        Best-effort on purpose: the process may already be finishing, and a
        failed cancel is not a reason to break the caller's interruption.
        """
        try:
            self._write({"op": "cancel"})
        except TtsError:
            logger.debug("cancel could not be delivered", exc_info=True)

    def _stop(self) -> None:
        """End the child, gracefully if it will, forcibly if it will not.

        Only **stdin** is closed from here. Closing stdout while the reader
        thread is still parked in it blocks on Windows -- measured at 57 seconds
        for a process that had already exited -- because the close waits for the
        pending read to finish and the read waits for the close. The two output
        pipes are dropped instead and reclaimed with the file objects, by which
        point their readers have seen EOF and stopped touching them.
        """
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin is not None:
            with contextlib.suppress(OSError):  # already closed by the child
                process.stdin.close()
        try:
            process.wait(timeout=EXIT_GRACE)
        except subprocess.TimeoutExpired:
            # ``stdin`` closing is the polite request; this is the other one. A
            # worker that ignores both is wedged inside a native call and will
            # not come back, and leaving it holding 4 GB of VRAM is far worse
            # than killing it.
            logger.warning("the voice worker did not exit; killing it")
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=EXIT_GRACE)
        self._lines.clear()
        self._reader_done.set()

    def shutdown(self) -> None:
        with self._lock:
            if self.alive:
                try:
                    self._write({"op": "shutdown"})
                except TtsError:
                    logger.debug("shutdown could not be delivered", exc_info=True)
            self._stop()

    # -- requests ----------------------------------------------------------

    def run(
        self,
        payload: dict[str, Any],
        timeout: float,
        on_chunk: Callable[[bytes, bool], None] | None,
    ) -> dict[str, Any]:
        """Send one request and pump the replies until its own comes back.

        Chunk notifications are forwarded as they arrive, so a streaming caller
        starts playing before the utterance is finished.
        """
        with self._lock:
            self._spawn()
            self._last_use = time.monotonic()
            if not self._file_lock.acquire(timeout=timeout):
                raise TtsError(
                    "另一个小夜正在用离线语音，等了很久还没轮到",
                    details={"lock": str(self._file_lock._path)},
                )
            try:
                request_id = self._next_id
                self._next_id += 1
                self._write({**payload, "id": request_id})
                return self._pump(request_id, timeout, on_chunk)
            except TtsError as exc:
                if bool(exc.details.get("timed_out")):
                    # The worker is still chewing on the abandoned request at full CPU.
                    # Killing it is the only honest cancel: the protocol's cancel is
                    # checked between chunks, and a request that hit the deadline is
                    # by definition not producing any.
                    logger.warning("voice worker exceeded %.0fs; restarting it", timeout)
                    self._stop()
                raise
            finally:
                self._file_lock.release()

    def _pump(
        self,
        request_id: int,
        timeout: float,
        on_chunk: Callable[[bytes, bool], None] | None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                remaining = 0.0
            line = self._take_line(remaining)
            if line is None:
                # ``_take_line`` answers None for "it exited" *and* for "the deadline
                # passed"; telling them apart is the difference between a useful message
                # and a lie -- a worker that is alive and silent is slow, not dead.
                if self.alive:
                    raise TtsError(
                        "离线语音这次太慢了，超过了等待上限",
                        details={"diagnostics": list(self._diagnostics)[-5:], "timed_out": True},
                    )
                raise TtsError(
                    "离线语音进程退出了",
                    details={"diagnostics": list(self._diagnostics)[-8:]},
                )
            if not line.startswith(PROTOCOL_MARKER):
                # Import banners and native-library chatter land here.
                if line.strip():
                    self._diagnostics.append(line)
                continue
            message = self._decode(line)
            if message is None:
                continue
            if message.get("notify") == "chunk":
                if on_chunk is not None and isinstance(message.get("pcm"), str):
                    try:
                        on_chunk(base64.b64decode(message["pcm"]), bool(message.get("final")))
                    except (ValueError, TypeError):
                        logger.warning("a streamed chunk was not valid base64")
                continue
            if message.get("id") != request_id:
                continue
            return message

    @staticmethod
    def _decode(line: str) -> dict[str, Any] | None:
        try:
            message = json.loads(line[len(PROTOCOL_MARKER) :])
        except ValueError:
            return None
        return message if isinstance(message, dict) else None


_pool_lock = threading.Lock()
_workers: dict[str, _Worker] = {}


WORKER_IDLE_SECONDS: Final[float] = 600.0
"""How long an idle voice worker stays alive before it is shut down.

Ten minutes: long enough that a session of 试听 calls reuses one loaded model, short
enough that walking away gives the gigabytes back. The worker is torch plus CosyVoice;
on a 16 GB machine it is the single largest resident block this app owns.
"""

_REAP_INTERVAL: Final[float] = 60.0
_reaper_started = False


def reap_idle_workers(now: float | None = None) -> int:
    """Shut down pooled workers nobody has asked anything of lately.

    Returns how many were reaped. Takes the clock as an argument so tests can age a
    worker without sleeping; the reaper thread passes nothing and gets real time.
    """
    moment = time.monotonic() if now is None else now
    with _pool_lock:
        stale = [
            key for key, worker in _workers.items() if worker.idle_for(moment) > WORKER_IDLE_SECONDS
        ]
        for key in stale:
            del _workers[key]
    for _key in stale:
        logger.info(
            "voice worker idle for >%.0fs; shutting it down to give the memory back",
            WORKER_IDLE_SECONDS,
        )
    return len(stale)


def _start_reaper() -> None:
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True

    def loop() -> None:
        while True:
            time.sleep(_REAP_INTERVAL)
            try:
                reap_idle_workers()
            except Exception:  # a reaper that dies must not take the pool with it
                logger.exception("the idle-worker reaper failed")

    thread = threading.Thread(target=loop, name="voice-worker-reaper", daemon=True)
    thread.start()


def _pooled_worker(home: Path, python: Path, script: Path) -> _Worker:
    """The worker for this data root, started on first use and kept afterwards."""
    key = str(home)
    with _pool_lock:
        worker = _workers.get(key)
        if worker is None or not worker.alive:
            if worker is not None:
                logger.warning("the voice worker for %s died; starting a new one", key)
            worker = _Worker(
                python=python,
                script=script,
                env=_child_env(home),
                workdir=home / "cache" / "voice-worker",
                lock=home / "cache" / "voice-sidecar.lock",
            )
            _workers[key] = worker
        _start_reaper()
        return worker


def shutdown_voice_sidecars() -> None:
    """Stop every pooled worker. Called at interpreter exit and by the tests."""
    with _pool_lock:
        workers = list(_workers.values())
        _workers.clear()
    for worker in workers:
        worker.shutdown()


atexit.register(shutdown_voice_sidecars)


class CosyVoiceSidecar:
    """Cloned voices, synthesised by the voice virtualenv in a separate process.

    Implements :class:`~jarvis.tts.types.SpeechSynthesizer`; callers cannot tell
    it apart from the in-process engines except by the ``name`` it reports.
    """

    def __init__(
        self,
        model_dir: str,
        *,
        voice: str | None = None,
        reference: Callable[[str], tuple[Path, str] | None] | None = None,
        fp16: bool | None = None,
        home: Path | None = None,
        timeout: float = REQUEST_TIMEOUT,
        python: Path | None = None,
        worker: Path | None = None,
    ) -> None:
        """Create the engine handle. No process is started and no model loaded.

        Args:
            model_dir: CosyVoice2 weights directory, as the worker should see it.
            voice: Built-in speaker id to use when the caller names none. Only
                useful for a CosyVoice install that ships a speaker table; the
                ``0.5B`` release does not, which is why cloning is the feature.
            reference: ``voice_id -> (wav_path, transcript)`` for a recorded
                voice, or ``None`` when that id is not a recording.
            fp16: Force half precision. ``None`` -- the default -- lets the
                worker follow the device.
            home: Voice data root. Injectable so tests never touch the real one.
            timeout: Seconds to wait for one request.
            python: Interpreter to run the worker with, overriding discovery.
            worker: Worker script, overriding discovery.
        """
        self._model_dir = model_dir
        self._voice = voice or ""
        self._reference = reference
        self._fp16 = fp16
        self._home = _voice_home(home)
        self._timeout = timeout
        self._python = python
        self._worker_path = worker
        self._worker: _Worker | None = None
        self._loaded = False

    @property
    def name(self) -> str:
        return "cosyvoice-sidecar"

    @property
    def sample_rate(self) -> int:
        return 24_000

    @property
    def loaded(self) -> bool:
        """Whether this handle has already asked the worker to load the model."""
        return self._loaded

    # -- wiring ------------------------------------------------------------

    def _resolve_worker(self) -> _Worker:
        if self._worker is not None:
            return self._worker
        python = self._python or sidecar_python(self._home)
        source = self._worker_path or worker_script()
        script = (
            materialised_worker(self._home, source)
            if source is not None and self._worker_path is None
            else source
        )
        if python is None or script is None:
            ready, reason = cloning_ready(self._home)
            raise TtsError(reason or "离线语音不可用", details={"ready": ready})
        self._worker = _pooled_worker(self._home, python, script)
        return self._worker

    def _ensure_loaded(self) -> _Worker:
        """Load the weights once per worker process, not once per engine object."""
        worker = self._resolve_worker()
        if self._loaded:
            return worker
        # ``fp16: None`` travels as JSON ``null`` and means "decide from the
        # device", which the worker does -- it has torch and can ask the GPU.
        # Whether it ends up on cuda or cpu comes back in the reply.
        reply = worker.run(
            {"op": "load", "model_dir": self._model_dir, "fp16": self._fp16},
            self._timeout,
            None,
        )
        if not reply.get("ok"):
            raise TtsError(
                f"离线语音模型没装上：{reply.get('error', '未知原因')}",
                details={"engine": self.name, "model": self._model_dir},
            )
        self._loaded = True
        logger.info(
            "voice worker ready (load %.1fs, device %s)",
            reply.get("load_seconds"),
            reply.get("device"),
        )
        return worker

    # -- synthesis ---------------------------------------------------------

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[AudioChunk]:
        """Yield chunks as the worker produces them.

        The request runs on a side thread so chunks can be yielded *while* the
        model is still working -- waiting for the reply would collect the whole
        utterance first and hand back the very pause streaming exists to remove.
        """
        del volume  # CosyVoice has no gain knob.
        chosen = voice or self._voice
        reference = self._lookup_reference(chosen)
        if self._reference is not None and chosen.startswith("clone:") and reference is None:
            raise TtsError(
                f"这个自定义音色的录音找不到了：{chosen}",
                details={"engine": self.name, "voice": chosen},
            )
        if reference is not None and not reference[1].strip():
            raise TtsError(
                "这个自定义音色没有留下参考文本，她分不清音色和内容",
                details={"engine": self.name, "voice": chosen},
            )
        if not text.strip():
            return

        worker = self._ensure_loaded()
        rate = 1.0 if speed is None or speed <= 0 else float(speed)
        payload: dict[str, Any] = {
            "op": "synthesize",
            "text": text,
            "voice": chosen,
            "speed": rate,
            "stream": True,
            "reference": (
                None
                if reference is None
                else {"path": str(reference[0]), "prompt_text": reference[1]}
            ),
        }

        produced: deque[AudioChunk] = deque()
        cancelled = threading.Event()
        response: list[tuple[dict[str, Any] | None, BaseException | None]] = []
        finished = threading.Event()

        def collect(pcm: bytes, is_final: bool) -> None:
            produced.append(
                AudioChunk(
                    audio=pcm,
                    sample_rate=self.sample_rate,
                    is_final=is_final,
                    format=FORMAT_PCM_S16LE,
                )
            )

        def request() -> None:
            try:
                reply = worker.run(payload, self._timeout, collect)
                response.append((reply, None))
            except BaseException as exc:
                response.append((None, exc))
            finally:
                finished.set()

        threading.Thread(target=request, name="sidecar-synthesize", daemon=True).start()
        try:
            while True:
                while produced:
                    yield produced.popleft()
                if finished.is_set() and not produced:
                    break
                if should_stop is not None and should_stop() and not cancelled.is_set():
                    # Stop the *model*, not just the playback: otherwise the GPU
                    # keeps generating a paragraph nobody is going to hear.
                    cancelled.set()
                    worker.cancel()
                time.sleep(_DRAIN_INTERVAL)
        finally:
            # Wait for the worker to finish before deciding whether to raise: the
            # caller interrupts this generator (Barge-In), and reporting a failure
            # that had not happened yet would turn an interruption into an error.
            # A cancelled request gets the short grace, because the worker is
            # already on its way out of the loop; a normal one may legitimately
            # need the full budget.
            finished.wait(timeout=CANCEL_GRACE if cancelled.is_set() else REQUEST_TIMEOUT)
            if response:
                reply, exc = response[0]
                if exc is not None:
                    raise exc
                if reply is not None and not reply.get("ok") and not cancelled.is_set():
                    raise TtsError(
                        f"离线语音合成失败：{reply.get('error', '未知原因')}",
                        details={"engine": self.name},
                    )

    def _lookup_reference(self, voice_id: str) -> tuple[Path, str] | None:
        """Ask the callback what this id is, tolerating a callback that throws."""
        if self._reference is None or not voice_id:
            return None
        try:
            return self._reference(voice_id)
        except Exception:  # pragma: no cover - depends on the store's state
            logger.exception("voice reference lookup failed for %s", voice_id)
            return None

    def close(self) -> None:
        """Release this handle. **The worker deliberately stays alive.**

        Loading the weights costs ~31 s and 2.4 GB of VRAM, and both this app and
        the picker build an engine per utterance. Tearing the process down here
        would put that cost on every sentence. The pooled worker is stopped by
        :func:`shutdown_voice_sidecars` when the interpreter exits.
        """
        self._worker = None


__all__ = [
    "CosyVoiceSidecar",
    "cloning_ready",
    "materialised_worker",
    "shutdown_voice_sidecars",
    "sidecar_python",
    "voice_runtime",
    "worker_script",
]
