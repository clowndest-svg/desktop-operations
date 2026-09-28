"""Wake-word detection: always-on, low-CPU keyword spotting.

Responsibility (delivered in phase 6):
    * :class:`WakeWordEngine` protocol with OpenWakeWord (default, offline)
      and Porcupine (optional, AccessKey) adapters.
    * :class:`AsrWakeWordEngine` — Chinese/any-language wake word by matching
      the transcript of each utterance; wired up by the orchestrator, which
      lends it the already-loaded recognition model.
    * :class:`WakeWordDetector` — threshold + cooldown debouncing.
    * :class:`WakeWordService` — lifecycle component owning the microphone
      loop; emits :class:`WakeEvent` to the wake callback.

Both native engines are optional dependencies (``pip install jarvis-assistant[voice]``);
the service is disabled by default (``wakeword.enabled: false``).

Allowed dependencies: ``core``, ``config``, ``audio``.
"""

from jarvis.wakeword.asr_engine import AsrWakeWordEngine
from jarvis.wakeword.detector import WakeWordDetector
from jarvis.wakeword.engines import OpenWakeWordEngine, PorcupineEngine
from jarvis.wakeword.service import (
    WakeWordService,
    WakeWordSettings,
    default_engine_factory,
)
from jarvis.wakeword.types import WakeEvent, WakeHit, WakeWordEngine

__all__ = [
    "AsrWakeWordEngine",
    "OpenWakeWordEngine",
    "PorcupineEngine",
    "WakeEvent",
    "WakeHit",
    "WakeWordDetector",
    "WakeWordEngine",
    "WakeWordService",
    "WakeWordSettings",
    "default_engine_factory",
]
