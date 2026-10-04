"""Realtime synthesis of a cloud-cloned voice, over one WebSocket.

This is the other half of :mod:`jarvis.tts.cloud`: that module *creates* a voice
(upload a sample, get an id), this one *speaks* with it. Splitting them is not
tidiness -- they have different lifetimes. Enrolment happens once per recording
and is a slow HTTP POST; synthesis happens once per sentence and must start
making sound in under a second.

Why a WebSocket instead of the one-shot HTTP endpoint
-----------------------------------------------------
The non-streaming path answers with a whole audio file, which means the phone
waits for the entire sentence before the first syllable. Measured against the
offline engine, that is the difference between "像豆包一样打电话" and a
walkie-talkie. The realtime endpoint emits ``response.audio.delta`` events
carrying base64 PCM as the sentence is generated, so playback starts a few
hundred milliseconds in, and the first chunk is what the caller actually hears.

Four implementation decisions worth keeping:

* **Sessions are pooled, and ``close()`` does not disconnect.** The caller
  (:class:`~jarvis.app.voice_call.VoiceCall`) builds a fresh engine per sentence
  and closes it afterwards -- a sensible contract that the offline engine
  answers by *releasing*, not by killing (see :mod:`jarvis.tts.sidecar`). If
  ``close()`` here tore the socket down, every sentence would pay a TLS
  handshake and a session handshake again, doubling the delay this module exists
  to remove. So the pool lives at module level, :meth:`QwenRealtimeVoice.close`
  releases, and :func:`shutdown_voice_sessions` is what actually ends them (also
  registered with ``atexit``, so a process that forgets still cleans up).
* **Pool keys carry a digest of the key, never the key.** The API key can change
  while the process runs, and a session opened with the old one must not be
  reused -- but putting the secret in a dictionary key is how secrets end up in
  a crash dump or a repr. A short SHA-256 prefix answers "same credential?" and
  reveals nothing.
* **The socket has two different timeouts.** The handshake must fail fast -- a
  black-holed connect must not hold a phone call for the OS default of a minute
  -- while the stream must tolerate silence, because the service sends nothing
  between an utterance's last delta and ``response.done``. One timeout for both
  is how a healthy reply gets truncated.
* **The last chunk is decided with one chunk of lookahead.** ``is_final`` already
  exists on :class:`~jarvis.tts.types.AudioChunk`, and only the ``response.done``
  event proves a chunk was last -- which arrives *after* it. Emitting an empty
  trailing chunk instead would make every consumer handle a zero-length blob.

A known limitation, stated rather than hidden: ``speed`` and ``volume`` are
ignored. The session handshake is the one place they could go, and an
unrecognised field there can fail the whole session -- so a slider that does
nothing beats a call that goes silent. The local and Edge engines still honour
both.
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import json
import logging
import threading
from typing import TYPE_CHECKING, Final
from urllib.parse import quote

from jarvis.core.exceptions import TtsError
from jarvis.tts.cloud import REALTIME_HOST, SYNTH_MODEL, resolve_key
from jarvis.tts.types import FORMAT_PCM_S16LE, AudioChunk
from jarvis.tts.ws import WebSocket, WebSocketError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

    from jarvis.tts.types import ShouldStop

logger = logging.getLogger("jarvis.tts.cloud_voices")

CONNECT_TIMEOUT: Final[float] = 10.0
"""Seconds allowed for the TCP + TLS + WebSocket handshake.

Short on purpose: this runs while somebody is holding a phone to their ear, and
"nothing happened for a minute, then it failed" is worse than failing at once.
"""

STREAM_TIMEOUT: Final[float] = 20.0
"""Seconds of silence tolerated mid-utterance.

