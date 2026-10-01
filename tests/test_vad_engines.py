"""Tests for :class:`jarvis.vad.engines.SileroVadEngine`'s inference contract.

The engine imports torch inside ``__init__`` and keeps it as an attribute, which is
what lets these tests hand it a fake: the one thing worth pinning about a TorchScript
call is *how* it is called, and that needs no model at all.
"""

from __future__ import annotations

import numpy
import pytest

from jarvis.core.exceptions import VadError
from jarvis.vad.engines import SileroVadEngine


class _FakeTensor:
    def __init__(self, value: float) -> None:
        self._value = value

    def detach(self) -> _FakeTensor:
        return self

    def cpu(self) -> _FakeTensor:
        return self

    def numpy(self) -> numpy.ndarray:
        return numpy.array([self._value], dtype=numpy.float32)


class _NoGrad:
    def __init__(self, owner: _FakeTorch) -> None:
        self._owner = owner

    def __enter__(self) -> None:
        self._owner.depth += 1

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._owner.depth -= 1


class _FakeTorch:
    """Records whether the model ran inside ``no_grad``.

    ``no_grad`` is modelled as a depth counter rather than a flag: the assertion
    that matters is "grad was disabled *at the moment of the call*", which a
    before/after boolean cannot express if someone moves the call outside the
    block but leaves the block in place.
    """

    def __init__(self) -> None:
        self.depth = 0
        self.depth_at_call = -1

    def no_grad(self) -> _NoGrad:
        return _NoGrad(self)

    def from_numpy(self, array: object) -> _FakeTensor:
        self.depth_at_call = self.depth
        return _FakeTensor(0.77)


def _engine(model: object, torch: _FakeTorch | None = None) -> SileroVadEngine:
    """An engine with its dependencies injected, bypassing the model load.

    ``setattr`` rather than attribute assignment: these are the private handles
    ``__init__`` fills in from the real torch/numpy, and a test handing it fakes is
    exactly the case ``__new__`` + injection exists for.
    """
    engine = object.__new__(SileroVadEngine)
    setattr(engine, "_model", model)  # noqa: B010
    setattr(engine, "_numpy", numpy)  # noqa: B010
    setattr(engine, "_torch", torch if torch is not None else _FakeTorch())  # noqa: B010
    setattr(engine, "_sample_rate", 16_000)  # noqa: B010
    return engine


def _frame() -> bytes:
    return b"\x01\x00" * SileroVadEngine._FRAME_SAMPLES


def test_inference_runs_with_gradients_disabled() -> None:
    """The regression: 50 MB/minute of silence, in native memory tracemalloc can't see.

    Silero's wrapper is stateful -- it feeds its own RNN state into the next call --
    so with autograd on, every frame's graph chains onto the last one and the
    process grows for as long as the microphone is open. ``detach()`` on the result
    does not help; the chain is held by the model's state, not by the returned
    tensor. The measured climb was +49.4 MB/min, and flat (0.0 MB/min) with this
    block in place, at identical frame counts.
    """
    torch = _FakeTorch()
    engine = _engine(lambda *args: _FakeTensor(0.77), torch)

    score = engine.process(_frame())

    assert score == pytest.approx(0.77)
    assert torch.depth_at_call == 1, (
        "the model was called with autograd enabled; the stateful RNN will chain "
        "one graph per frame and the process will never stop growing"
    )


def test_grad_block_is_left_after_the_call() -> None:
    """Otherwise a stray ``no_grad()`` elsewhere would look like the same fix."""
    torch = _FakeTorch()
    engine = _engine(lambda *args: _FakeTensor(0.5), torch)

    engine.process(_frame())

    assert torch.depth == 0


def test_a_wrong_sized_frame_is_refused_before_the_model_runs() -> None:
    called: list[int] = []

    def model(*args: object) -> _FakeTensor:
        called.append(1)
        return _FakeTensor(0.0)

    engine = _engine(model)

    with pytest.raises(VadError):
        engine.process(b"\x00\x00" * 100)

    assert not called
