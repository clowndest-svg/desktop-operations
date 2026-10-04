"""Tests for the cloud voice path: enrolment, the WebSocket frame layer, and
the realtime engine.

None of these touch the network. Enrolment takes its POST through an injected
``opener``; the realtime engine takes its socket through a stand-in that speaks
the same event vocabulary on an in-memory pair of queues. That is deliberate --
the bugs worth catching here are not "did HTTP work" but:

* the **frame codec** -- a client that forgets to mask is disconnected by a
  compliant server, and an unmasked *server* frame silently produces garbage
  rather than an error (:mod:`jarvis.tts.ws`);
* the **pool** -- ``close()`` must return a session rather than tear down the
  TLS connection, or every sentence pays a handshake the module exists to avoid;
* the **lookahead** -- ``is_final`` can only be put on the last chunk once the
  event *after* it has been read, and getting that wrong emits an empty trailing
  blob that every consumer has to special-case.

The realtime service is also the one place where a wrong field in the session
handshake fails the *whole* session, so the payload shape is asserted literally
rather than by round-tripping it through the code that produced it.
"""

from __future__ import annotations

import base64
import json
import socket
from typing import Any, cast

import pytest

from jarvis.core.exceptions import TtsError
from jarvis.tts import cloud
from jarvis.tts import cloud_voices as cloud_voices_module
from jarvis.tts import ws as ws_module
from jarvis.tts.cloud import CloudVoiceError, enrol, resolve_key
from jarvis.tts.cloud_voices import QwenRealtimeVoice, shutdown_voice_sessions
from jarvis.tts.ws import WebSocket, WebSocketError, accept_key, build_frame, read_frame

# ---------------------------------------------------------------------------
# enrolment (jarvis/tts/cloud.py)
# ---------------------------------------------------------------------------


class _Recorder:
    """Stands in for ``_post``: captures the payload, returns a canned answer."""

    def __init__(self, answer: dict[str, Any] | None = None) -> None:
        # ``is None`` rather than ``or``: an empty answer is a value this test
        # family needs (it is the "no voice id" case), and ``or`` would quietly
        # substitute the default and make that test pass for the wrong reason.
        self.answer = (
            answer
            if answer is not None
            else {"output": {"voice": "xyvoiceSelf01", "target_model": "m"}}
        )
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, url: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, key, payload))
        return self.answer


def _pcm(samples: int = 16_000) -> bytes:
    """A second of 16 kHz s16le silence, which is all ``enrol`` inspects."""
    return b"\x00\x00" * samples