Not a deadline for the whole reply -- a slow sentence legitimately takes longer
than this from the first delta to ``response.done`` -- but a bound on how long
the reader blocks with *nothing* arriving, so a dropped connection surfaces as
an error instead of a frozen call.
"""

MAX_CHARS: Final[int] = 2_000
"""Refused above this. A long reply is the caller's job to split (see
``VoiceCall.MAX_SPOKEN_CHARS``); sending a wall of text here would produce an
audio blob nobody wants to hold in memory twice."""

SAMPLE_RATE: Final[int] = 24_000
"""What the session asks for and what the deltas are. Matches the other engines,
so the playback layer keeps needing exactly one rate."""

MAX_SESSIONS: Final[int] = 4
"""How many idle sessions to keep. A phone call uses one. The picker can preview
several voices in a row, and each is its own session -- but past a handful the
pool is holding sockets nobody will come back to."""

_POOL: dict[tuple[str, str, str], QwenRealtimeVoice] = {}
"""Idle sessions, keyed by ``(voice, model, key digest)``. Module level because
the per-sentence engine the caller builds cannot own the connection it wants to
reuse."""

_POOL_LOCK = threading.Lock()


class QwenRealtimeVoice:
    """A cloud-cloned voice, spoken over the DashScope realtime WebSocket.

    Construct with :meth:`acquire` for the pooled behaviour the call path wants,
    or directly when a private connection is genuinely wanted.
    """

    def __init__(
        self,
        *,
        voice: str,
        model: str = SYNTH_MODEL,
        api_key: str | None = None,
        language: str = "zh",
    ) -> None:
        """Create the engine. Does not connect yet.

        Args mirror the enrolment side: ``voice`` is the id ``enrol()`` returned,
        ``model`` must be the ``target_model`` it was bound to (synthesising a
        voice with a model it was not enrolled for fails), and ``api_key`` is
        read per call so a key typed into the settings panel takes effect on the
        next sentence rather than the next restart.
        """
        if not voice.strip():
            raise TtsError("云端音色没有 id，说不了话")
        self._voice = voice.strip()
        self._model = model or SYNTH_MODEL
        self._api_key = api_key
        self._language = language or "zh"
        self._ws: WebSocket | None = None
        self._pooled = False
        """Whether this idle engine is currently sitting in :data:`_POOL`.

        Needed because :meth:`close` deliberately keeps ``self._ws`` alive --
        see below -- so "already pooled" cannot be inferred from the socket
        being absent. Without it a double ``close()`` would file the same engine
        twice and evict an unrelated session to make room.
        """

    # -- identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        return "qwen-tts-realtime"

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE

    @property
    def voice(self) -> str:
        """The enrolled voice id, so a caller can log which one spoke."""
        return self._voice

    # -- the pool ----------------------------------------------------------

    @classmethod
    def acquire(
        cls,
        *,
        voice: str,
        model: str = SYNTH_MODEL,
        api_key: str | None = None,
        language: str = "zh",
    ) -> QwenRealtimeVoice:
        """A session for this voice, reusing an idle one when there is one.

        Answers with an engine whose ``close()`` returns it to the pool. Fails
        the same way the constructor does, and for the same reason: an id-less
        voice has nothing to connect *to*, and finding that out at connect time
        would waste a handshake.
        """
        if not voice.strip():
            raise TtsError("云端音色没有 id，说不了话")
        # Resolved here rather than in the constructor: this is the entry point
        # the synthesizer path uses, so a missing key must be reported before a
        # session is created -- and the digest needs the resolved value anyway.
        key = resolve_key(api_key)
        slot = (voice.strip(), model or SYNTH_MODEL, _digest(key))
        with _POOL_LOCK:
            pooled = _POOL.pop(slot, None)
        if pooled is not None:
            logger.debug("reusing a pooled realtime session for %s", voice)
            # Out of the pool and in a caller's hands again: without this the
            # next ``close()`` would see ``_pooled`` still set and refuse to
            # file it, so the session would never be reused twice.
            pooled._pooled = False
            return pooled
        return cls(voice=voice, model=model, api_key=key, language=language)

    @property
    def _slot(self) -> tuple[str, str, str]:
        """Where this session goes back in the pool."""
        return (self._voice, self._model, _digest(self._resolved_key()))

    def _resolved_key(self) -> str:
        """The key this session was opened with, or the current one if unused.

        An unused engine has never resolved anything, and asking now is what
        makes a pooled-but-rekeyed session land in a *different* slot instead of
        being handed back as if nothing changed.
        """
        return resolve_key(self._api_key)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Return the session to the pool. Idempotent, and never raises.

        Deliberately **not** a disconnect: see the module docstring. Called from
        a ``finally`` by the caller, so an exception here would replace the real
        synthesis error with a much less useful one -- hence the pool insertion
        is guarded too.

        The socket is **kept** on ``self``. Clearing it here is the tempting
        version and it is a bug: :meth:`disconnect` is what closes a pooled
        session, and it reads the socket off ``self`` -- so a ``close`` that
        dropped the reference would hand the pool an engine that can no longer
        be shut down, leaking one live socket per sentence until the vendor's
        own idle timeout reaped them. ``_pooled`` is what records "in the pool".
        """
        if self._pooled or self._ws is None:
            return
        if self._ws.closed:
            # The peer already hung up. Do not pool it: that relocates the
            # failure to whatever sentence picks it up next.
            self._ws = None
            return
        try:
            slot = self._slot
        except Exception:  # pragma: no cover - a key removed mid-call
            # The key was deleted while the session was alive: there is no slot
            # to file it under, and holding a live socket for a credential that
            # no longer exists is the one case where dropping it is right.
            logger.info("no key to pool a realtime session under; disconnecting")
            self.disconnect()
            return
        self._pooled = True
        with _POOL_LOCK:
            _evict_locked()
            _POOL[slot] = self
        logger.debug("realtime session for %s returned to the pool", self._voice)

    def disconnect(self) -> None:
        """Actually end the session. Called by :func:`shutdown_voice_sessions`.

        Sends ``session.finish`` first so the service can stop billing the
        session immediately instead of waiting for the socket to time out.
        Failures are swallowed: this runs at process exit, where there is
        nothing left to report to.

        Also un-pools the engine, and that is not bookkeeping -- an engine that
        is evicted from the pool stays reachable from nowhere, so leaving
        ``_pooled`` set would be harmless, but ``disconnect`` is *also* called
        directly (a voice switch, a broken socket) on engines a caller still
        holds, and those must become closeable again.
        """
        self._pooled = False
        ws, self._ws = self._ws, None
        if ws is None:
            return
        try:
            if not ws.closed:
                ws.send(json.dumps({"type": "session.finish"}))
        except Exception:  # pragma: no cover - the peer is already gone
            logger.debug("session.finish could not be sent", exc_info=True)
        try:
            ws.close()
        except Exception:  # pragma: no cover - closing a socket that is gone
            logger.debug("closing the realtime socket failed", exc_info=True)

    # -- synthesis ---------------------------------------------------------

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        should_stop: ShouldStop | None = None,
    ) -> Iterator[AudioChunk]:
        """Stream ``text`` as PCM chunks read off the wire.

        ``speed`` and ``volume`` are ignored -- see the module docstring for why
        that is a deliberate trade rather than an oversight.

        Raises:
            TtsError: For a missing key, a refused session, a vendor error event,
                or a socket that died. Never a bare ``WebSocketError``: the
                caller here is an RPC handler whose only job is to show the
                message on a phone.
        """
        del speed, volume  # see the module docstring
        spoken = text.strip()
        if not spoken:
            return
        if len(spoken) > MAX_CHARS:
            raise TtsError(f"这一句太长了（{len(spoken)} 字），最多 {MAX_CHARS} 字")

        chosen = (voice or self._voice).strip() or self._voice
        pending: bytes | None = None
        for audio in self._stream(spoken, chosen, should_stop):
            # One chunk of lookahead: the *last* delta can only be recognised
            # once the next event proves there is no more audio coming.
            if pending is not None:
                yield self._chunk(pending, is_final=False)
            pending = audio
        if pending is not None:
            # A plain statement after the loop, deliberately not a ``finally``:
            # a ``yield`` inside ``finally`` turns a generator that is being
            # closed early into ``RuntimeError: generator ignored
            # GeneratorExit``, replacing a real error with a confusing one. The
            # Barge-In path still reaches here, because ``_drain`` *returns*
            # when asked to stop rather than raising.
            yield self._chunk(pending, is_final=True)

    @staticmethod
    def _chunk(pcm: bytes, *, is_final: bool) -> AudioChunk:
        return AudioChunk(
            audio=pcm,
            sample_rate=SAMPLE_RATE,
            is_final=is_final,
            format=FORMAT_PCM_S16LE,
        )

    def _stream(self, text: str, voice: str, should_stop: ShouldStop | None) -> Iterator[bytes]:
        """Yield decoded PCM per delta, retrying once if the pooled socket died.

        The retry exists because a reused connection can be closed by the peer
        while the phone was idle -- and "we spoke nothing yet" is exactly the
        situation where trying again is invisible and correct. It cannot
        duplicate speech: the retry only happens before the first delta.
        """
        spoken_anything = False
        for attempt in (0, 1):
            ws = self._connection(voice)
            self._send_turn(ws, text)
            try:
                for audio in self._drain(ws, should_stop):
                    spoken_anything = True
                    yield audio
            except (WebSocketError, OSError) as exc:
                self._drop()
                if attempt == 0 and not spoken_anything:
                    logger.warning("realtime socket died before any audio (%s); reconnecting", exc)
                    continue
                raise TtsError(f"云端音色连接断了：{exc}") from exc
            return

    def _drain(self, ws: WebSocket, should_stop: ShouldStop | None) -> Iterator[bytes]:
        """Read events until ``response.done``, yielding PCM for each delta."""
        while True:
            if should_stop is not None and should_stop():
                # Stop pulling audio rather than draining to the end: the caller
                # was interrupted, and the remaining generation is pure cost.
                # Leaving the turn half-read is safe -- the next sentence begins
                # with a fresh append + commit, and commits are not queued.
                logger.debug("barge-in: abandoning the rest of this utterance")
                return
            message = ws.recv()
            if message is None:
                raise WebSocketError("云端音色服务关闭了连接")
            event = self._parse(message)
            kind = event.get("type")
            if kind == "response.audio.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    yield base64.b64decode(delta)
            elif kind == "response.done":
                return
            elif kind == "error":
                raise TtsError(f"云端音色报错：{self._error_text(event)}")
            # Everything else -- response.created, content_part.added,
            # output_item.done, session.updated -- is lifecycle bookkeeping the
            # streaming path does not need. Ignored rather than logged: at one
            # event per sentence it would be pure noise.

    @staticmethod
    def _parse(message: str) -> dict[str, object]:
        try:
            parsed = json.loads(message)
        except ValueError as exc:
            raise TtsError(f"云端音色发来了看不懂的消息：{message[:200]}") from exc
        if not isinstance(parsed, dict):
            raise TtsError(f"云端音色发来了意外的结构：{message[:200]}")
        return parsed

    @staticmethod
    def _error_text(event: dict[str, object]) -> str:
        """The vendor's own words, which are the only part that names the cause."""
        error = event.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code")
            if isinstance(message, str) and message:
                return message
        return json.dumps(event, ensure_ascii=False)[:300]

    # -- connection --------------------------------------------------------

    def _connection(self, voice: str) -> WebSocket:
        """The socket that is currently speaking as ``voice``.

        A voice switch mid-call is answered by reconnecting rather than by a
        mid-session ``session.update``: the session's voice is what the whole
        connection is *for*, and a second handshake is one round trip that
        happens only when somebody actually changes voice.
        """
        if voice != self._voice:
            logger.info("realtime voice switched to %s; reopening the session", voice)
            self._drop()
            self._voice = voice
        ws = self._ws
        if ws is not None and not ws.closed:
            return ws
        self._drop()
        ws = self._open()
        self._ws = ws
        return ws

    def _drop(self) -> None:
        """Throw away a socket without pooling it. Used when it has failed."""
        self._pooled = False
        ws, self._ws = self._ws, None
        if ws is None:
            return
        try:
            ws.close()
        except Exception:  # pragma: no cover - already broken by definition
            logger.debug("closing a broken socket failed", exc_info=True)

    def _open(self) -> WebSocket:
        """Connect and complete the session handshake.

        The key is resolved here, per call, and it is the *first* thing done: a
        missing key must produce its own sentence rather than surfacing as a
        401 from the vendor, which is the same wall of text for a typo and for
        an empty field.
        """
        key = resolve_key(self._api_key)
        url = f"wss://{REALTIME_HOST}/api-ws/v1/realtime?model={quote(self._model, safe='')}"
        try:
            ws = WebSocket.connect(
                url,
                headers=[("Authorization", f"Bearer {key}")],
                timeout=CONNECT_TIMEOUT,
            )
        except WebSocketError as exc:
            raise TtsError(f"连不上云端音色服务：{exc}") from exc
        try:
            ws.send(json.dumps(self._session_update(), ensure_ascii=False))
            self._await_session(ws)
            # Handshake done; the stream phase gets the generous timeout.
            ws.set_timeout(STREAM_TIMEOUT)
        except (WebSocketError, OSError) as exc:
            try:
                ws.close()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.debug("closing a half-open socket failed", exc_info=True)
            raise TtsError(f"云端音色会话没能建立：{exc}") from exc
        logger.debug(
            "realtime session ready (voice=%s, model=%s, %d Hz)",
            self._voice,
            self._model,
            SAMPLE_RATE,
        )
        return ws

    def _session_update(self) -> dict[str, object]:
        """The handshake that says *which* voice and *what format* to stream.

        ``mode: commit`` rather than ``server_commit``: the server-side
        auto-commit decides for itself when a buffer is complete, which makes
        the first chunk's timing depend on text length heuristics. Here the
        caller always sends one whole sentence and commits it explicitly, so the
        deterministic mode is the one that matches how this is driven.
        """
        return {
            "type": "session.update",
            "session": {
                "voice": self._voice,
                "mode": "commit",
                "language_type": self._language,
                "response_format": "pcm",
                "sample_rate": SAMPLE_RATE,
            },
        }

    @staticmethod
    def _await_session(ws: WebSocket) -> None:
        """Wait for ``session.updated`` before sending any text.

        Sending an append first is the tempting shortcut and a race: the service
        may still be applying the previous session's voice, in which case the
        first sentence comes back in the wrong voice -- intermittently, which is
        the worst way for a bug to present.
        """
        while True:
            message = ws.recv()
            if message is None:
                raise WebSocketError("云端音色服务在会话建立前就断开了")
            event = QwenRealtimeVoice._parse(message)
            kind = event.get("type")
            if kind == "session.updated":
                return
            if kind == "error":
                raise WebSocketError(QwenRealtimeVoice._error_text(event))

    @staticmethod
    def _send_turn(ws: WebSocket, text: str) -> None:
        """Append one sentence and commit it. The service answers with audio.

        Always *one* sentence per commit: in ``mode: commit`` the service starts
        generating on the commit, so a buffer holding two sentences would make
        the first chunk's arrival depend on the second sentence's length -- the
        exact latency this module exists to remove.
        """
        ws.send(json.dumps({"type": "input_text_buffer.append", "text": text}, ensure_ascii=False))
        ws.send(json.dumps({"type": "input_text_buffer.commit"}))


