"""A voice made from a recording: where it lives, what it looks like, how it is used.

This is the store behind "录一段我的声音，让她用这个声音说话". One cloned voice is
two things kept together: the **reference clip** (what she imitates) and the
**transcript of that clip** (what the clip says). Both are needed -- a zero-shot
engine is handed the reference audio *and* its text, so that it can tell the
speaker's timbre apart from the words being spoken. Storing only the audio would
mean the transcript has to be re-guessed on every synthesis.

Three decisions worth stating, because each one is a thing that goes wrong later:

* **Files, not preferences.** The reference is a 16 kHz WAV of a few hundred
  kilobytes. ``preferences.json`` is a flat key/value store that gets read on
  every utterance; putting a WAV in it would mean parsing a megabyte of base64
  to answer "which voice is selected". So the audio lives in a directory of its
  own and only a small index is read on the hot path.
* **A real WAV, not raw PCM.** CosyVoice loads the reference from a path via
  torchaudio, and a WAV is the one format that both torchaudio and the operator's
  media player agree on. When a cloned voice sounds wrong, being able to
  double-click the reference and hear what she was actually given is the whole
  diagnosis.
* **Bounded.** A reference clip is copied into GPU memory on every synthesis.
  Two minutes of audio would make every sentence slow and is not what a zero-shot
  prompt wants anyway; the window here is 2--30 seconds, with the sweet spot
  (3--15 s) called out to the caller rather than silently enforced.

The store owns no engine and loads no model: it is a directory plus an index, so
it can be tested without torch anywhere in sight.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("jarvis.app.voice_library")

CLONE_PREFIX = "clone:"
"""Cloned voice ids are namespaced so they can sit in the same list as the
built-in ones without a second registry to keep in sync. The built-in ids are
bare engine voice names (``zh-CN-XiaoxiaoNeural``); nothing else starts with
``clone:``, so one string answers "which kind is this"."""

MIN_MS = 2_000
"""Below this there is not enough of a speaker left to imitate."""

MAX_MS = 30_000
"""Above this the reference starts costing more per sentence than it buys."""

COMFORT_MS = (3_000, 15_000)
"""Where zero-shot cloning actually works well. Reported as advice, not enforced:
a 20-second recording is not an error, it is just slower and no better."""

MAX_VOICES = 20
"""A phone-side picker with more than this stops being a picker. Also caps the
disk this feature can quietly consume."""

MAX_NAME = 24
MAX_PROMPT = 200

_INDEX_VERSION = 1
_SAFE_ID = re.compile(r"^clone:[0-9a-f]{12}$")


def _as_int(value: object, fallback: int) -> int:
    """Read a number out of an untyped index entry.

    The index is JSON: anything in it is ``object`` as far as the type checker is
    concerned, and the file may have been hand-edited. A value that is not a
    number means "use the fallback" rather than an exception, because one bad
    entry must not take the whole list down with it.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True, slots=True)
class ClonedVoice:
    """One recorded voice, as the rest of the app sees it."""

    voice_id: str
    """``clone:<12 hex>`` -- the id everything else passes around."""

    name: str
    """What the operator called it, e.g. ``我自己``."""

    prompt_text: str
    """What the reference clip says, verbatim."""

    sample_rate: int
    """Rate of the reference audio. Always 16 kHz in practice; carried so a
    mismatch is visible instead of being silently resampled wrong."""

    duration_ms: int
    created_at: str
    ref_path: Path
    """Absolute path to the WAV. Not serialised -- it is derived from the id."""

    cloud_voice: str = ""
    """The vendor's id for this voice once the sample has been uploaded.

    Empty means "local only", which is a perfectly good state: the offline engine
    clones from ``ref_path`` without anybody else being involved. When it is set,
    synthesis *prefers* the cloud: the offline path needs 16 seconds to produce
    its first chunk on a 4 GB GPU, and a phone call cannot wait that long. See
    :mod:`jarvis.tts.cloud` for the whole argument.
    """

    cloud_model: str = ""
    """The synthesis model ``cloud_voice`` was bound to on the vendor's side.

    Travels with the voice rather than being assumed from a constant: the vendor
    binds a voice to the model it was enrolled for, and synthesising with any
    other one fails. A stale constant in two places is how that gets discovered
    in production.
    """

    def to_public(self) -> dict[str, object]:
        """The shape that crosses the wire to the phone.

        ``cloud`` is reported because it changes what the user will *hear*: a
        cloud voice answers in a few hundred milliseconds, a local one takes
        most of a minute on this class of machine. The phone uses it to set the
        wait, rather than showing "thinking…" for either.
        """
        return {
            "id": self.voice_id,
            "name": self.name,
            "prompt_text": self.prompt_text,
            "duration_ms": self.duration_ms,
            "created_at": self.created_at,
            "engine": "cosyvoice",
            "kind": "clone",
            "cloud": bool(self.cloud_voice),
            "cloud_model": self.cloud_model,
        }


