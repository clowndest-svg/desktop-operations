"""Cloud voice cloning on Alibaba DashScope (百炼) — the real-time path.

Why this exists
---------------
The offline engine clones a voice beautifully and cannot hold a conversation on
a 4 GB GPU: measured on the target machine, the first chunk of a *three
character* reply took 16 seconds, and a twenty character sentence took 19 with
43 second gaps between chunks. "像豆包一样打电话" is not a throughput problem
that tuning fixes; it needs a service that synthesises faster than real time,
and that service is somebody else's GPU.

Two capabilities are involved and they are separate endpoints on DashScope:

* **enrolment** — upload a sample, get back a voice id (``voice``). Done once
  per recording, not per sentence.
* **realtime synthesis** — a WebSocket that streams PCM as it is generated, so
  the first audible chunk arrives in a few hundred milliseconds instead of
  after the whole sentence.

Choices worth stating, because each one is a thing that would otherwise have to
be guessed at later:

* **The Qwen-TTS enrolment path, not the CosyVoice one.** The CosyVoice
  enrolment endpoint (``model: voice-enrollment``) takes a **publicly
  reachable URL** for the reference audio; the Qwen path
  (``model: qwen-voice-enrollment``) accepts the audio as a **base64 data URL**.
  A phone recording is a private file on a LAN with no public address, so the
  CosyVoice path would require uploading it to third-party storage and making it
  world-readable. Sending it directly to the vendor is both simpler and the
  smaller disclosure, and the corresponding synthesis model is literally named
  ``*-realtime``.
* **stdlib only.** The project does not add dependencies to talk to a REST
  endpoint, and this client is one POST. The WebSocket side needs a protocol
  implementation, which is in :mod:`jarvis.tts.cloud_voices`.
* **The key is read per call.** A key typed into the settings panel has to work
  on the next sentence rather than the next restart, so nothing is captured at
  construction time.
"""

from __future__ import annotations

import base64
import json
import logging
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

logger = logging.getLogger("jarvis.tts.cloud")

ENROLL_URL: Final[str] = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
"""Beijing region. The Singapore equivalent is ``dashscope-intl.aliyuncs.com``."""

ENROLL_MODEL: Final[str] = "qwen-voice-enrollment"
"""Enrolment that accepts the sample inline, as opposed to a public URL."""

SYNTH_MODEL: Final[str] = "qwen3-tts-vc-realtime"
"""The realtime synthesis model the enrolled voice is bound to."""

REALTIME_HOST: Final[str] = "dashscope.aliyuncs.com"

VOICE_PREFIX: Final[str] = "xyvoice"
"""Voice-name prefix. Digits and letters only, at most 16 characters."""

VOICE_PREFIX_MAX: Final[int] = 16

MAX_SAMPLE_BYTES: Final[int] = 10 * 1024 * 1024
"""DashScope's ceiling for an inline sample. A 16 kHz mono WAV hits this at
about five minutes, far past the 30 s the recorder allows."""

TIMEOUT: Final[float] = 120.0
"""Enrolment only. Synthesis has its own, shorter, budget."""

DEFAULT_KEY_ENV: Final[str] = "DASHSCOPE_API_KEY"
"""Where the key is looked for when nothing says otherwise.

The name is configurable through ``tts.cloud_api_key_env`` so the settings
panel, which stores a key by looking up a variable name on the section that owns
it, has something to find. This constant is the fallback for a config written
before that field existed.
"""

AUDIO_MIME: Final[str] = "audio/wav"
"""What a recorded reference actually is. The vendor also accepts mpeg and mp4,
but claiming a format the bytes are not is how a working upload becomes an
unexplained rejection."""


class CloudVoiceError(Exception):
    """An enrolment that failed, with a sentence written for the operator."""