def _digest(key: str) -> str:
    """A short fingerprint of a credential, for use in a pool key.

    Deliberately not the key itself and not reversible: the pool only ever has
    to answer "is this the same credential the session was opened with", and a
    dictionary key is a thing that ends up in tracebacks and debuggers.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _evict_locked() -> None:
    """Trim the pool to :data:`MAX_SESSIONS`. Caller holds :data:`_POOL_LOCK`.

    Arbitrary rather than LRU: entries are sessions nobody is using, and an
    evicted one costs one handshake to get back. Tracking recency would add
    bookkeeping to save nothing.
    """
    while len(_POOL) >= MAX_SESSIONS:
        slot, engine = _POOL.popitem()
        logger.debug("evicting an idle realtime session for %s", slot[0])
        engine.disconnect()


def shutdown_voice_sessions() -> int:
    """Close every pooled session. Returns how many were closed.

    Called by :func:`shutdown` at exit and by anything that needs the sockets
    gone now -- the count is returned rather than logged so a test can assert
    that a pool which should be empty is.
    """
    with _POOL_LOCK:
        engines = list(_POOL.values())
        _POOL.clear()
    for engine in engines:
        engine.disconnect()
    if engines:
        logger.info("closed %d idle cloud voice session(s)", len(engines))
    return len(engines)


atexit.register(shutdown_voice_sessions)


__all__ = [
    "CONNECT_TIMEOUT",
    "MAX_CHARS",
    "MAX_SESSIONS",
    "SAMPLE_RATE",
    "STREAM_TIMEOUT",
    "QwenRealtimeVoice",
    "shutdown_voice_sessions",
]
