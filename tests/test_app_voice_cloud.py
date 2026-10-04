"""The upload seam: recording in, vendor id out, and nothing lost on the way.

:mod:`jarvis.app.voice_cloud` is the only place the store and the cloud client
meet, and the reason it exists as its own module is that the two calls have to
happen in a particular order. So what is tested here is the ordering and the
failure policy, not the HTTPS -- ``jarvis.tts.cloud`` has its own tests for that,
and the vendor call there is stubbed for the same reason it is stubbed anywhere:
it needs a key and a network.

Two of these cases are only reachable from here. The bridge tests in
``test_ui_desktop.py`` can see the reply but not the *orphan* -- a cloud voice
created by a vendor call that then failed to link -- and that is exactly the
state this module is responsible for not leaving behind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jarvis.app import voice_cloud
from jarvis.app.voice_library import VoiceLibrary


def _library(tmp_path: Path) -> VoiceLibrary:
    library = VoiceLibrary(tmp_path / "voices")
    library.start()
    return library


def _pcm(seconds: float = 4.0, sample_rate: int = 16_000) -> bytes:
    """Silence of the right length: the vendor is stubbed, content is irrelevant."""
    return b"\x00\x00" * int(sample_rate * seconds)


class _Enrolment:
    """A scripted stand-in for ``cloud.enrol`` that records what it was asked."""

    def __init__(self, *, voice: str = "xyvoiceSelf01", target: str = "m", fallback: str = ""):
        self.voice = voice
        self.target = target
        self.fallback = fallback
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        from jarvis.tts.cloud import EnrolledVoice

        self.calls.append(kwargs)
        return EnrolledVoice(
            voice=self.voice, target_model=self.target, fallback_reason=self.fallback
        )


class _Boom:
    """A stand-in that raises whatever the case under test needs."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def __call__(self, **kwargs: Any) -> Any:
        self.calls += 1
        raise self.error


class _Deleter:
    """A stand-in for ``cloud.delete`` that records and reports success.

    A class rather than ``lambda voice, **kw: seen.append(voice) or True``:
    under ``mypy --strict`` a ``list.append`` inside an expression is an error
    (``func-returns-value``), and the honest fix is a method that returns a real
    bool instead of a type ignore.
    """

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def __call__(self, voice: str, **kwargs: Any) -> bool:
        self.deleted.append(voice)
        return True