@dataclass(frozen=True, slots=True)
class EnrolledVoice:
    """A voice that now exists on the vendor's side."""

    voice: str
    """The id to pass as ``voice`` when synthesising."""

    target_model: str
    """The synthesis model it is bound to. Synthesising with a different model
    fails, so this travels with the voice rather than being assumed."""

    request_id: str = ""
    fallback_reason: str = ""
    """Non-empty when the vendor accepted the sample only in degraded mode.

    Surfaced rather than swallowed: it means the clone will sound unlike the
    person, and the operator's next move (re-record somewhere quieter) is only
    obvious if they are told.
    """


def resolve_key(api_key: str | None) -> str:
    """The key to use, from the argument or the environment.

    Read on every call: a key entered in the settings panel must take effect on
    the next sentence, not the next restart, and caching it in a module global
    would quietly pin whichever value was present at import.

    Public because synthesis needs the same key and must not re-derive it: two
    readers of one setting drift apart the moment one of them gains a fallback.

    The variable name is :data:`DEFAULT_KEY_ENV` and not read from
    ``tts.cloud_api_key_env``, deliberately. That field exists so the settings
    panel -- which stores a key by looking up a variable name on the section
    that owns it -- has something to write to; the panel and this module must
    therefore agree on one name, and a second lookup here is how they would stop
    agreeing. ``defaults.yaml`` documents the pairing.
    """
    import os

    value = (api_key or "").strip() or os.environ.get(DEFAULT_KEY_ENV, "").strip()
    if not value:
        raise CloudVoiceError(
            "没有配置阿里云百炼的 API Key，用不了云端音色。"
            f"在「设置」里填上，或设环境变量 {DEFAULT_KEY_ENV}。"
        )
    return value


