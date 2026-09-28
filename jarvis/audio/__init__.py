"""Audio capture primitives shared by the voice pipeline.

Responsibility (delivered in phase 6):
    * :class:`AudioFormat` — the canonical capture format (16 kHz mono
      s16le) every voice component agrees on.
    * :class:`AudioSource` — pull-based capture protocol; production
      implementation :class:`SounddeviceSource` (PortAudio via the optional
      ``sounddevice`` package), tests inject fakes.
    * :class:`FrameAssembler` — regroups arbitrary byte chunks into the
      fixed-size frames wake-word/VAD engines require.

This package exists because ``wakeword``, ``vad`` and ``asr`` all consume
the *same* microphone stream; capture must not be reimplemented per
consumer.

Allowed dependencies: ``core``, ``config``.
"""

from jarvis.audio.format import AudioFormat
from jarvis.audio.frames import FrameAssembler
from jarvis.audio.source import AudioSource, SounddeviceSource

__all__ = [
    "AudioFormat",
    "AudioSource",
    "FrameAssembler",
    "SounddeviceSource",
]
