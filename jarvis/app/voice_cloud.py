"""Getting a recorded voice onto the vendor, and off it again.

This is the seam between the store (:mod:`jarvis.app.voice_library`, which knows
nothing about networks) and the client (:mod:`jarvis.tts.cloud`, which knows
nothing about the index). Both halves are deliberate:

* the store's ``add`` must not open an HTTPS connection, or every test of it
  would need a network;
* the client must not read the index, or it would have to be re-tested against
  every change to the on-disk format.

So the two calls that have to happen *in a particular order* -- upload, then
link; delete the cloud half, then forget the local one -- live here.

**Why this is in ``app`` and not in the UI.** It used to live in
``jarvis/ui/desktop.py``, which was a layering violation: the HUD layer is only
allowed to reach ``app``/``config``/``core``, and ``tts`` is none of those. The
test that caught it (``test_no_layer_violations``) is the point of having the
rule -- the HUD is not a place where a network call is allowed to grow.

Every function here **never raises**. Each one runs *after* something the user
already has (a stored recording, or an id they just typed), so a failure has to
come back as a sentence to show them rather than as a broken reply. The caller
gets ``""`` for success and Chinese prose for anything else.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.app.voice_library import VoiceLibrary

logger = logging.getLogger("jarvis.app.voice_cloud")


def upload_voice(
    library: VoiceLibrary | None,
    voice_id: str,
    audio: bytes,
    sample_rate: int,
    prompt_text: object,
) -> str:
    """Send one stored recording to the vendor, and link the returned id.

    Returns ``""`` on success, or a sentence for the user. Never raises.

    The order matters. The recording is already stored by the time this runs, so
    a failed upload degrades to *a working local voice plus a message* -- never
    to a lost recording. That is why nothing here can propagate an exception.

    The key is resolved per call inside :func:`jarvis.tts.cloud.enrol`, so a key
    typed into the settings panel applies to the next recording rather than to
    the next restart.
    """
    if library is None:
        return ""
    try:
        from jarvis.tts.cloud import CloudVoiceError, enrol
    except ImportError as exc:  # pragma: no cover - the module is in-tree
        logger.warning("cloud voices are unavailable: %s", exc)
        return "这个版本没有云端音色的代码"
    try:
        enrolled = enrol(
            pcm=audio,
            sample_rate=sample_rate,
            transcript=str(prompt_text or ""),
            name=str(voice_id.removeprefix("clone:")),
        )
    except CloudVoiceError as exc:
        logger.info("cloud enrolment failed for %s: %s", voice_id, exc)
        return str(exc)
    except Exception as exc:  # pragma: no cover - unexpected vendor/network shape
        logger.warning("cloud enrolment raised unexpectedly for %s", voice_id, exc_info=True)
        return f"上传到云端时出了意外：{type(exc).__name__}"
    if not library.link_cloud(
        voice_id, cloud_voice=enrolled.voice, cloud_model=enrolled.target_model
    ):
        # The voice was deleted while the sample was in flight. Do not leave a
        # clone on somebody else's servers that nothing here points at.
        forget_cloud_voice(enrolled.voice)
        return "这个音色在录的过程中被删掉了"
    if enrolled.fallback_reason:
        # Degraded on the vendor's side: it will sound unlike the person, and the
        # next obvious move (re-record somewhere quieter) is only findable if it
        # is said.
        return f"云端只做了降级克隆（{enrolled.fallback_reason}），可能不太像"
    return ""


def forget_cloud_voice(cloud_voice: str) -> None:
    """Best-effort vendor-side delete, for a voice nothing local points at.

    Used in two places, and both are about not leaving a clone alive: after the
    user deletes the recording, and as a cleanup when the link could not be
    written. Failures are swallowed on purpose -- the local state is already
    correct by then, and an orphan on the vendor's side is not worth turning a
    successful delete into an error message.
    """
    import contextlib

    try:
        from jarvis.tts.cloud import delete
    except ImportError:  # pragma: no cover - the module is in-tree
        return
    with contextlib.suppress(Exception):
        delete(cloud_voice)


__all__ = ["forget_cloud_voice", "upload_voice"]