def _post(url: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One JSON POST, with the failures translated into something readable."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        raise CloudVoiceError(f"云端音色服务拒绝了这次请求（HTTP {exc.code}）：{detail}") from exc
    except urllib.error.URLError as exc:
        raise CloudVoiceError(f"连不上云端音色服务：{exc.reason}") from exc
    except ssl.SSLError as exc:  # pragma: no cover - depends on the machine
        raise CloudVoiceError(f"和云端音色服务的加密连接建立失败：{exc}") from exc
    try:
        answer = json.loads(raw)
    except ValueError as exc:
        raise CloudVoiceError(f"云端音色服务返回的不是 JSON：{raw[:200]}") from exc
    if not isinstance(answer, dict):
        raise CloudVoiceError(f"云端音色服务返回了意外的结构：{raw[:200]}")
    return answer


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """Pull the vendor's own message out of an error body.

    The generic HTTP reason ("Bad Request") says nothing; the body carries
    ``{"code": ..., "message": ...}`` and that message is the only thing that
    tells an operator whether they typed the key wrong or the sample was too
    quiet.
    """
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except OSError:  # pragma: no cover - the body is already gone
        return exc.reason or "没有更多信息"
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw[:300] or (exc.reason or "没有更多信息")
    if isinstance(parsed, dict):
        message = parsed.get("message") or parsed.get("code")
        if isinstance(message, str) and message:
            return message
    return raw[:300]


def enrol(
    *,
    pcm: bytes,
    sample_rate: int,
    transcript: str,
    name: str,
    api_key: str | None = None,
    target_model: str = SYNTH_MODEL,
    timeout: float = TIMEOUT,
    opener: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None,
) -> EnrolledVoice:
    """Create a cloud voice from a recorded sample.

    Args:
        pcm: Raw **s16le mono** samples, exactly as the recorder captured them.
        sample_rate: Rate of ``pcm``. Anything but 16 kHz is refused rather than
            resampled: a silent resample here produces a voice that sounds like
            a stranger, with nothing to explain it.
        transcript: What the sample says. Improves the clone and is what lets the
            vendor check the audio really contains speech.
        name: A short label. Sanitised to the digits-and-letters the vendor
            accepts -- a rejected enrolment because somebody typed a Chinese
            name would be an absurd way to lose a recording.
        api_key: Explicit key; falls back to ``DASHSCOPE_API_KEY``.
        target_model: Synthesis model to bind to. Must match what is later used.
        timeout: Seconds for the upload.
        opener: Injection point for the tests. Defaults to the real POST.

    Raises:
        CloudVoiceError: With a message the phone can show verbatim.
    """
    if sample_rate != 16_000:
        raise CloudVoiceError(f"录音必须是 16kHz，云端音色收到的是 {sample_rate}Hz")
    if not pcm:
        raise CloudVoiceError("没收到录音")
    if len(pcm) > MAX_SAMPLE_BYTES:
        raise CloudVoiceError(f"录音太大了（{len(pcm) / 1024 / 1024:.1f}MB），重录短一点")
    if not transcript.strip():
        raise CloudVoiceError("还要填上你刚才念的那句话，云端才能把音色和内容分开")
    # The key is resolved *after* the sample is validated, deliberately: with it
    # first, a caller who forgot both the key and the transcript is told about
    # the key, fixes that, and is then told about the transcript. One round of
    # fixing rather than two, and the message is the more fundamental problem.
    key = resolve_key(api_key)

    import io
    import wave

    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)
        wav_bytes = buffer.getvalue()
    encoded = base64.b64encode(wav_bytes).decode("ascii")
    payload = {
        "model": ENROLL_MODEL,
        "input": {
            "action": "create",
            "target_model": target_model,
            "preferred_name": _safe_prefix(name),
            "audio": {"data": f"data:{AUDIO_MIME};base64,{encoded}"},
            "text": transcript.strip(),
            "language": "zh",
        },
    }

    send = opener or (lambda url, k, body: _post(url, k, body))
    answer = send(ENROLL_URL, key, payload)
    output = answer.get("output")
    if not isinstance(output, dict):
        detail = json.dumps(answer, ensure_ascii=False)[:300]
        raise CloudVoiceError(f"云端没有返回音色信息：{detail}")
    voice = output.get("voice")
    if not isinstance(voice, str) or not voice:
        raise CloudVoiceError(
            "云端没有给出音色 id：" + (str(output.get("fallback_reason") or "") or "未知原因")
        )
    bound = output.get("target_model")
    reason = output.get("fallback_reason")
    return EnrolledVoice(
        voice=voice,
        target_model=bound if isinstance(bound, str) and bound else target_model,
        request_id=str(answer.get("request_id") or ""),
        fallback_reason=reason if isinstance(reason, str) else "",
    )


def _safe_prefix(name: str) -> str:
    """Reduce a label to what the vendor accepts, keeping something recognisable.

    Digits and letters only, at most :data:`VOICE_PREFIX_MAX`. A name with no
    usable characters at all falls back to the default rather than being
    rejected -- the label is a convenience, and refusing a recording because of
    it would be the wrong trade.
    """
    kept = "".join(ch for ch in name if ch.isascii() and ch.isalnum())
    trimmed = kept[:VOICE_PREFIX_MAX]
    return trimmed or VOICE_PREFIX


def delete(voice: str, *, api_key: str | None = None) -> bool:
    """Remove a voice from the vendor's side. ``False`` if it was already gone.

    Called when the operator deletes their recording: leaving the clone alive on
    somebody else's servers after they asked to be forgotten is not a state this
    feature is allowed to end in.
    """
    payload = {"model": ENROLL_MODEL, "input": {"action": "delete", "voice": voice}}
    try:
        _post(ENROLL_URL, resolve_key(api_key), payload)
    except CloudVoiceError as exc:
        logger.warning("cloud voice %s could not be deleted: %s", voice, exc)
        return False
    return True


__all__ = [
    "DEFAULT_KEY_ENV",
    "ENROLL_URL",
    "REALTIME_HOST",
    "SYNTH_MODEL",
    "CloudVoiceError",
    "EnrolledVoice",
    "delete",
    "enrol",
    "resolve_key",
]