class TestEnrolment:
    def test_resolve_key_prefers_the_argument_then_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "from-env")
        assert resolve_key("explicit") == "explicit"
        assert resolve_key(None) == "from-env"

    def test_a_missing_key_names_the_environment_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Not configured" has to be actionable; a bare KeyError is not."""
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        with pytest.raises(CloudVoiceError, match="DASHSCOPE_API_KEY"):
            resolve_key(None)

    def test_enrol_posts_a_base64_data_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The Qwen path exists *because* it accepts the sample inline; if the
        sample stopped being inline, the reason for choosing this endpoint would
        be gone."""
        recorder = _Recorder()
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        voice = enrol(
            pcm=_pcm(),
            sample_rate=16_000,
            transcript="你好",
            name="my voice",
            opener=recorder,
        )
        url, key, payload = recorder.calls[0]
        assert url == cloud.ENROLL_URL
        assert key == "k"
        assert payload["model"] == cloud.ENROLL_MODEL
        inline = payload["input"]["audio"]["data"]
        assert inline.startswith("data:audio/wav;base64,")
        # Round-trips to a real RIFF/WAVE container, not just any bytes.
        decoded = base64.b64decode(inline.split(",", 1)[1])
        assert decoded[:4] == b"RIFF" and decoded[8:12] == b"WAVE"
        assert voice.voice == "xyvoiceSelf01"
        assert voice.target_model == "m"

    def test_a_wrong_sample_rate_is_refused_rather_than_resampled(self) -> None:
        """A silent resample produces a voice that sounds like a stranger with
        nothing in the logs to explain it, so the mismatch is an error."""
        with pytest.raises(CloudVoiceError, match="16kHz"):
            enrol(pcm=_pcm(), sample_rate=24_000, transcript="你好", name="x", opener=_Recorder())

    def test_an_empty_sample_is_refused(self) -> None:
        with pytest.raises(CloudVoiceError, match="没收到录音"):
            enrol(pcm=b"", sample_rate=16_000, transcript="你好", name="x", opener=_Recorder())

    def test_a_missing_transcript_is_refused(self) -> None:
        with pytest.raises(CloudVoiceError, match="念的那句话"):
            enrol(pcm=_pcm(), sample_rate=16_000, transcript="  ", name="x", opener=_Recorder())

    def test_the_name_is_reduced_to_what_the_vendor_accepts(self) -> None:
        """A rejected enrolment because somebody typed a Chinese label would be
        an absurd way to lose a recording -- so the label is sanitised, never the
        request refused."""
        recorder = _Recorder()
        enrol(
            pcm=_pcm(),
            sample_rate=16_000,
            transcript="你好",
            name="小夜 voice 0.3",
            opener=recorder,
        )
        assert recorder.calls[0][2]["input"]["preferred_name"] == "voice03"

    def test_a_name_with_nothing_usable_falls_back_to_the_prefix(self) -> None:
        recorder = _Recorder()
        enrol(pcm=_pcm(), sample_rate=16_000, transcript="你好", name="小夜", opener=recorder)
        assert recorder.calls[0][2]["input"]["preferred_name"] == cloud.VOICE_PREFIX

    def test_a_fallback_answer_is_surfaced_not_swallowed(self) -> None:
        """``fallback_reason`` means the clone will sound unlike the person;
        the operator's next move is only obvious if they are told."""
        answer = {
            "output": {"voice": "v1", "target_model": "m", "fallback_reason": "audio too short"}
        }
        voice = enrol(
            pcm=_pcm(), sample_rate=16_000, transcript="你好", name="x", opener=_Recorder(answer)
        )
        assert voice.fallback_reason == "audio too short"

    def test_an_answer_without_a_voice_id_is_an_error(self) -> None:
        with pytest.raises(CloudVoiceError, match="没有返回音色信息"):
            enrol(
                pcm=_pcm(),
                sample_rate=16_000,
                transcript="你好",
                name="x",
                opener=_Recorder({}),
            )

    def test_delete_reports_that_the_voice_was_already_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deleting a recording must not leave the clone alive on the vendor's
        servers -- but a voice that is already absent is a success, not a
        failure."""

        def boom(url: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
            raise CloudVoiceError("gone")

        monkeypatch.setattr(cloud, "_post", boom)
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        assert cloud.delete("v1") is False


class TestEnrolmentHTTPErrors:
    def test_the_vendors_message_is_quoted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The generic HTTP reason ("Bad Request") says nothing; the body carries
        the only text that tells an operator what to change."""

        def boom(request: object, timeout: float = 0) -> object:
            raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
                url=cloud.ENROLL_URL,
                code=400,
                msg="Bad Request",
                hdrs=None,
                fp=_Body(json.dumps({"code": "InvalidApiKey", "message": "key 不正确"}).encode()),
            )

        monkeypatch.setattr("urllib.request.urlopen", boom)
        monkeypatch.setenv("DASHSCOPE_API_KEY", "bad")
        with pytest.raises(CloudVoiceError, match="key 不正确"):
            enrol(pcm=_pcm(), sample_rate=16_000, transcript="你好", name="x")


