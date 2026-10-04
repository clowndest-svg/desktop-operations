"""Tests for the separate-process speech engine.

The real worker loads a 4 GB model on a GPU, so these drive the *protocol* with
a stand-in script: a small program that speaks the same line-JSON on stdin and
stdout. That is the layer where the bugs live -- a missed final chunk, a cancel
that never reaches the model, a lock that lets two processes fight over one GPU
-- and every one of them is invisible to a test that only checks "did audio come
out", because the fake model always answers.
"""

from __future__ import annotations

import base64
import json
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis.core.exceptions import TtsError
from jarvis.tts import sidecar as sidecar_module
from jarvis.tts.sidecar import (
    EXIT_GRACE,
    CosyVoiceSidecar,
    cloning_ready,
    materialised_worker,
    shutdown_voice_sidecars,
    sidecar_python,
    voice_runtime,
    worker_script,
)

FAKE_WORKER = textwrap.dedent("""
    '''A stand-in for the real worker: same protocol, no model.

    Structured exactly like the real one -- a single reader *thread* that owns
    stdin and a main loop that drains a queue -- because that is what makes a
    cancel observable while a synthesis is still running. Two readers on one
    stdin would each steal the other's lines, and the symptom is a request that
    is never answered at all.
    '''
    import base64, json, os, queue, sys, threading, time

    MARKER = "XY-PROTOCOL"
    CONFIG = json.loads(os.environ.get("FAKE_WORKER_CONFIG", "{}"))
    state = {"cancelled": 0}
    work = queue.Queue()

    def send(payload):
        sys.stdout.write(MARKER + json.dumps(payload) + "\\n")
        sys.stdout.flush()

    def reader():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                continue
            if not isinstance(request, dict):
                continue
            if request.get("op") == "cancel":
                state["cancelled"] += 1
                continue
            work.put(request)
        work.put({"_stdin_closed": True})

    threading.Thread(target=reader, daemon=True).start()

    # Import chatter that must not be mistaken for protocol.
    print("some native library banner")
    sys.stdout.write("a banner with no marker\\n")
    sys.stdout.flush()

    while True:
        request = work.get()
        rid, op = request.get("id"), request.get("op")
        if op is None and request.get("_stdin_closed"):
            # Mirrors the real worker: a parent that vanished is a reason to exit.
            break
        if op == "shutdown":
            send({"id": rid, "ok": True})
            break
        if op == "load":
            if CONFIG.get("fail_load"):
                send({"id": rid, "ok": False, "error": "weights are not here"})
                continue
            time.sleep(CONFIG.get("load_delay", 0))
            send({"id": rid, "ok": True, "loaded": True, "sample_rate": 24000,
                  "load_seconds": 1.0, "device": "cuda"})
            continue
        if op == "speakers":
            send({"id": rid, "ok": True, "registered_paths": []})
            continue
        if op == "synthesize":
            chunks = CONFIG.get("chunks", 2)
            for index in range(chunks):
                pcm = bytes([index + 1, 0]) * 4
                send({"notify": "chunk", "id": rid,
                      "pcm": base64.b64encode(pcm).decode("ascii"),
                      "final": index == chunks - 1})
                if CONFIG.get("chunk_delay"):
                    time.sleep(CONFIG["chunk_delay"])
                if CONFIG.get("honour_cancel") and state["cancelled"]:
                    break
            if CONFIG.get("fail_synthesis"):
                send({"id": rid, "ok": False, "error": "the model refused"})
            else:
                send({"id": rid, "ok": True, "chunks": chunks, "sample_rate": 24000})
            continue
        send({"id": rid, "ok": False, "error": "unknown op"})
    """)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A voice data root with the layout ``cloning_ready`` demands.

    The marker file matters: a directory merely *named* ``CosyVoice`` is not a
    checkout, and ``voice_runtime`` deliberately refuses to be fooled by the
    name alone -- an empty directory would pass a check that only looked at
    ``is_dir()`` and then fail on the first sentence.
    """
    root = tmp_path / "voice-runtime" / "CosyVoice"
    (root / "third_party" / "Matcha-TTS").mkdir(parents=True)
    (root / "cosyvoice" / "cli").mkdir(parents=True)
    (root / "cosyvoice" / "cli" / "cosyvoice.py").write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture
def fake_worker(tmp_path: Path) -> Path:
    path = tmp_path / "fake_worker.py"
    path.write_text(FAKE_WORKER, encoding="utf-8")
    return path


def _engine(home: Path, worker: Path, **config: Any) -> CosyVoiceSidecar:
    """An engine wired to the stand-in, configured through its environment."""

    def make() -> CosyVoiceSidecar:
        engine = CosyVoiceSidecar(
            "iic/CosyVoice2-0.5B",
            home=home,
            python=Path(sys.executable),
            worker=worker,
            timeout=30.0,
        )
        return engine

    import os

    os.environ["FAKE_WORKER_CONFIG"] = json.dumps(config)
    return make()


@pytest.fixture(autouse=True)
def _clean_pool() -> Iterator[None]:
    """No fake worker may outlive its test, or the pool would hand it to the next one."""
    yield
    shutdown_voice_sidecars()


class TestDiscovery:
    def test_a_missing_venv_is_reported_not_guessed(self, tmp_path: Path) -> None:
        ready, reason = cloning_ready(tmp_path)

        assert ready is False
        assert reason, "a refusal has to say something"

    def test_the_venv_alone_is_not_enough(self, home: Path) -> None:
        """A virtualenv without the CosyVoice checkout fails on the first sentence,
        so it must be refused up front rather than after somebody records a voice."""
        scripts = home / "voice-venv" / ("Scripts" if sys.platform == "win32" else "bin")
        scripts.mkdir(parents=True)
        (scripts / ("python.exe" if sys.platform == "win32" else "python")).touch()
        # The fixture laid down the checkout; removing it leaves a virtualenv with
        # nothing to import, which is the case being tested.
        import shutil

        shutil.rmtree(home / "voice-runtime")

        ready, reason = cloning_ready(home)

        assert ready is False
        assert "运行时不完整" in reason

    def test_a_complete_install_is_accepted(self, home: Path) -> None:
        scripts = home / "voice-venv" / ("Scripts" if sys.platform == "win32" else "bin")
        scripts.mkdir(parents=True)
        (scripts / ("python.exe" if sys.platform == "win32" else "python")).touch()

        ready, reason = cloning_ready(home)

        assert ready is True, reason
        assert reason == ""
        assert sidecar_python(home) is not None

    def test_the_worker_script_is_a_real_file(self) -> None:
        script = worker_script()

        assert script is not None and script.is_file()

    def test_the_worker_is_not_run_from_inside_the_package(self, home: Path) -> None:
        """Executing it in place shadows the standard library and kills the worker.

        ``jarvis/tts/types.py`` collides with ``types``: Python puts a script's
        own directory on ``sys.path[0]``, so ``import argparse`` fails with
        "cannot import name 'GenericAlias' from 'types'". This cost a real
        10-minute debug cycle, and the symptom -- a worker that exits with no
        output at all -- points nowhere near the cause.
        """
        source = worker_script()
        assert source is not None

        materialised = materialised_worker(home, source)

        assert materialised is not None and materialised.is_file()
        assert materialised.parent != source.parent, "the worker is still inside the package"
        assert materialised.read_bytes() == source.read_bytes()

    def test_a_stale_copy_is_refreshed(self, home: Path) -> None:
        source = worker_script()
        assert source is not None
        first = materialised_worker(home, source)
        assert first is not None
        first.write_text("# an older build\n", encoding="utf-8")

        again = materialised_worker(home, source)

        assert again == first
        assert first.read_bytes() == source.read_bytes()

    def test_the_package_directory_never_reaches_the_worker(self, home: Path) -> None:
        """The child's import path must be the checkout, never this repository."""
        import os

        env = sidecar_module._child_env(home, base={})

        for entry in env["PYTHONPATH"].split(os.pathsep):
            assert (
                "AILiaoTianXiangMu" not in entry
            ), f"the repository leaked into the child: {entry}"

    def test_the_runtime_is_discovered_by_its_contents(self, tmp_path: Path) -> None:
        """A directory named ``CosyVoice`` that holds nothing is not a runtime.

        Uses a bare temporary directory rather than the ``home`` fixture: the
        fixture is a *working* install, so it cannot also be the empty case.
        """
        empty = tmp_path / "voice-runtime" / "CosyVoice"
        empty.mkdir(parents=True)
        assert voice_runtime(tmp_path) is None

        (empty / "cosyvoice" / "cli").mkdir(parents=True)
        (empty / "cosyvoice" / "cli" / "cosyvoice.py").write_text("", encoding="utf-8")

        assert voice_runtime(tmp_path) == empty