class TestUploadPolicy:
    """What has to be true regardless of what the vendor says."""

    def test_no_library_is_not_an_error(self) -> None:
        """A process without a store (a test bridge, a stripped build) must not
        crash on the way to a reply: the caller checks for ``""``."""
        assert voice_cloud.upload_voice(None, "clone:x", b"", 16_000, "") == ""

    def test_the_recording_is_untouched_when_the_vendor_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole reason the upload runs *after* ``add``: a rejection has to
        leave a working local voice behind."""
        from jarvis.tts import cloud

        library = _library(tmp_path)
        stored = library.add(name="我", pcm=_pcm(), sample_rate=16_000, prompt_text="你好")
        monkeypatch.setattr(cloud, "enrol", _Boom(cloud.CloudVoiceError("Key 不对")))

        message = voice_cloud.upload_voice(library, stored.voice_id, _pcm(), 16_000, "你好")

        assert message == "Key 不对"
        assert [voice.voice_id for voice in library.voices()] == [stored.voice_id]
        assert library.voices()[0].cloud_voice == ""

    def test_an_unexpected_exception_becomes_a_sentence_not_a_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This runs on the bridge thread, where an escaping exception is a
        silently dead page. Anything the vendor client does not model has to
        arrive as prose."""
        from jarvis.tts import cloud

        library = _library(tmp_path)
        stored = library.add(name="我", pcm=_pcm(), sample_rate=16_000, prompt_text="你好")
        monkeypatch.setattr(cloud, "enrol", _Boom(RuntimeError("socket reset")))

        message = voice_cloud.upload_voice(library, stored.voice_id, _pcm(), 16_000, "你好")

        assert "RuntimeError" in message
        assert library.voices()[0].cloud_voice == ""

    def test_the_name_sent_has_no_clone_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ids are ``clone:<random hex>`` on this side and the vendor has never
        heard of that prefix, so the label must not carry it.

        The label is the id's payload rather than the human name on purpose: a
        Chinese name sanitises to nothing (the vendor takes ASCII alphanumerics)
        and every voice would then share one fallback label. The hex suffix is
        unique, which is the one property the label actually needs.
        """
        from jarvis.tts import cloud

        library = _library(tmp_path)
        stored = library.add(name="我的声音", pcm=_pcm(), sample_rate=16_000, prompt_text="你好")
        enroller = _Enrolment()
        monkeypatch.setattr(cloud, "enrol", enroller)

        voice_cloud.upload_voice(library, stored.voice_id, _pcm(), 16_000, "你好")

        sent = str(enroller.calls[0]["name"])
        assert not sent.startswith("clone:")
        assert sent, "an empty label would be sanitised away by the vendor"


class TestLinkAndOrphan:
    """The states that only exist between the upload and the index write."""

    def test_a_successful_upload_links_the_vendor_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jarvis.tts import cloud

        library = _library(tmp_path)
        stored = library.add(name="我", pcm=_pcm(), sample_rate=16_000, prompt_text="你好")
        monkeypatch.setattr(cloud, "enrol", _Enrolment(voice="xyvoiceAbc123", target="m2"))

        assert voice_cloud.upload_voice(library, stored.voice_id, _pcm(), 16_000, "你好") == ""
        assert library.voices()[0].cloud_voice == "xyvoiceAbc123"
        assert library.voices()[0].cloud_model == "m2"

    def test_a_voice_deleted_mid_upload_is_cleaned_up_on_the_vendor_side(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The race the store's ``link_cloud`` reports: the recording was deleted
        while the sample was in flight. The vendor already made the clone, so
        nothing local points at it -- and a clone of somebody's voice left alive
        with nothing pointing at it is the state this module exists to avoid."""
        from jarvis.tts import cloud

        library = _library(tmp_path)
        enroller = _Enrolment(voice="xyvoiceOrphan")
        monkeypatch.setattr(cloud, "enrol", enroller)
        deleter = _Deleter()
        monkeypatch.setattr(cloud, "delete", deleter)

        # A voice id that is not in the index, which is what a delete during the
        # upload looks like from here.
        message = voice_cloud.upload_voice(library, "clone:gone", _pcm(), 16_000, "你好")

        assert "被删掉" in message
        assert deleter.deleted == ["xyvoiceOrphan"]

    def test_a_degraded_clone_is_linked_and_announced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``fallback_reason`` means it will not sound like the person -- not that
        it failed.

        A degraded clone still synthesises, so it is linked (otherwise the picker
        would silently route a usable fast voice through the slow local path) and
        the degradation is reported alongside. Swallowing the reason would leave
        the user unable to explain why their own voice came back sounding like a
        stranger; refusing the link would throw away a working voice.
        """
        from jarvis.tts import cloud

        library = _library(tmp_path)
        stored = library.add(name="我", pcm=_pcm(), sample_rate=16_000, prompt_text="你好")
        monkeypatch.setattr(cloud, "enrol", _Enrolment(fallback="noise_too_high"))

        message = voice_cloud.upload_voice(library, stored.voice_id, _pcm(), 16_000, "你好")

        assert "noise_too_high" in message
        assert library.voices()[0].cloud_voice == "xyvoiceSelf01", "a usable voice is linked"


class TestForget:
    """Deleting the vendor half, and never failing loudly about it."""

    def test_it_deletes_the_vendor_voice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.tts import cloud

        deleter = _Deleter()
        monkeypatch.setattr(cloud, "delete", deleter)

        voice_cloud.forget_cloud_voice("xyvoiceSelf01")

        assert deleter.deleted == ["xyvoiceSelf01"]

    def test_a_failing_delete_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """By the time this runs the local state is already correct. An orphan
        on the vendor's side is not worth turning a successful delete into an
        error message the user cannot act on."""
        from jarvis.tts import cloud

        def boom(voice: str, **kwargs: Any) -> bool:
            raise cloud.CloudVoiceError("network down")

        monkeypatch.setattr(cloud, "delete", boom)

        voice_cloud.forget_cloud_voice("xyvoiceSelf01")  # must not raise