class _Body:
    """Minimal read()-able body for an HTTPError."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        """Present so ``tempfile``'s finaliser does not warn about a missing
        ``close`` when the error object is collected."""
        return None


# ---------------------------------------------------------------------------
# the frame layer (jarvis/tts/ws.py)
# ---------------------------------------------------------------------------


class _Pipe:
    """A socket stand-in good enough for ``read_frame``: ``recv`` from a buffer."""

    def __init__(self, data: bytes = b"") -> None:
        self._buffer = bytearray(data)

    def recv(self, count: int) -> bytes:
        if not self._buffer:
            return b""
        taken = bytes(self._buffer[:count])
        del self._buffer[:count]
        return taken

    def sendall(self, data: bytes) -> None:  # pragma: no cover - not read here
        raise AssertionError("the frame reader must not write")

    def settimeout(self, seconds: float | None) -> None:  # pragma: no cover
        return None

    def fileno(self) -> int:
        return 0

    def close(self) -> None:  # pragma: no cover
        return None


def _sock(pipe: _Pipe) -> socket.socket:
    """``_Pipe`` wearing the type ``read_frame`` declares.

    The functions under test only call ``recv``, and a real socket cannot be
    driven byte-by-byte from a literal without a server; the cast keeps mypy
    honest about everything *else* in this file.
    """
    return cast("socket.socket", pipe)


def _server_frame(payload: bytes, opcode: int = 0x1, *, fin: bool = True) -> bytes:
    """A **server** frame: never masked. Written by hand, not with
    ``build_frame``, so the test cannot agree with a bug in the encoder."""
    header = bytearray()
    header.append((0x80 if fin else 0x00) | opcode)
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 0x10000:
        header.append(126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(127)
        header.extend(length.to_bytes(8, "big"))
    return bytes(header) + payload


class TestFrames:
    def test_client_frames_are_masked(self) -> None:
        """RFC 6455 requires it, and the failure mode is a bare close that reads
        as "the service dropped me"."""
        frame = build_frame(b"hello")
        assert frame[0] == 0x81
        assert frame[1] & 0x80, "the mask bit must be set on a client frame"
        # Unmasking with the frame's own key gets the payload back.
        key = frame[2:6]
        masked = frame[6:]
        assert bytes(b ^ key[i % 4] for i, b in enumerate(masked)) == b"hello"

    def test_a_long_payload_uses_the_16_bit_length(self) -> None:
        frame = build_frame(b"x" * 200)
        assert frame[1] & 0x7F == 126
        assert int.from_bytes(frame[2:4], "big") == 200

    def test_a_very_long_payload_uses_the_64_bit_length(self) -> None:
        frame = build_frame(b"x" * 70_000)
        assert frame[1] & 0x7F == 127
        assert int.from_bytes(frame[2:10], "big") == 70_000

    def test_reading_a_server_frame(self) -> None:
        opcode, fin, payload = read_frame(_sock(_Pipe(_server_frame(b"hi"))))
        assert (opcode, fin, payload) == (0x1, True, b"hi")

    def test_read_exactly_survives_short_reads(self) -> None:
        """``recv`` is allowed to return fewer bytes than asked for; a reader
        that trusts the first call loses the tail of every audio delta."""

        class _Dribble(_Pipe):
            def recv(self, count: int) -> bytes:
                return super().recv(1)

        opcode, _fin, payload = read_frame(_sock(_Dribble(_server_frame(b"abcdef"))))
        assert opcode == 0x1
        assert payload == b"abcdef"

    def test_a_masked_server_frame_is_unmasked_not_mistaken_for_plain(self) -> None:
        """A peer that sets the mask bit would otherwise have its payload read as
        plain data and produce plausible-looking garbage."""
        key = b"\x01\x02\x03\x04"
        payload = b"masked!"
        masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        frame = bytes([0x81, 0x80 | len(payload)]) + key + masked
        _opcode, _fin, decoded = read_frame(_sock(_Pipe(frame)))
        assert decoded == payload

    def test_accept_key_is_the_rfc_example(self) -> None:
        """RFC 6455 §1.3's own vector; a wrong constant makes every handshake
        fail against a correct server."""
        assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="

    def test_recv_reassembles_continuation_frames(self) -> None:
        """An audio delta routinely spans frames; returning the first frame only
        hands the caller broken JSON."""
        stream = _server_frame(b'{"type":', opcode=0x1, fin=False) + _server_frame(
            b'"response.done"}', opcode=0x0, fin=True
        )
        ws = WebSocket(_sock(_Pipe(stream)))
        assert ws.recv() == '{"type":"response.done"}'

    def test_recv_returns_none_on_close(self) -> None:
        ws = WebSocket(_sock(_Pipe(_server_frame(b"", opcode=0x8))))
        assert ws.recv() is None

    def test_recv_answers_a_ping_with_a_pong(self) -> None:
        """An unanswered ping is a disconnection after the peer's timeout."""
        sent: list[bytes] = []

        class _PingThenData(_Pipe):
            def sendall(self, data: bytes) -> None:
                sent.append(data)

        stream = _server_frame(b"alive", opcode=0x9) + _server_frame(b"hello")
        ws = WebSocket(_sock(_PingThenData(stream)))
        assert ws.recv() == "hello"
        assert sent, "a ping must be answered"
        assert sent[0][0] & 0x0F == 0xA, "the reply must be a pong, not a text frame"

    def test_a_frame_larger_than_the_ceiling_is_refused(self) -> None:
        """A peer that never sends a final frame would otherwise grow the buffer
        until the process dies."""
        header = bytes([0x81, 0x7F]) + (ws_module.MAX_MESSAGE_BYTES + 1).to_bytes(8, "big")
        with pytest.raises(WebSocketError, match="超过上限"):
            read_frame(_sock(_Pipe(header)))


