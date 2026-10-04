"""What we asked a model before trusting it: can this pair answer, and can it see?

Two decisions the panel used to make blind. A model row could be saved with a typo in its
name or a key with no rights on it, and the operator found out on the first real question
-- which is also the worst possible moment, because by then the answer is already missing.
So saving now happens after a probe, and the probe measures the one capability that
changes what gets sent: whether this model takes an image with the question.

The picture is a 64x64 solid-red PNG built from bytes, so this needs no asset files and no
Pillow -- Pillow is not in the packaged build. The question has one right answer that a
text-only model cannot guess from context: a provider that cannot see either refuses the
request or says something that is not 红.
"""

from __future__ import annotations

import base64
import logging
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from jarvis.app.preferences import LLM_CAPS, Preferences
from jarvis.core.exceptions import JarvisError
from jarvis.llm.client import LlmClient
from jarvis.llm.types import ChatMessage, GenerationOptions

logger = logging.getLogger("jarvis.app.model_probe")

__all__ = ["ModelCaps", "ModelProber", "ProbeResult", "probe", "red_png_data_url"]

MAX_DETAIL_CHARS = 240
"""Long enough for a provider's whole complaint, short enough to keep in a settings file."""

_PROBE_QUESTIONS = ("只回复两个字：收到", "这张图整体是什么颜色？只回答颜色名。")
"""The two asks: one proves the wire works, the other proves the picture arrived."""

_PROBE_TOKENS = 24
"""A probe must not be allowed to write an essay. Long enough for 红 and for a reasoning
model to get past its own preamble, short enough that testing a model is not a spend."""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One probe's answer, as shown in the panel and as filed in :class:`ModelCaps`."""

    ok: bool
    """Whether plain text questions can be answered. This is the gate on saving."""

    detail: str
    """What the model said, or why it could not. Empty on the clean path."""

    vision: bool | None
    """``True`` only when it named the colour of the picture it was sent.

    ``None`` means the vision half was not attempted -- a pair that fails the plain
    question never gets as far as the image, and recording that as ``False`` would say
    something we did not test.
    """

    latency_ms: float
    provider: str
    model: str

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "vision": self.vision,
            "latency_ms": round(self.latency_ms, 1),
            "provider": self.provider,
            "model": self.model,
        }


def red_png_data_url() -> str:
    """A 64x64 solid red PNG, assembled from bytes, as a data URL.

    Built rather than shipped as a file because the packaged exe would need the file added
    to PyInstaller's data list, and a red square is 15 lines of ``zlib``.
    """
    size = 64

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            len(payload).to_bytes(4, "big")
            + kind
            + payload
            + zlib.crc32(kind + payload).to_bytes(4, "big")
        )

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * size for _ in range(size))
    header = size.to_bytes(4, "big") * 2 + b"\x08\x02\x00\x00\x00"
    png = b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", header),
            chunk(b"IDAT", zlib.compress(raw)),
            chunk(b"IEND", b""),
        ]
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def probe(client: LlmClient, *, with_vision: bool = True) -> ProbeResult:
    """Ask one model the two questions, and report what came back.

    Never raises: a probe that throws is a panel with a dead button, and "the key is wrong"
    is an answer the operator can act on. Every failure ends up in :attr:`ProbeResult.detail`.
    """
    started = time.perf_counter()
    provider, model = client.provider_name, client.model
    options = GenerationOptions(max_tokens=_PROBE_TOKENS, temperature=0.0)
    try:
        answer = client.complete([ChatMessage.user(_PROBE_QUESTIONS[0])], options=options)
    except JarvisError as exc:
        return _failure(client, started, _text_of(exc))
    except Exception as exc:  # a provider SDK blowing up is still just a failed test
        logger.exception("probe of %s/%s blew up", provider, model)
        return _failure(client, started, f"{type(exc).__name__}: {exc}")
    text = (answer.content or "").strip()
    if not text:
        return _failure(client, started, "连接通了，但它一个字也没回")
    if not with_vision:
        return ProbeResult(
            ok=True,
            detail=text[:MAX_DETAIL_CHARS],
            vision=None,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            provider=provider,
            model=model,
        )
    return _probe_vision(client, started, text, options)


def _probe_vision(
    client: LlmClient,
    started: float,
    text: str,
    options: GenerationOptions,
) -> ProbeResult:
    """The second request, with the picture on. A refusal here is an answer, not a failure.

    The pair is already reachable, so a rejected image is that model saying it has no
    eyes -- which is worth recording precisely because it is not an error condition. Both
    halves have to hold for the record to read 支持, and "it took the request and then
    described a blue square" is the case that would otherwise be filed as working.
    """
    base, model = client.provider_name, client.model
    with_picture = replace(ChatMessage.user(_PROBE_QUESTIONS[1]), images=(red_png_data_url(),))
    try:
        seen = client.complete([with_picture], options=options)
    except JarvisError:
        return ProbeResult(
            ok=True,
            detail=text[:MAX_DETAIL_CHARS],
            vision=False,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            provider=base,
            model=model,
        )
    except Exception:
        logger.exception("vision probe of %s/%s blew up", base, model)
        return ProbeResult(
            ok=True,
            detail=text[:MAX_DETAIL_CHARS],
            vision=None,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            provider=base,
            model=model,
        )
    said = (seen.content or "").strip()
    return ProbeResult(
        ok=True,
        detail=f"文字「{text[:60]}」／看图「{said[:60]}」"[:MAX_DETAIL_CHARS],
        vision="红" in said,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        provider=base,
        model=model,
    )


