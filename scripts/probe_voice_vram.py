"""How much VRAM does CosyVoice2 actually need to construct?

The sidecar's load fails with::

    CUDA out of memory. Tried to allocate 18.00 MiB.
    GPU 0 has a total capacity of 4.00 GiB of which 2.06 GiB is free.

Two gigabytes free and an 18 MiB request refused. That is not a capacity
problem, and this script is what proves it: it measures the memory free after
the CUDA context exists, what torch actually holds when construction dies, and
the peak on a machine where it succeeds.

**Run it with the *voice* interpreter, not this project's** -- it imports torch
and CosyVoice, which only the voice virtualenv has, and that is also why it must
not import anything from ``jarvis``::

    $JARVIS_HOME/voice-venv/Scripts/python.exe scripts/probe_voice_vram.py

Point it somewhere else with ``--model-dir``, or set ``JARVIS_HOME`` if the data
root is not the default.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

_DEFAULT_HOME = Path(os.environ.get("JARVIS_HOME") or Path.home() / ".jarvis")


def _default_model(home: Path) -> Path:
    """The CosyVoice2 snapshot under the model cache, if it is there."""
    cache = home / "models" / "modelscope" / "models"
    return cache / "iic--CosyVoice2-0.5B" / "snapshots" / "master"


def mib(value: int) -> float:
    return value / 1024 / 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--model-dir",
        default="",
        help="CosyVoice2 snapshot to load; defaults to the one under JARVIS_HOME",
    )
    parser.add_argument(
        "--home",
        default=str(_DEFAULT_HOME),
        help="data root (defaults to $JARVIS_HOME)",
    )
    args = parser.parse_args(argv)

    home = Path(args.home)
    model = Path(args.model_dir) if args.model_dir else _default_model(home)
    for variable, subdir in (
        ("MODELSCOPE_CACHE", "modelscope"),
        ("HF_HOME", "huggingface"),
        ("TORCH_HOME", "torch"),
    ):
        os.environ.setdefault(variable, str(home / "models" / subdir))

    import torch

    print("model", model, "exists:", model.is_dir())
    print("torch", torch.__version__, "cuda", torch.version.cuda)
    if not torch.cuda.is_available():
        print("no CUDA device visible; nothing to measure")
        return 1
    print("device", torch.cuda.get_device_name(0))

    # Import order matters: building the CUDA context is itself an allocation,
    # so it is paid *before* the first measurement or the number is a lie.
    torch.zeros(1, device="cuda")
    free, total = torch.cuda.mem_get_info()
    print(f"after context: free {mib(free):.0f} MiB / total {mib(total):.0f} MiB")

    from cosyvoice.cli.cosyvoice import CosyVoice2

    started = time.time()
    try:
        model_obj = CosyVoice2(
            str(model),
            load_jit=False,
            load_trt=False,
            load_vllm=False,
            fp16=True,
        )
    except Exception as exc:
        free, _ = torch.cuda.mem_get_info()
        print(f"\nCONSTRUCTION FAILED after {time.time() - started:.1f}s")
        print(f"  {type(exc).__name__}: {str(exc).splitlines()[0]}")
        held = mib(torch.cuda.memory_allocated())
        print(f"  free now {mib(free):.0f} MiB; torch holds {held:.0f} MiB")
        print(
            "\n  Free-but-unusable is the WDDM signature. Note that "
            "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True is NOT a cure on "
            "Windows: PyTorch ignores it with a warning. See docs/声音克隆.md."
        )
        return 1

    peak = mib(torch.cuda.max_memory_allocated())
    free, _ = torch.cuda.mem_get_info()
    print(f"\nCONSTRUCTED in {time.time() - started:.1f}s")
    print(f"  torch peak  {peak:.0f} MiB")
    print(f"  free now    {mib(free):.0f} MiB")
    print(f"  sample rate {model_obj.sample_rate}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