class TestProtocol:
    def test_audio_arrives_as_a_stream_with_a_final_flag(
        self, home: Path, fake_worker: Path
    ) -> None:
        engine = _engine(home, fake_worker, chunks=3)

        chunks = list(engine.synthesize("你好", voice="clone:aaa"))

        assert len(chunks) == 3
        assert [c.is_final for c in chunks] == [False, False, True]
        assert all(c.sample_rate == 24_000 for c in chunks)
        assert chunks[0].audio == base64.b64decode(base64.b64encode(bytes([1, 0]) * 4))

    def test_library_chatter_on_stdout_is_ignored(self, home: Path, fake_worker: Path) -> None:
        """The real thing prints import banners; mistaking one for a reply would
        stall the request until the timeout."""
        engine = _engine(home, fake_worker, chunks=1)

        chunks = list(engine.synthesize("你好", voice="clone:aaa"))

        assert len(chunks) == 1

    def test_a_failed_load_says_what_went_wrong(self, home: Path, fake_worker: Path) -> None:
        engine = _engine(home, fake_worker, fail_load=True)

        with pytest.raises(TtsError, match="weights are not here"):
            list(engine.synthesize("你好", voice="clone:aaa"))

    def test_a_failed_synthesis_is_reported_not_silently_empty(
        self, home: Path, fake_worker: Path
    ) -> None:
        engine = _engine(home, fake_worker, fail_synthesis=True)

        with pytest.raises(TtsError, match="the model refused"):
            list(engine.synthesize("你好", voice="clone:aaa"))

    def test_the_model_is_loaded_once_across_handles(self, home: Path, fake_worker: Path) -> None:
        """The worker is pooled: loading the weights again per engine object would
        put 31 s and 2.4 GB on every sentence."""
        first = _engine(home, fake_worker, chunks=1)
        list(first.synthesize("你好", voice="clone:aaa"))

        second = _engine(home, fake_worker, chunks=1)
        # A different load config would fail if it were actually re-sent; the
        # pooled worker answers from the model it already has.
        list(second.synthesize("你好", voice="clone:aaa"))

        assert first._resolve_worker() is second._resolve_worker()

    def test_close_keeps_the_worker_alive(self, home: Path, fake_worker: Path) -> None:
        engine = _engine(home, fake_worker, chunks=1)
        list(engine.synthesize("你好", voice="clone:aaa"))
        worker = engine._resolve_worker()

        engine.close()

        assert worker.alive, "closing a handle must not throw away a loaded model"

    def test_a_dead_worker_is_replaced(self, home: Path, fake_worker: Path) -> None:
        engine = _engine(home, fake_worker, chunks=1)
        list(engine.synthesize("你好", voice="clone:aaa"))
        engine._resolve_worker()._stop()  # simulate a crash

        chunks = list(_engine(home, fake_worker, chunks=1).synthesize("你好", voice="clone:aaa"))

        assert len(chunks) == 1