class VoiceLibraryError(Exception):
    """A recording that cannot be accepted, with a sentence the phone can show."""


class VoiceLibrary:
    """The cloned voices on this machine. One directory, one index, no model."""

    name = "voice_library"

    def __init__(self, root: Path) -> None:
        self._root = root
        self._index_path = root / "index.json"

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Create the directory. Never fails on an already-present one.

        A store that cannot be created is not fatal at startup: the assistant
        works without cloned voices, and refusing to boot over an unwritable
        directory would be a much worse failure than a picker with one fewer
        section in it.
        """
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("could not create the voice directory %s: %s", self._root, exc)

    def stop(self) -> None:
        return None

    # -- reading -----------------------------------------------------------

    def voices(self) -> list[ClonedVoice]:
        """Every cloned voice, newest first. Unreadable entries are skipped.

        A half-written index entry (or a reference file deleted by hand) drops
        out of the list rather than raising: the picker is a read path, and a
        read path that can 500 is a picker that stops opening.

        Named ``voices`` and not ``list``: a method by that name shadows the
        builtin for every annotation written inside this class body, which turns
        an ordinary ``-> list[...]`` signature into a type error two methods
        later. The name also matches what :class:`~jarvis.app.voice_picker.
        VoicePicker` calls the same question.
        """
        raw = self._read_index()
        found: list[ClonedVoice] = []
        for entry in raw:
            voice = self._from_entry(entry)
            if voice is not None:
                found.append(voice)
        return found

    def resolve(self, voice_id: str) -> ClonedVoice | None:
        """Look one up by id. ``None`` for anything that is not a cloned voice."""
        if not _SAFE_ID.match(voice_id):
            return None
        for voice in self.voices():
            if voice.voice_id == voice_id:
                return voice
        return None

    def is_clone(self, voice_id: str) -> bool:
        """Whether an id names a cloned voice, without touching the disk.

        Used on the hot path (``VoicePicker`` asks on every listing) so that the
        common case -- a built-in voice -- costs one ``startswith``.
        """
        return voice_id.startswith(CLONE_PREFIX)

    # -- writing -----------------------------------------------------------

    def add(
        self,
        *,
        name: object,
        pcm: bytes,
        sample_rate: int,
        prompt_text: object,
        cloud_voice: str = "",
        cloud_model: str = "",
    ) -> ClonedVoice:
        """Accept one recording and store it.

        Args:
            name: What to call it. Trimmed; must be 1--:data:`MAX_NAME` chars.
            pcm: Raw **s16le mono** samples, exactly as the phone captured them.
            sample_rate: Rate of ``pcm``. 16 kHz is what the pipeline wants; other
                rates are refused rather than resampled, because a wrong guess
                here produces a voice that sounds like a stranger and no error.
            prompt_text: What the clip says.
            cloud_voice: The vendor's id, when the sample has already been
                uploaded. Passed *in* rather than done here: this class is a
                directory and an index, and a method that opened an HTTPS
                connection would make every test of it need a network.
            cloud_model: The synthesis model that id is bound to.

        Raises:
            VoiceLibraryError: With a message written for the person holding the
                phone -- it is shown verbatim.
        """
        clean_name = self._clean_name(name)
        clean_prompt = self._clean_prompt(prompt_text)
        duration_ms = self._check_audio(pcm, sample_rate)
        if len(self.voices()) >= MAX_VOICES:
            raise VoiceLibraryError(f"最多存 {MAX_VOICES} 个自定义音色，先删一个再录")

        voice_id = CLONE_PREFIX + secrets.token_hex(6)
        ref_path = self._ref_path(voice_id)
        try:
            self._write_wav(ref_path, pcm, sample_rate)
        except OSError as exc:
            raise VoiceLibraryError(f"写不进录音文件：{exc}") from exc

        voice = ClonedVoice(
            voice_id=voice_id,
            name=clean_name,
            prompt_text=clean_prompt,
            sample_rate=sample_rate,
            duration_ms=duration_ms,
            created_at=datetime.now().isoformat(timespec="seconds"),
            ref_path=ref_path,
            cloud_voice=str(cloud_voice or ""),
            cloud_model=str(cloud_model or ""),
        )
        entries = self._read_index()
        entries.append(
            {
                "id": voice.voice_id,
                "name": voice.name,
                "prompt_text": voice.prompt_text,
                "sample_rate": voice.sample_rate,
                "duration_ms": voice.duration_ms,
                "created_at": voice.created_at,
                "cloud_voice": voice.cloud_voice,
                "cloud_model": voice.cloud_model,
            }
        )
        try:
            self._write_index(entries)
        except OSError as exc:
            # The audio is on disk but unindexed: drop it rather than leave a
            # file nothing points at, then report the failure.
            ref_path.unlink(missing_ok=True)
            raise VoiceLibraryError(f"写不进音色清单：{exc}") from exc
        logger.info("stored cloned voice %s (%s, %d ms)", voice.voice_id, voice.name, duration_ms)
        return voice

    def remove(self, voice_id: str) -> bool:
        """Forget one. ``False`` when it was not there to begin with.

        The index is written *before* the audio is deleted: if the process dies
        between the two, what is left is an orphaned WAV (harmless, and reusable
        if the id is ever restored) instead of an index entry pointing at a
        missing file (which makes the picker fail on every open).

        The return is the *entry* rather than a bare ``True`` because a voice
        that was uploaded carries a vendor-side id, and somebody has to delete
        that too. Handing the entry back is what lets the caller do it without
        reading the index a second time -- and a second read is a second chance
        to race with another delete.
        """
        entries = self._read_index()
        kept = [entry for entry in entries if entry.get("id") != voice_id]
        if len(kept) == len(entries):
            return False
        self._write_index(kept)
        self._ref_path(voice_id).unlink(missing_ok=True)
        logger.info("removed cloned voice %s", voice_id)
        return True

    def cloud_id(self, voice_id: str) -> str:
        """The vendor-side id for a voice, or ``""`` if it was never uploaded.

        Read straight from the index rather than from :meth:`resolve` so that a
        voice whose *audio* has gone missing can still have its cloud half
        deleted. Requiring a healthy local file before being allowed to clean up
        after somebody asked to be forgotten would be the wrong order.
        """
        for entry in self._read_index():
            if entry.get("id") == voice_id:
                found = entry.get("cloud_voice")
                return found if isinstance(found, str) else ""
        return ""

    def unlink_cloud(self, voice_id: str) -> None:
        """Remember that a voice is local-only again.

        Called after the vendor-side delete (or a deliberate decision not to
        upload), so the next synthesis does not try to use an id that is gone.
        """
        entries = self._read_index()
        changed = False
        for entry in entries:
            if entry.get("id") == voice_id and entry.get("cloud_voice"):
                entry["cloud_voice"] = ""
                entry["cloud_model"] = ""
                changed = True
        if changed:
            self._write_index(entries)

    def link_cloud(self, voice_id: str, *, cloud_voice: str, cloud_model: str) -> bool:
        """Record that a voice now also exists on the vendor's side.

        The mirror of :meth:`unlink_cloud`, and separate from :meth:`add` for the
        same reason: the upload happens in the caller, and a store whose ``add``
        opened an HTTPS connection would need a network for every one of its
        tests. A recording whose upload failed is stored locally and *stays*
        that way -- a half-linked voice would make the picker promise a fast
        answer it cannot deliver.

        ``False`` when the id is not in the index (deleted between the upload
        and this call), which is a real race: the phone can delete a voice while
        its sample is still in flight.
        """
        if not cloud_voice:
            return False
        entries = self._read_index()
        changed = False
        for entry in entries:
            if entry.get("id") == voice_id:
                entry["cloud_voice"] = cloud_voice
                entry["cloud_model"] = cloud_model
                changed = True
        if changed:
            self._write_index(entries)
            logger.info("linked cloned voice %s to cloud voice %s", voice_id, cloud_voice)
        return changed

    # -- validation --------------------------------------------------------

    @staticmethod
    def _clean_name(name: object) -> str:
        text = str(name or "").strip()
        if not text:
            raise VoiceLibraryError("给这个音色起个名字")
        if len(text) > MAX_NAME:
            raise VoiceLibraryError(f"名字最多 {MAX_NAME} 个字")
        return text

    @staticmethod
    def _clean_prompt(prompt_text: object) -> str:
        text = str(prompt_text or "").strip()
        if not text:
            raise VoiceLibraryError("还要填上你刚才念的那句话，她才能把音色和内容分开")
        if len(text) > MAX_PROMPT:
            raise VoiceLibraryError(f"这句话太长了，最多 {MAX_PROMPT} 个字")
        return text

    @staticmethod
    def _check_audio(pcm: bytes, sample_rate: int) -> int:
        """Return the duration in ms, or raise with something worth reading."""
        if sample_rate != 16_000:
            raise VoiceLibraryError(f"录音必须是 16kHz，收到的是 {sample_rate}Hz")
        if len(pcm) % 2:
            raise VoiceLibraryError("录音数据不完整（半个采样），重录一次")
        if not pcm:
            raise VoiceLibraryError("没收到录音")
        duration_ms = int(len(pcm) / 2 * 1000 / sample_rate)
        if duration_ms < MIN_MS:
            raise VoiceLibraryError(
                f"太短了（{duration_ms / 1000:.1f} 秒），至少要说 {MIN_MS // 1000} 秒"
            )
        if duration_ms > MAX_MS:
            raise VoiceLibraryError(
                f"太长了（{duration_ms / 1000:.1f} 秒），最多 {MAX_MS // 1000} 秒"
            )
        return duration_ms

    # -- disk --------------------------------------------------------------

    def _ref_path(self, voice_id: str) -> Path:
        return self._root / f"{voice_id.removeprefix(CLONE_PREFIX)}.wav"

    def _read_index(self) -> list[dict[str, object]]:
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            logger.warning("voice index unreadable (%s); treating it as empty", exc)
            return []
        if not isinstance(raw, dict):
            return []
        entries = raw.get("voices")
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    def _write_index(self, entries: list[dict[str, object]]) -> None:
        """Write through a temporary file and rename.

        A picker that reads a half-flushed index sees no voices at all, and the
        operator's conclusion is that the recording was lost. ``os.replace`` is
        atomic on Windows and POSIX alike, so a reader sees either the old index
        or the new one and never a torn one.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {"version": _INDEX_VERSION, "voices": entries}
        scratch = self._index_path.with_suffix(".json.tmp")
        scratch.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(scratch, self._index_path)

    @staticmethod
    def _write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(pcm)

    def _from_entry(self, entry: dict[str, object]) -> ClonedVoice | None:
        voice_id = str(entry.get("id") or "")
        if not _SAFE_ID.match(voice_id):
            return None
        cloud_voice = entry.get("cloud_voice")
        cloud_model = entry.get("cloud_model")
        return ClonedVoice(
            voice_id=voice_id,
            name=str(entry.get("name") or "未命名"),
            prompt_text=str(entry.get("prompt_text") or ""),
            sample_rate=_as_int(entry.get("sample_rate"), 16_000),
            duration_ms=_as_int(entry.get("duration_ms"), 0),
            created_at=str(entry.get("created_at") or ""),
            ref_path=self._ref_path(voice_id),
            cloud_voice=cloud_voice if isinstance(cloud_voice, str) else "",
            cloud_model=cloud_model if isinstance(cloud_model, str) else "",
        )


def resolves_to_cloud(voice: ClonedVoice | None) -> bool:
    """Whether a cloned voice will be spoken by the vendor rather than locally.

    One function because two callers must agree: the picker builds an engine for
    a preview and the call path builds one for a reply, and if they decided
    differently then 试听 would sound like one engine while the answer sounded
    like another -- the single most confusing way for this feature to be wrong.
    """
    return voice is not None and bool(voice.cloud_voice)


__all__ = [
    "CLONE_PREFIX",
    "COMFORT_MS",
    "MAX_VOICES",
    "ClonedVoice",
    "VoiceLibrary",
    "VoiceLibraryError",
    "resolves_to_cloud",
]