def _failure(client: LlmClient, started: float, detail: str) -> ProbeResult:
    return ProbeResult(
        ok=False,
        detail=detail[:MAX_DETAIL_CHARS],
        vision=None,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        provider=client.provider_name,
        model=client.model,
    )


def _text_of(exc: JarvisError) -> str:
    detail = str(exc).strip()
    return detail or type(exc).__name__


class ModelProber:
    """The panel's 测一下 button, as an object.

    Holds both halves of the answer -- how to reach a model, and where the finding is
    kept -- because a probe that is not filed is a log line: the operator saw it pass
    once and nothing downstream ever learned.
    """

    def __init__(
        self,
        client_for: Callable[[str, str], LlmClient],
        caps: ModelCaps,
    ) -> None:
        self._client_for = client_for
        self._caps = caps

    def test(self, provider: str, model: str) -> dict[str, object]:
        """Probe one pair and file the finding. Never raises.

        A client that cannot even be built is the same answer as a client that refuses to
        answer -- both mean this row cannot be used -- so the reason goes back in the same
        ``detail`` field the panel already knows how to show.
        """
        try:
            client = self._client_for(provider, model)
        except JarvisError as exc:
            return {
                "ok": False,
                "vision": None,
                "detail": _text_of(exc),
                "provider": provider,
                "model": model,
            }
        except Exception as exc:
            logger.exception("could not build a client for %s/%s", provider, model)
            return {
                "ok": False,
                "vision": None,
                "detail": f"{type(exc).__name__}: {exc}"[:MAX_DETAIL_CHARS],
                "provider": provider,
                "model": model,
            }
        result = probe(client)
        self._caps.record(result.provider, result.model, result)
        return result.to_dict()

    def caps(self) -> ModelCaps:
        return self._caps


def pair_key(provider: str, model: str) -> str:
    """The one key shape for "this provider, this model".

    Same separator the per-model thinking table uses, so a row that exists in both places
    is the same row and a name with a space or a slash in it cannot collide with another.
    """
    return f"{provider}\x00{model}"


class ModelCaps:
    """What probes have established about each model, keyed by provider and model id.

    Its own preference key rather than a field on the thinking table: the thinking row is
    edited by the operator, and a measurement has to be overwritten by the next
    measurement, not by someone changing how hard a model should think.

    Everything in here is a claim about the past. :meth:`verified` is therefore only as
    good as the last probe, which is why an expired row reads as untested rather than as
    a license to send an image.
    """

    STALE_AFTER_DAYS = 30
    """How long a probe says anything. A provider can add eyes; it can also lose a model."""

    def __init__(self, preferences: Preferences) -> None:
        self._prefs = preferences

    def _write(self, table: dict[str, Any]) -> None:
        """Store the table, loudly if the store refuses it.

        ``Preferences.set`` answers ``False`` for a key it does not recognise, and an
        ignored write here is indistinguishable from "never probed" -- which is the one
        state this class must never lie about.
        """
        if not self._prefs.set(LLM_CAPS, table):
            logger.error(
                "preference %r refused the write; every model will read as never tested",
                LLM_CAPS,
            )

    def record(self, provider: str, model: str, result: ProbeResult) -> None:
        table = self._table()
        table[pair_key(provider, model)] = {
            "chat": result.ok,
            "vision": result.vision,
            "detail": result.detail,
            "latency_ms": round(result.latency_ms, 1),
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        self._write(table)

    def forget(self, provider: str, model: str) -> None:
        """Drop a row, because a deleted model must not come back pre-verified.

        The key is the pair's name and nothing else, so a model removed and re-added under
        the same id would otherwise inherit a probe that was made against whatever stood
        there before it.
        """
        table = self._table()
        if table.pop(pair_key(provider, model), None) is not None:
            self._write(table)

    def verified(self, provider: str, model: str) -> bool:
        """Whether a recent probe found this pair answering."""
        row = self._row(provider, model)
        return bool(row and row.get("chat") is True and not self._stale(row))

    def vision(self, provider: str, model: str) -> bool | None:
        """``True``/``False`` from the last probe, ``None`` for "nobody has looked".

        ``None`` is not ``False``: a pair that was never probed still gets its pictures on
        the wire, and the operator finds out from the provider's own answer rather than
        from a rule this file invented.
        """
        row = self._row(provider, model)
        if row is None or self._stale(row):
            return None
        value = row.get("vision")
        return value if isinstance(value, bool) else None

    def as_rows(self) -> list[dict[str, Any]]:
        """Every finding, one row per pair, for the panel to badge its list with.

        Rows rather than a table keyed by pair: the key carries a NUL, which the page can
        build but should not have to, and a list survives the JSON hop to a webview
        without either side inventing an escaping rule.
        """
        rows: list[dict[str, Any]] = []
        for key, row in self._table().items():
            provider, _, model = key.partition("\x00")
            rows.append({"provider": provider, "model": model, **dict(row)})
        return rows

    def _row(self, provider: str, model: str) -> dict[str, Any] | None:
        row = self._table().get(pair_key(provider, model))
        return row if isinstance(row, dict) else None

    def _table(self) -> dict[str, Any]:
        raw = self._prefs.get(LLM_CAPS, {})
        return dict(raw) if isinstance(raw, dict) else {}

    def _stale(self, row: dict[str, Any]) -> bool:
        stamp = str(row.get("at") or "")
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            return True
        return (datetime.now() - when).days >= self.STALE_AFTER_DAYS