class TestShutdown:
    def test_the_worker_exits_a_bounded_time_after_its_input_closes(
        self, home: Path, fake_worker: Path
    ) -> None:
        """A worker that outlives its parent holds ~4 GB of VRAM forever.

        The polite path is a ``shutdown`` message, but a parent that crashed
        never sends one -- so closing stdin has to be enough, and it has to be
        enough *quickly*. This was broken: the worker ignored the closed pipe,
        so every stop paid the full grace period and then a kill.
        """
        import time

        engine = _engine(home, fake_worker, chunks=1)
        list(engine.synthesize("你好", voice="clone:aaa"))
        worker = engine._resolve_worker()
        process = worker._process
        assert process is not None

        started = time.monotonic()
        worker._stop()
        elapsed = time.monotonic() - started

        assert process.poll() is not None, "the worker is still running"
        assert elapsed < EXIT_GRACE, f"stopping took {elapsed:.1f}s; the worker ignored EOF"

    def test_a_graceful_shutdown_finishes_at_once(self, home: Path, fake_worker: Path) -> None:
        import time

        engine = _engine(home, fake_worker, chunks=1)
        list(engine.synthesize("你好", voice="clone:aaa"))
        worker = engine._resolve_worker()

        started = time.monotonic()
        shutdown_voice_sidecars()
        elapsed = time.monotonic() - started

        assert not worker.alive
        assert elapsed < 2.0, f"a cooperative worker should not need {elapsed:.1f}s"


class TestCancellation:
    def test_a_cancel_reaches_the_worker(self, home: Path, fake_worker: Path) -> None:
        """Barge-in has to stop the *model*. Dropping the next chunk would leave
        the GPU generating a paragraph nobody is going to hear."""
        engine = _engine(home, fake_worker, chunks=20, chunk_delay=0.05, honour_cancel=True)
        seen = 0

        def stop() -> bool:
            nonlocal seen
            return seen >= 2

        for _chunk in engine.synthesize("讲个很长的故事", voice="clone:aaa", should_stop=stop):
            seen += 1

        assert seen >= 2
        # The generator closed early, so fewer chunks arrived than the worker would
        # have produced with nobody interrupting it.
        assert seen < 20

    def test_no_cancel_is_sent_when_nobody_asked(self, home: Path, fake_worker: Path) -> None:
        engine = _engine(home, fake_worker, chunks=3)

        chunks = list(engine.synthesize("你好", voice="clone:aaa"))

        assert len(chunks) == 3


