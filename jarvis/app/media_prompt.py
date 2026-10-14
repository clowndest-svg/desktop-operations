"""Translate complete media descriptions before any image/video inference.

Google's public Translate endpoint needs no key. It is an online service: failure
must stop generation instead of quietly dropping words through a small dictionary.
Both desktop forms and chat tools use this same boundary.
"""

from __future__ import annotations

import json
import threading
import unicodedata
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from jarvis.llm.transport import open_request


class TranslationError(RuntimeError):
    """No verified English description is available."""


@dataclass(frozen=True, slots=True)
class Translation:
    original: str
    english: str
    language: str
    provider: str = "Google Translate"

    @property
    def note(self) -> str:
        return f"{self.provider} 自动识别 {self.language} → 英文，按整句翻译。"


class Translator(Protocol):
    def translate(self, text: str) -> Translation: ...


class PromptTranslator:
    """Bounded in-memory cache; no credentials, prompt logs or disk cache."""

    def __init__(self, opener: Callable[..., Any] = open_request) -> None:
        self._open = opener
        self._cache: OrderedDict[str, Translation] = OrderedDict()
        self._lock = threading.Lock()

    def translate(self, text: str) -> Translation:
        source = str(text or "").strip()
        if not source:
            raise TranslationError("请先填写要生成的内容。")
        if len(source) > 8000:
            raise TranslationError("描述太长，请控制在 8000 字符以内，避免翻译被截断。")
        with self._lock:
            cached = self._cache.get(source)
            if cached is not None:
                self._cache.move_to_end(source)
                return cached
        query = urllib.parse.urlencode(
            {"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": source}
        )
        for host in ("translate.googleapis.com", "translate.google.com"):
            request = urllib.request.Request(
                f"https://{host}/translate_a/single?{query}",
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            )
            try:
                with self._open(request, timeout=15) as response:
                    payload = json.loads(response.read(1024 * 1024))
                translated = self._parse(source, payload)
            except Exception:
                # Do not surface URLs (which include private prompts), HTML or tokens.
                continue
            with self._lock:
                self._cache[source] = translated
                while len(self._cache) > 128:
                    self._cache.popitem(last=False)
            return translated
        raise TranslationError(
            "在线翻译暂时不可用，尚未提交给生成模型。请检查网络后重试；"
            "不支持的语言或未完整翻译的文字也会在这里停止。"
        )

    @staticmethod
    def _parse(source: str, payload: object) -> Translation:
        if not isinstance(payload, list) or len(payload) < 3:
            raise TranslationError("翻译服务返回格式异常。")
        segments, language = payload[0], payload[2]
        if not isinstance(segments, list) or not isinstance(language, str) or not language:
            raise TranslationError("翻译服务没有返回完整译文。")
        parts: list[str] = []
        originals: list[str] = []
        for segment in segments:
            if not isinstance(segment, list) or len(segment) < 2:
                raise TranslationError("翻译服务返回了不完整的句子。")
            if not isinstance(segment[0], str) or not isinstance(segment[1], str):
                raise TranslationError("翻译服务返回了不完整的句子。")
            parts.append(segment[0])
            originals.append(segment[1])

        def compact(value: str) -> str:
            return "".join(value.split())

        if compact("".join(originals)) != compact(source):
            raise TranslationError("翻译没有覆盖整段原文。")
        english = source if language == "en" else "".join(parts).strip()
        if not english or any(
            ch.isalpha() and "LATIN" not in unicodedata.name(ch, "") for ch in english
        ):
            raise TranslationError("译文仍含有未翻译的文字。")
        return Translation(source, english, language)