# ---------------------------------------------------------------------------
# the realtime engine (jarvis/tts/cloud_voices.py)
# ---------------------------------------------------------------------------


class _FakeSocket:
    """A ``WebSocket`` stand-in driven by a scripted list of server events.

    ``deltas`` are the raw PCM the engine should yield; ``script`` is the event
    vocabulary around them, so a test can place ``response.done`` exactly where
    it matters.
    """

    def __init__(self, script: list[str | None]) -> None:
        self._script = list(script)
        self.sent: list[str] = []
        self.closed = False
        self.timeout: float | None = None

    # -- WebSocket surface --------------------------------------------------

    def send(self, message: str) -> None:
        self.sent.append(message)

    def recv(self) -> str | None:
        if not self._script:
            return None
        return self._script.pop(0)

    def set_timeout(self, seconds: float | None) -> None:
        self.timeout = seconds

    def close(self) -> None:
        self.closed = True

    # -- test helpers -------------------------------------------------------

    def sent_types(self) -> list[str]:
        return [json.loads(item).get("type") for item in self.sent]

    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(item) for item in self.sent]


def _delta(pcm: bytes) -> str:
    return json.dumps({"type": "response.audio.delta", "delta": base64.b64encode(pcm).decode()})


def _session_updated() -> str:
    return json.dumps({"type": "session.updated"})


@pytest.fixture(autouse=True)
def _cloud_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key for every test, so the key check does not mask the check under
    test. The test that *is* about a missing key deletes it itself."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def _clean_pool() -> Any:
    """Every test starts and ends with an empty pool.

    Module-level state that leaks between tests turns one failure into a cascade
    where the next test reuses a session the previous one poisoned.
    """
    shutdown_voice_sessions()
    yield
    shutdown_voice_sessions()


def _engine(
    monkeypatch: pytest.MonkeyPatch, script: list[str | None]
) -> tuple[QwenRealtimeVoice, _FakeSocket]:
    """An engine whose transport is ``script``.

    ``WebSocket.connect`` is replaced rather than ``_open`` so that the *real*
    handshake runs -- sending ``session.update`` and waiting for
    ``session.updated``. Patching ``_open`` would skip exactly the code these
    tests exist to check, and would let a broken handshake pass.
    """
    sock = _FakeSocket(script)
    monkeypatch.setattr(ws_module.WebSocket, "connect", classmethod(lambda cls, *a, **k: sock))
    return QwenRealtimeVoice(voice="xyvoiceSelf01"), sock