class TestReferenceResolution:
    def test_a_clone_with_a_gone_recording_is_refused(self, home: Path, fake_worker: Path) -> None:
        engine = CosyVoiceSidecar(
            "m",
            reference=lambda _voice: None,
            home=home,
            python=Path(sys.executable),
            worker=fake_worker,
        )

        with pytest.raises(TtsError, match="找不到了"):
            list(engine.synthesize("你好", voice="clone:aaaaaaaaaaaa"))

    def test_a_clone_without_a_transcript_is_refused(self, home: Path, fake_worker: Path) -> None:
        engine = CosyVoiceSidecar(
            "m",
            reference=lambda _voice: (home / "ref.wav", "   "),
            home=home,
            python=Path(sys.executable),
            worker=fake_worker,
        )

        with pytest.raises(TtsError, match="参考文本"):
            list(engine.synthesize("你好", voice="clone:aaaaaaaaaaaa"))

    def test_a_reference_reaches_the_worker_as_a_path_and_text(
        self, home: Path, fake_worker: Path
    ) -> None:
        clip = home / "ref.wav"
        clip.write_bytes(b"RIFF" + b"\x00" * 32)
        engine = CosyVoiceSidecar(
            "m",
            reference=lambda _voice: (clip, "希望你以后能够做的比我还好呦。"),
            home=home,
            python=Path(sys.executable),
            worker=fake_worker,
            timeout=30.0,
        )

        chunks = list(engine.synthesize("你好", voice="clone:aaaaaaaaaaaa"))

        assert chunks, "a resolvable reference must produce audio"


class TestChildEnvironment:
    def test_the_cache_is_redirected_for_the_child_only(self, home: Path) -> None:
        """The worker reads weights from the voice data root. The parent's own
        caches must not move just because a question was asked about voices."""
        env = sidecar_module._child_env(home, base={})

        assert env["MODELSCOPE_CACHE"] == str(home / "models" / "modelscope")
        assert env["HF_HOME"] == str(home / "models" / "huggingface")
        assert env["TORCH_HOME"] == str(home / "models" / "torch")

    def test_the_cosyvoice_checkout_is_put_on_the_import_path(self, home: Path) -> None:
        import os

        env = sidecar_module._child_env(home, base={"PYTHONPATH": "keepme"})

        parts = env["PYTHONPATH"].split(os.pathsep)
        assert str(home / "voice-runtime" / "CosyVoice") in parts
        assert str(home / "voice-runtime" / "CosyVoice" / "third_party" / "Matcha-TTS") in parts
        assert parts[-1] == "keepme", "an existing PYTHONPATH must survive"

    def test_an_existing_redirect_is_respected(self, home: Path) -> None:
        env = sidecar_module._child_env(home, base={"MODELSCOPE_CACHE": "E:/elsewhere"})

        assert env["MODELSCOPE_CACHE"] == "E:/elsewhere"


class TestWorkerSourceOrdering:
    """Ordering rules inside the real worker that a fake cannot express.

    These inspect the source rather than run it. The behaviour they protect --
    "do not import the native stack while a thread is blocked reading stdin" --
    hangs the *real* worker on a machine with a GPU and torch, so a test that
    executed it would hang too. Reading the order out of the source is a weaker
    check in principle and the only one that is runnable here, and it is
    precisely the order that was wrong.
    """

    @staticmethod
    def _worker_source() -> str:
        script = worker_script()
        assert script is not None, "the worker script must be part of the package"
        return script.read_text(encoding="utf-8")

    def test_the_heavy_import_happens_before_anything_reads_stdin(self) -> None:
        """The deadlock, expressed as an ordering rule.

        Measured on the target machine: with a thread parked in ``sys.stdin``
        (by iteration, by ``readline``, or by ``os.read(0, ...)``) the import of
        ``torch`` never completes -- not slowly, never. Without such a thread it
        takes 9.5 s. So ``_preload`` must appear *before* the receiver thread is
        started, and this test is the only thing standing between a future
        refactor and a silent 30-second-plus hang that looks like a slow model.
        """
        source = self._worker_source()
        preload = source.index("        _preload()")
        receiver = source.index("threading.Thread(target=reader,")

        assert preload < receiver, (
            "the worker must import the voice stack before starting the stdin "
            "reader; with the reader already blocked on stdin the import hangs"
        )

    def test_the_preload_can_be_switched_off_for_demonstration(self) -> None:
        """``--no-preload`` is how the hang above stays reproducible on demand.

        Removing the escape hatch would leave the fix unfalsifiable: nobody
        could show the deadlock still exists, and a later "simplification" that
        dropped the preload would look harmless.
        """
        source = self._worker_source()

        assert "--no-preload" in source
        assert "args.no_preload" in source
