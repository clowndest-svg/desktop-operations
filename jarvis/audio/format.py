"""Canonical audio format for the whole voice pipeline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """PCM capture format shared by wake-word, VAD and ASR.

    The pipeline standardises on 16 kHz / mono / signed 16-bit little-endian
    because every supported engine (OpenWakeWord, Porcupine, Silero VAD,
    SenseVoice/FunASR/Whisper) natively consumes exactly that. Converting
    once at the capture boundary beats converting in every consumer.
    """

    sample_rate: int = 16_000
    """Samples per second."""

    channels: int = 1
    """Capture is always mono; engines do not consume stereo."""

    sample_width: int = 2
    """Bytes per sample (2 = s16le)."""

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive, got {self.sample_rate}")
        if self.channels != 1:
            raise ValueError(f"only mono capture is supported, got {self.channels} channels")
        if self.sample_width != 2:
            raise ValueError(f"only 16-bit PCM is supported, got width {self.sample_width}")

    @property
    def frame_bytes_per_sample(self) -> int:
        """Bytes occupied by one sample across all channels."""
        return self.sample_width * self.channels

    def bytes_for_samples(self, samples: int) -> int:
        """Size in bytes of a buffer holding ``samples`` samples."""
        return samples * self.frame_bytes_per_sample

    def duration_ms(self, samples: int) -> float:
        """Duration in milliseconds of ``samples`` samples."""
        return samples * 1000.0 / self.sample_rate


DEFAULT_FORMAT = AudioFormat()
"""The one true capture format; import this instead of constructing ad hoc."""
