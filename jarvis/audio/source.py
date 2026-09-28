"""Audio capture sources.

:class:`AudioSource` is the pull-based protocol every capture backend
implements. Production uses :class:`SounddeviceSource` (PortAudio via the
optional ``sounddevice`` package); unit tests inject in-memory fakes so no
test ever touches real hardware.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from jarvis.audio.format import DEFAULT_FORMAT, AudioFormat
from jarvis.core.exceptions import AudioError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import sounddevice


@runtime_checkable
class AudioSource(Protocol):
    """Pull-based PCM capture stream.

    Contract:
        * ``open()`` acquires the device; ``close()`` releases it (idempotent).
        * ``read(samples)`` blocks until that many samples are available and
          returns exactly ``format.bytes_for_samples(samples)`` bytes.
    """

    @property
    def format(self) -> AudioFormat:
        """Format of the produced PCM data."""
        ...

    def open(self) -> None:
        """Acquire the capture device."""
        ...

    def read(self, samples: int) -> bytes:
        """Block until ``samples`` samples are captured, return raw PCM."""
        ...

    def close(self) -> None:
        """Release the capture device (safe to call twice)."""
        ...


class SounddeviceSource:
    """Microphone capture through PortAudio (``sounddevice`` package).

    The import happens inside :meth:`open` so that merely constructing the
    source (or importing this module) works on machines without the
    optional voice dependencies installed — failure is reported as a clear
    :class:`AudioError` only when capture is actually requested.
    """

    def __init__(
        self,
        audio_format: AudioFormat = DEFAULT_FORMAT,
        *,
        device: int | str | None = None,
    ) -> None:
        self._format = audio_format
        self._device = device
        self._stream: sounddevice.RawInputStream | None = None

    @property
    def format(self) -> AudioFormat:
        return self._format

    def open(self) -> None:
        if self._stream is not None:
            return
        try:
            import sounddevice as sd
        except ImportError as exc:  # pragma: no cover - environment specific
            raise AudioError(
                "microphone capture requires the optional 'sounddevice' package "
                "(install with: pip install jarvis-assistant[voice])",
                details={"missing_package": "sounddevice"},
            ) from exc
        try:
            stream = sd.RawInputStream(
                samplerate=self._format.sample_rate,
                channels=self._format.channels,
                dtype="int16",
                device=self._device,
            )
            stream.start()
        except Exception as exc:
            raise AudioError(
                "failed to open the capture device",
                details={"device": self._device, "cause": repr(exc)},
            ) from exc
        self._stream = stream

    def read(self, samples: int) -> bytes:
        if self._stream is None:
            raise AudioError("audio source is not open")
        data, overflowed = self._stream.read(samples)
        if overflowed:
            # Overflow means the consumer fell behind; log-worthy but the
            # stream keeps running. Callers decide whether to care.
            pass
        return bytes(data)

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:  # pragma: no cover - device teardown must not raise
            pass
