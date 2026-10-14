"""Synthetic library fixtures may ship, while real user audio remains blocked."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def test_only_scipy_synthetic_audio_is_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path(__file__).resolve().parents[1] / "scripts" / "delivery_package.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("media_delivery_fixture_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prefix = "小夜/本地引擎/venv/Lib/site-packages/scipy/io/tests/data/"
    assert module._scipy_test_audio(prefix + "test-44100Hz-2ch-32bit-float-le.wav")
    for member in (
        "小夜/本地引擎/ComfyUI/input/recording.wav",
        "小夜/本地引擎/ComfyUI/user/recording.wav",
        "小夜/数据/voices/recording.wav",
        prefix + "recording.wav",
        prefix + "test-44100Hz/recording.wav",
        prefix + "test-44100Hz-secret.key",
    ):
        assert not module._scipy_test_audio(member)