class TestRealtimeHandshake:
    def test_the_session_update_names_the_voice_and_the_format(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asserted literally: an unrecognised field here fails the *whole*
        session, so this payload is the contract, not an implementation detail."""
        engine, sock = _engine(monkeypatch, [_session_updated()])
        engine._connection(engine.voice)
        update = sock.payloads()[0]
        assert update["type"] == "session.update"
        session = update["session"]
        assert session["voice"] == "xyvoiceSelf01"
        assert session["response_format"] == "pcm"
        assert session["sample_rate"] == 24_000
        assert session["mode"] == "commit"

    def test_no_text_is_sent_before_session_updated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sending an append first is the tempting shortcut and a race: the
        first sentence can come back in the *previous* session's voice,
        intermittently, which is the worst way for a bug to present."""
        engine, sock = _engine(monkeypatch, [_session_updated()])
        engine._connection(engine.voice)
        assert sock.sent_types() == ["session.update"]

    def test_a_session_error_becomes_a_tts_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine, _sock = _engine(
            monkeypatch,
            [json.dumps({"type": "error", "error": {"code": "BadVoice", "message": "音色不存在"}})],
        )
        with pytest.raises(TtsError, match="音色不存在"):
            engine._connection(engine.voice)


class TestRealtimeSynthesis:
    def test_a_turn_appends_one_sentence_then_commits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine, sock = _engine(
            monkeypatch,
            [
                _session_updated(),
                _delta(b"\x01\x02"),
                json.dumps({"type": "response.done"}),
            ],
        )
        list(engine.synthesize("你好"))
        assert "input_text_buffer.append" in sock.sent_types()
        assert "input_text_buffer.commit" in sock.sent_types()

    def test_the_last_chunk_is_marked_final_and_the_others_are_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``is_final`` needs one chunk of lookahead: the last delta is only
        knowable once the *next* event proves there is no more audio."""
        engine, _sock = _engine(
            monkeypatch,
            [
                _session_updated(),
                _delta(b"\x01\x02"),
                _delta(b"\x03\x04"),
                _delta(b"\x05\x06"),
                json.dumps({"type": "response.done"}),
            ],
        )
        chunks = list(engine.synthesize("你好"))
        assert [c.audio for c in chunks] == [b"\x01\x02", b"\x03\x04", b"\x05\x06"]
        assert [c.is_final for c in chunks] == [False, False, True]
        assert all(c.sample_rate == 24_000 for c in chunks)

    def test_a_single_delta_is_yielded_as_the_final_chunk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine, _sock = _engine(
            monkeypatch,
            [_session_updated(), _delta(b"\xaa"), json.dumps({"type": "response.done"})],
        )
        chunks = list(engine.synthesize("好"))
        assert len(chunks) == 1
        assert chunks[0].is_final is True

    def test_empty_text_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine, sock = _engine(monkeypatch, [_session_updated()])
        assert list(engine.synthesize("   ")) == []
        assert sock.sent == []

    def test_a_very_long_line_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine, _sock = _engine(monkeypatch, [_session_updated()])
        with pytest.raises(TtsError, match="太长"):
            list(engine.synthesize("字" * (cloud_voices_module.MAX_CHARS + 1)))

    def test_a_barge_in_stops_pulling_audio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The caller was interrupted; the remaining generation is pure cost.
        Stopping must yield the audio already received, with the last marked
        final, rather than raising."""
        engine, sock = _engine(
            monkeypatch,
            [
                _session_updated(),
                _delta(b"\x01"),
                _delta(b"\x02"),
                _delta(b"\x03"),
                json.dumps({"type": "response.done"}),
            ],
        )
        stop_after = _StopAfter(1)
        chunks = list(engine.synthesize("你好", should_stop=stop_after))
        assert [c.audio for c in chunks] == [b"\x01"]
        assert chunks[-1].is_final is True
        # The unread deltas are still in the script: proof the drain stopped.
        remaining = sock._script[0]
        assert remaining is not None
        assert json.loads(remaining)["type"] == "response.audio.delta"

    def test_a_vendor_error_event_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine, _sock = _engine(
            monkeypatch,
            [
                _session_updated(),
                json.dumps({"type": "error", "error": {"message": "配额用完"}}),
            ],
        )
        with pytest.raises(TtsError, match="配额用完"):
            list(engine.synthesize("你好"))


class _StopAfter:
    """A ``should_stop`` that returns True from the Nth call onwards."""

    def __init__(self, allowed: int) -> None:
        self._allowed = allowed
        self._calls = 0

    def __call__(self) -> bool:
        self._calls += 1
        return self._calls > self._allowed


class TestRealtimePool:
    def test_close_returns_the_session_to_the_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The caller builds an engine per sentence and closes it. If ``close``
        tore the socket down, every sentence would pay a TLS handshake -- the
        exact delay this module exists to remove."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        sock = _FakeSocket([_session_updated()])
        monkeypatch.setattr(QwenRealtimeVoice, "_open", lambda self: sock)
        first = QwenRealtimeVoice.acquire(voice="xyvoiceSelf01")
        first._connection(first.voice)
        first.close()
        assert sock.closed is False, "close() must not disconnect"
        second = QwenRealtimeVoice.acquire(voice="xyvoiceSelf01")
        assert second is first, "the pooled session should have been reused"

    def test_a_different_key_does_not_reuse_the_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session opened with an old credential must not be handed back after
        the key changed."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "first")
        sock = _FakeSocket([_session_updated()])
        monkeypatch.setattr(QwenRealtimeVoice, "_open", lambda self: sock)
        first = QwenRealtimeVoice.acquire(voice="v")
        first._connection(first.voice)
        first.close()
        monkeypatch.setenv("DASHSCOPE_API_KEY", "second")
        second = QwenRealtimeVoice.acquire(voice="v")
        assert second is not first

    def test_the_pool_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Past a handful, the pool holds sockets nobody will come back to."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        opened: list[_FakeSocket] = []

        def fake_open(self: QwenRealtimeVoice) -> _FakeSocket:
            sock = _FakeSocket([_session_updated()])
            opened.append(sock)
            return sock

        monkeypatch.setattr(QwenRealtimeVoice, "_open", fake_open)
        for index in range(cloud_voices_module.MAX_SESSIONS + 2):
            engine = QwenRealtimeVoice.acquire(voice=f"v{index}")
            engine._connection(engine.voice)
            engine.close()
        assert len(cloud_voices_module._POOL) <= cloud_voices_module.MAX_SESSIONS

    def test_shutdown_closes_every_pooled_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        socks: list[_FakeSocket] = []

        def fake_open(self: QwenRealtimeVoice) -> _FakeSocket:
            sock = _FakeSocket([_session_updated()])
            socks.append(sock)
            return sock

        monkeypatch.setattr(QwenRealtimeVoice, "_open", fake_open)
        engine = QwenRealtimeVoice.acquire(voice="v")
        engine._connection(engine.voice)
        engine.close()
        assert shutdown_voice_sessions() == 1
        assert socks[0].closed is True
        assert shutdown_voice_sessions() == 0

    def test_a_dead_socket_is_not_pooled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pooling a socket the peer already closed just relocates the failure
        to the next sentence."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        sock = _FakeSocket([_session_updated()])
        sock.closed = True
        monkeypatch.setattr(QwenRealtimeVoice, "_open", lambda self: sock)
        engine = QwenRealtimeVoice.acquire(voice="v")
        engine._ws = cast("WebSocket", sock)
        engine.close()
        assert shutdown_voice_sessions() == 0


class TestRealtimeReconnect:
    def test_a_socket_that_died_before_any_audio_is_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pooled connection can be closed by the peer while the phone was
        idle -- and "we spoke nothing yet" is exactly when retrying is invisible
        and correct."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        first = _FakeSocket([_session_updated(), None])  # None == peer closed
        second = _FakeSocket(
            [_session_updated(), _delta(b"\x01"), json.dumps({"type": "response.done"})]
        )
        opens = [first, second]
        monkeypatch.setattr(QwenRealtimeVoice, "_open", lambda self: opens.pop(0))
        engine = QwenRealtimeVoice(voice="v")
        chunks = list(engine.synthesize("你好"))
        assert [c.audio for c in chunks] == [b"\x01"]

    def test_a_socket_that_died_after_audio_is_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retrying after audio was already played would speak the sentence
        twice."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        sock = _FakeSocket([_session_updated(), _delta(b"\x01"), None])
        monkeypatch.setattr(QwenRealtimeVoice, "_open", lambda self: sock)
        engine = QwenRealtimeVoice(voice="v")
        with pytest.raises(TtsError, match="连接断了"):
            list(engine.synthesize("你好"))


class TestRealtimeIdentity:
    def test_a_voiceless_engine_is_refused_before_connecting(self) -> None:
        with pytest.raises(TtsError, match="没有 id"):
            QwenRealtimeVoice(voice="   ")

    def test_the_reported_name_and_rate_are_the_ones_the_call_path_expects(self) -> None:
        engine = QwenRealtimeVoice(voice="v", api_key="k")
        assert engine.name == "qwen-tts-realtime"
        assert engine.sample_rate == 24_000
        assert engine.voice == "v"

    def test_speed_and_volume_are_accepted_and_ignored(self) -> None:
        """The session handshake is the one place they could go, and an
        unrecognised field there can fail the whole session -- so a slider that
        does nothing beats a call that goes silent."""
        engine = QwenRealtimeVoice(voice="v", api_key="k")
        assert list(engine.synthesize("", speed=2.0, volume=0.1)) == []
