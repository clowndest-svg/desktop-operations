"""Ask an OpenAI-compatible endpoint which models it serves (``GET <base>/models``).

Lives in ``app`` rather than ``llm`` on purpose: the layering rule says the window
(``ui``) may only talk to ``app``/``config``/``core``, and this is a settings-panel
question, not part of answering a chat turn. Keeping it out of ``llm`` also keeps the
chat client's transport protocol free of a method only one dialog uses.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping

__all__ = ["list_endpoint_models"]


def _get_json(url: str, *, timeout: float, headers: Mapping[str, str]) -> tuple[int, object]:
    """One plain GET that returns ``(status, parsed json or None)``.

    Deliberately *not* part of :class:`~jarvis.llm.transport.HttpTransport`: listing
    models is a settings-panel question, not a chat request, and widening the protocol
    every fake transport in the test suite implements would cost more than this call.
    """
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            body = response.read()
    except urllib.error.HTTPError as exc:  # a refusal is an answer, not a crash
        try:
            return int(exc.code), json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            return int(exc.code), None
    try:
        return status, json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return status, None


def list_endpoint_models(
    base_url: str,
    *,
    timeout_seconds: float = 10.0,
    api_key: str = "",
) -> tuple[list[str], str]:
    """Ask an OpenAI-compatible endpoint which models it serves.

    Returns ``(model ids, refusal reason)``; exactly one of the two is empty. The reason
    is what the panel shows next to the button, so it has to say *why*: "连不上" and
    "这一家要钥匙才肯列模型" send the operator to different places.
    """
    url = base_url.rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        status, payload = _get_json(url, timeout=timeout_seconds, headers=headers)
    except urllib.error.URLError as exc:
        return [], f"连不上 {base_url}：{getattr(exc, 'reason', exc)}"
    except OSError as exc:
        return [], f"连不上 {base_url}：{exc}"
    except ValueError as exc:  # bad URL shape
        return [], f"地址不对：{exc}"
    if status in (401, 403):
        return [], "这一家要钥匙才肯列模型；先把 Key 填上再拉"
    if status >= 400:
        return [], f"它回了 {status}，不是模型清单"
    if not isinstance(payload, dict):
        return [], "回的不是模型清单（缺 data 数组）"
    rows = payload.get("data")
    if not isinstance(rows, list):
        return [], "回的不是模型清单（缺 data 数组）"
    ids = [str(row.get("id") or "").strip() for row in rows if isinstance(row, dict)]
    ids = [item for item in ids if item]
    if not ids:
        return [], "清单是空的：这家一个模型都没列"
    return ids, ""
