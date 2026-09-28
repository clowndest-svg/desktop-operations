"""PyInstaller runtime hook: finish funasr's model registration in a frozen app.

funasr registers model classes as a side effect of importing them, and its
auto-registration discovers the modules by **walking the package directory on
disk**. In a frozen build the modules live inside the PYZ archive, so the walk
finds almost nothing: on this project's build the registry came up with 13 models
instead of 48, and ``SenseVoiceSmall`` was not among them. The user-visible symptom
is a red "语音不可用" and::

    RuntimeError: model 'SenseVoiceSmall' is not registered

Importing the modules explicitly restores the registry. Failures are written to
``<data>/logs/funasr-hook.log`` rather than swallowed: this is a windowed app, so
stdout goes nowhere, and a hook that quietly skips a broken import is exactly the
kind of defect that costs an afternoon.
"""

from __future__ import annotations

import importlib
import os

# Everything SenseVoice needs, plus the fallbacks the configuration can select.
PREFERRED = (
    "funasr.models.sense_voice.model",
    "funasr.models.sense_voice.encoder",
    "funasr.models.sense_voice.decoder",
    "funasr.models.sense_voice.tokenizer",
    "funasr.models.paraformer.model",
    "funasr.models.paraformer.encoder",
    "funasr.models.paraformer.decoder",
    "funasr.models.transformer.model",
    "funasr.models.ct_transformer.model",
    "funasr.models.fsmn_vad_streaming.model",
    "funasr.models.e2e_asr_transformer",
    "funasr.frontends.default",
)

_FAILURES: list[str] = []
_IMPORTED = 0


def _try(name: str) -> bool:
    """Import one module, recording why it failed instead of hiding it."""
    global _IMPORTED
    try:
        importlib.import_module(name)
    except Exception as exc:  # optional dependency; report it, never swallow silently
        _FAILURES.append(f"{name}: {type(exc).__name__}: {exc}")
        return False
    _IMPORTED += 1
    return True


for _name in PREFERRED:
    _try(_name)

# Belt and braces: sweep whatever *is* importable under funasr.models so a future
# engine change does not silently come back broken. The modules are already in the
# archive; this only runs their @tables.register decorators.
try:
    import pkgutil

    import funasr.models as _models

    for _info in pkgutil.walk_packages(_models.__path__, prefix="funasr.models."):
        _try(_info.name)
except Exception as _exc:  # the sweep is a bonus; its failure is not fatal
    _FAILURES.append(f"funasr.models sweep: {type(_exc).__name__}: {_exc}")

try:
    from funasr.register import tables

    _registered = "SenseVoiceSmall" in getattr(tables, "model_classes", {})
except Exception as _exc:
    _registered = False
    _FAILURES.append(f"registry probe: {type(_exc).__name__}: {_exc}")

lines = [f"imported={_IMPORTED} SenseVoiceSmall_registered={_registered} failures={len(_FAILURES)}"]
lines.extend("  " + entry for entry in _FAILURES[:12])
report = "\n".join(lines)

print(f"[jarvis][funasr hook] {report}", flush=True)
try:
    _root = os.environ.get("JARVIS_HOME") or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Jarvis"
    )
    _logs = os.path.join(_root, "logs")
    os.makedirs(_logs, exist_ok=True)
    with open(os.path.join(_logs, "funasr-hook.log"), "a", encoding="utf-8") as _sink:
        _sink.write(report + "\n")
except Exception:
    pass
