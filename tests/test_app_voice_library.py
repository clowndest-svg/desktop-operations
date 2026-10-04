"""The recorded-voice store: a directory, an index, and no model anywhere.

Everything here runs without torch, without CosyVoice and without a GPU, and
that is the point: storing someone's recording and *speaking with it* are two
different jobs with two different failure modes, and if the store needed the
engine then a machine that cannot synthesise could not even keep the recording
the user just made.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis.app.voice_library import (
    CLONE_PREFIX,
    MAX_MS,
    MAX_NAME,
    MAX_PROMPT,
    MAX_VOICES,
    ClonedVoice,
    VoiceLibrary,
    VoiceLibraryError,
)


def _library(tmp_path: Path) -> VoiceLibrary:
    library = VoiceLibrary(tmp_path / "voices")
    library.start()
    return library


def _pcm(seconds: float, sample_rate: int = 16_000) -> bytes:
    """Silence of the right length. Content does not matter to the store."""
    return b"\x00\x00" * int(sample_rate * seconds)


def _add(
    library: VoiceLibrary, *, seconds: float = 5.0, name: str = "我自己", prompt: str = "你好呀"
) -> ClonedVoice:
    return library.add(name=name, pcm=_pcm(seconds), sample_rate=16_000, prompt_text=prompt)


# -- what a recording has to be ------------------------------------------------


def test_a_good_recording_is_stored_and_listed(tmp_path: Path) -> None:
    library = _library(tmp_path)
    voice = _add(library)

    assert voice.voice_id.startswith(CLONE_PREFIX)
    assert voice.name == "我自己"
    assert voice.duration_ms == 5_000
    assert voice.ref_path.is_file(), "the reference WAV must exist on disk"
    assert [entry.voice_id for entry in library.voices()] == [voice.voice_id]


def test_the_reference_is_a_real_wav_a_player_can_open(tmp_path: Path) -> None:
    """Not raw PCM in a .wav extension.

    The whole diagnosis of "the clone sounds wrong" is double-clicking the
    reference and hearing what she was actually given; a file that only this
    program can read removes exactly that.
    """
    import wave

    library = _library(tmp_path)
    voice = _add(library, seconds=3.0)

    with wave.open(str(voice.ref_path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16_000
        assert handle.getnframes() == 3 * 16_000


@pytest.mark.parametrize("seconds", [0.5, 1.9])
def test_a_recording_that_is_too_short_is_refused(tmp_path: Path, seconds: float) -> None:
    library = _library(tmp_path)
    with pytest.raises(VoiceLibraryError, match="太短"):
        _add(library, seconds=seconds)
    assert library.voices() == [], "a refused recording must not be indexed"


def test_a_recording_that_is_too_long_is_refused(tmp_path: Path) -> None:
    library = _library(tmp_path)
    with pytest.raises(VoiceLibraryError, match="太长"):
        _add(library, seconds=MAX_MS / 1000 + 5)
    assert library.voices() == []


def test_a_mistaken_sample_rate_is_refused_rather_than_resampled(tmp_path: Path) -> None:
    """A wrong resample is a voice that sounds like a stranger, with no error.

    Refusing is the only outcome that can be explained to the person holding the
    phone.
    """
    library = _library(tmp_path)
    with pytest.raises(VoiceLibraryError, match="16kHz"):
        library.add(name="x", pcm=_pcm(5, 24_000), sample_rate=24_000, prompt_text="你好")
    assert library.voices() == []


def test_half_a_sample_is_refused(tmp_path: Path) -> None:
    library = _library(tmp_path)
    with pytest.raises(VoiceLibraryError, match="不完整"):
        library.add(name="x", pcm=b"\x00" * 100_001, sample_rate=16_000, prompt_text="你好")


def test_silence_is_not_a_valid_recording(tmp_path: Path) -> None:
    library = _library(tmp_path)
    with pytest.raises(VoiceLibraryError, match="没收到录音"):
        library.add(name="x", pcm=b"", sample_rate=16_000, prompt_text="你好")


# -- name and transcript -------------------------------------------------------


def test_a_missing_name_is_refused(tmp_path: Path) -> None:
    library = _library(tmp_path)
    with pytest.raises(VoiceLibraryError, match="起个名字"):
        _add(library, name="   ")


def test_a_name_that_is_too_long_is_refused(tmp_path: Path) -> None:
    library = _library(tmp_path)
    with pytest.raises(VoiceLibraryError, match=f"最多 {MAX_NAME}"):
        _add(library, name="名" * (MAX_NAME + 1))


def test_a_missing_transcript_is_refused(tmp_path: Path) -> None:
    """The transcript is not decoration.

    A zero-shot engine is handed the reference audio *and its text*; without the
    text it cannot separate "how this person sounds" from "what this person
    said", and it answers by speaking the reference sentence instead.
    """
    library = _library(tmp_path)
    with pytest.raises(VoiceLibraryError, match="刚才念的那句话"):
        _add(library, prompt="")


def test_a_transcript_that_is_too_long_is_refused(tmp_path: Path) -> None:
    library = _library(tmp_path)
    with pytest.raises(VoiceLibraryError, match="太长"):
        _add(library, prompt="啊" * (MAX_PROMPT + 1))


def test_the_transcript_survives_a_round_trip(tmp_path: Path) -> None:
    library = _library(tmp_path)
    _add(library, prompt="今天天气真好，我想出去走走")

    reopened = VoiceLibrary(tmp_path / "voices")
    assert reopened.voices()[0].prompt_text == "今天天气真好，我想出去走走"


# -- bounds --------------------------------------------------------------------


def test_the_number_of_voices_is_capped(tmp_path: Path) -> None:
    library = _library(tmp_path)
    for index in range(MAX_VOICES):
        _add(library, name=f"音色{index}")
    with pytest.raises(VoiceLibraryError, match=f"最多存 {MAX_VOICES}"):
        _add(library, name="多出来的")


def test_the_cap_counts_voices_not_files(tmp_path: Path) -> None:
    """A removal must actually free a slot, or the cap turns into a dead end."""
    library = _library(tmp_path)
    for index in range(MAX_VOICES):
        _add(library, name=f"音色{index}")
    assert library.remove(library.voices()[0].voice_id) is True
    _add(library, name="替补")
    assert len(library.voices()) == MAX_VOICES


# -- resolving -----------------------------------------------------------------


def test_is_clone_is_decided_by_the_name_alone(tmp_path: Path) -> None:
    """Asked on the hot path, so it must not touch the disk.

    ``VoicePicker`` calls this on every listing; a version that read the index
    would turn opening the voice panel into file I/O.
    """
    library = _library(tmp_path)
    _add(library)
    assert library.is_clone(f"{CLONE_PREFIX}abc123def456") is True
    assert library.is_clone("zh-CN-XiaoxiaoNeural") is False


def test_resolve_finds_a_stored_voice(tmp_path: Path) -> None:
    library = _library(tmp_path)
    voice = _add(library)
    found = library.resolve(voice.voice_id)
    assert found is not None
    assert found.ref_path == voice.ref_path


def test_resolve_refuses_an_id_that_could_escape_the_directory(tmp_path: Path) -> None:
    """The id is interpolated into a filename, so it is shape-checked, not trusted."""
    library = _library(tmp_path)
    for hostile in (
        "clone:../../etc/passwd",
        "clone:ABC",
        "clone:",
        "../index",
        "clone:zzzzzzzzzzzz",
    ):
        assert library.resolve(hostile) is None, hostile


def test_resolve_returns_none_for_a_builtin_voice(tmp_path: Path) -> None:
    library = _library(tmp_path)
    assert library.resolve("zh-CN-XiaoxiaoNeural") is None


# -- removal -------------------------------------------------------------------


def test_removing_deletes_both_the_index_entry_and_the_audio(tmp_path: Path) -> None:
    library = _library(tmp_path)
    voice = _add(library)
    assert library.remove(voice.voice_id) is True
    assert library.voices() == []
    assert not voice.ref_path.exists()


def test_removing_something_absent_says_so(tmp_path: Path) -> None:
    library = _library(tmp_path)
    assert library.remove(f"{CLONE_PREFIX}000000000000") is False


# -- reading a damaged store ---------------------------------------------------


def test_a_missing_index_reads_as_empty(tmp_path: Path) -> None:
    library = VoiceLibrary(tmp_path / "voices")
    assert library.voices() == []


def test_a_corrupt_index_reads_as_empty_rather_than_raising(tmp_path: Path) -> None:
    """The picker is a read path, and a read path that can 500 stops opening."""
    library = _library(tmp_path)
    (tmp_path / "voices" / "index.json").write_text("{ this is not json", encoding="utf-8")
    assert library.voices() == []


def test_an_entry_with_a_hostile_id_is_skipped(tmp_path: Path) -> None:
    library = _library(tmp_path)
    (tmp_path / "voices" / "index.json").write_text(
        json.dumps({"version": 1, "voices": [{"id": "clone:../../boom", "name": "x"}]}),
        encoding="utf-8",
    )
    assert library.voices() == []


def test_an_entry_with_unparsable_numbers_still_lists(tmp_path: Path) -> None:
    """One hand-edited field must not take the whole list down with it."""
    library = _library(tmp_path)
    (tmp_path / "voices" / "index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "voices": [
                    {
                        "id": f"{CLONE_PREFIX}abcdef123456",
                        "name": "手改的",
                        "prompt_text": "你好",
                        "sample_rate": True,
                        "duration_ms": "not a number",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    voices = library.voices()
    assert len(voices) == 1
    assert voices[0].sample_rate == 16_000, "a bool is not a sample rate"
    assert voices[0].duration_ms == 0


def test_a_voice_whose_audio_was_deleted_by_hand_is_still_listed(tmp_path: Path) -> None:
    """Deliberate: hiding it would read as "your recording is gone".

    The failure belongs on the moment she tries to speak it, where the engine
    can say which file is missing, not in a list that silently gets shorter.
    """
    library = _library(tmp_path)
    voice = _add(library)
    voice.ref_path.unlink()
    assert [entry.voice_id for entry in library.voices()] == [voice.voice_id]


# -- the wire shape ------------------------------------------------------------


def test_to_public_carries_no_paths(tmp_path: Path) -> None:
    """The public shape crosses to the phone; an absolute path is this machine's
    business, and it is also derived from the id anyway."""
    library = _library(tmp_path)
    voice = _add(library)
    public = voice.to_public()

    assert str(tmp_path) not in json.dumps(public)
    assert public["kind"] == "clone"
    assert public["engine"] == "cosyvoice"


def test_start_is_idempotent(tmp_path: Path) -> None:
    library = VoiceLibrary(tmp_path / "voices")
    library.start()
    library.start()
    assert isinstance(library.voices(), list)


def test_an_unwritable_root_does_not_raise_at_start(tmp_path: Path) -> None:
    """The assistant works without cloned voices.

    Refusing to boot over a directory would be a far worse failure than a picker
    with one fewer section in it.
    """
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("I am a file", encoding="utf-8")
    library = VoiceLibrary(blocked / "voices")
    library.start()
    assert library.voices() == []


def test_cloned_voice_is_frozen(tmp_path: Path) -> None:
    import dataclasses

    library = _library(tmp_path)
    voice: ClonedVoice = _add(library)
    with pytest.raises(dataclasses.FrozenInstanceError):
        voice.name = "changed"  # type: ignore[misc]
