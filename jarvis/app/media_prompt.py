"""Translate complete media descriptions before any image/video inference.

国内优先、多家兜底（r114 改）
--------------------------------
以前只打 Google 的免 Key 端点，两个真问题：一是它在国内经常连不上/被限流（HTTP 429），
"有网却翻不动"；二是它把真实失败原因吞成一句"检查网络"。用户明确要求：**别用 Google，
换成国内能直连的联网翻译，多备几家，一家不行换下一家。**

这一版因此做成一条**服务商链**：按顺序试，第一个配了钥匙且成功的那个胜出；每个失败都留下
**真实原因**（HTTP 码 / 超时 / 返回结构不对），但**绝不把提示词或带密钥的 URL 写进错误里**。

- **阿里云通义千问 Qwen-MT**（主）：走 `dashscope.aliyuncs.com` 的 OpenAI 兼容端点，
  复用项目里已有的 `DASHSCOPE_API_KEY`（云端语音、LLM provider 都在用这个域，国内直连）。
  专做翻译的 `qwen-mt-turbo`，不会像通用大模型那样改写。
- **百度翻译开放平台**（备）：`BAIDU_TRANSLATE_APPID` + `BAIDU_TRANSLATE_KEY`。

再加服务商只要往 :data:`PROVIDERS` 里添一条。英文输入直接跳过联网（见 :func:`_looks_english`）。
诚实边界：这条链验到**契约层**（离线假端点跑通请求拼装→解析→切换→落英文），**没拿真账号
逐家端到端跑过**——每家都要你自己的 Key，配好先生成一次确认。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from jarvis.llm.transport import open_request

DASHSCOPE_TRANSLATE_URL: Final = (
    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
)
DASHSCOPE_KEY_ENV: Final = "DASHSCOPE_API_KEY"
QWEN_MT_MODEL: Final = "qwen-mt-turbo"
BAIDU_TRANSLATE_URL: Final = "https://fanyi-api.baidu.com/api/trans/vip/translate"
BAIDU_APPID_ENV: Final = "BAIDU_TRANSLATE_APPID"
BAIDU_KEY_ENV: Final = "BAIDU_TRANSLATE_KEY"

# 只放几乎不可能是法/西/德等语言的高频英文虚词：命中一个才当作"已是英文"、跳过联网。
_ENGLISH_MARKERS: frozenset[str] = frozenset(
    {
        "the",
        "an",
        "and",
        "with",
        "this",
        "that",
        "from",
        "into",
        "onto",
        "your",
        "were",
        "they",
        "there",
        "which",
        "about",
        "over",
        "under",
        "behind",
        "beside",
        "while",
        "before",
        "after",
    }
)
_WORD = re.compile(r"[a-zA-Z]+")


def _looks_english(source: str) -> bool:
    """纯 ASCII 且带一个明确的英文虚词，才当作"已经是英文"（不误伤无重音的法/西语）。"""
    if not source.isascii():
        return False
    return bool({word.lower() for word in _WORD.findall(source)} & _ENGLISH_MARKERS)


def _has_untranslated_text(text: str) -> bool:
    """译文里还留着非拉丁字母（CJK 等）＝没翻干净。"""
    return any(ch.isalpha() and "LATIN" not in unicodedata.name(ch, "") for ch in text)


def _failure_reason(exc: Exception) -> str:
    """把异常收成一句能照着查的话，**不带 URL、密钥或提示词**。"""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, TimeoutError):
        return "连接超时"
    return type(exc).__name__


class TranslationError(RuntimeError):
    """No verified English description is available."""


@dataclass(frozen=True, slots=True)
class Translation:
    original: str
    english: str
    language: str
    provider: str = "联网翻译"
    note_text: str | None = None

    @property
    def note(self) -> str:
        if self.note_text is not None:
            return self.note_text
        return f"{self.provider} 自动识别 {self.language} → 英文，按整句翻译。"


class Translator(Protocol):
    def translate(self, text: str) -> Translation: ...


class PromptTranslator:
    """国内服务商链 + 有界内存缓存；不写凭据、不记提示词日志、不落盘缓存。"""

    def __init__(
        self,
        opener: Callable[..., Any] = open_request,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._open = opener
        self._environ = environ if environ is not None else os.environ
        self._cache: OrderedDict[str, Translation] = OrderedDict()
        self._lock = threading.Lock()

    def translate(self, text: str) -> Translation:
        source = str(text or "").strip()
        if not source:
            raise TranslationError("请先填写要生成的内容。")
        if len(source) > 8000:
            raise TranslationError("描述太长，请控制在 8000 字符以内，避免翻译被截断。")
        if _looks_english(source):
            return Translation(
                source, source, "en", provider="已是英文", note_text="输入已是英文，未联网翻译。"
            )
        with self._lock:
            cached = self._cache.get(source)
            if cached is not None:
                self._cache.move_to_end(source)
                return cached
        tried: list[str] = []
        for name, available, run in PROVIDERS:
            if not available(self._environ):
                continue
            try:
                translated = run(self, source)
            except Exception as exc:  # 一家不行换下一家，但把真实原因留着
                reason = str(exc) if isinstance(exc, TranslationError) else _failure_reason(exc)
                tried.append(f"{name}（{reason}）")
                continue
            with self._lock:
                self._cache[source] = translated
                while len(self._cache) > 128:
                    self._cache.popitem(last=False)
            return translated
        if not tried:
            raise TranslationError(
                "没配任何联网翻译钥匙，尚未提交给生成模型。设其一："
                f"{DASHSCOPE_KEY_ENV}（阿里云通义千问）或 "
                f"{BAIDU_APPID_ENV}+{BAIDU_KEY_ENV}（百度翻译）。"
            )
        raise TranslationError(
            f"联网翻译暂时不可用（{'；'.join(tried)}），尚未提交给生成模型。"
            "请重试；不支持的语言或未完整翻译的文字也会在这里停止。"
        )

    # -- 网络小工具 --------------------------------------------------------

    def _json(self, request: urllib.request.Request) -> object:
        with self._open(request, timeout=15) as response:
            return json.loads(response.read(1024 * 1024))

    # -- 各服务商 ----------------------------------------------------------

    def _via_qwen_mt(self, source: str) -> Translation:
        key = str(self._environ.get(DASHSCOPE_KEY_ENV, "")).strip()
        if not key:
            raise TranslationError(f"缺 {DASHSCOPE_KEY_ENV}。")
        if not key.isascii():
            raise TranslationError(f"{DASHSCOPE_KEY_ENV} 里有非 ASCII 字符，HTTP 头装不下。")
        model = str(self._environ.get("XIAOYE_TRANSLATE_MODEL", "")).strip() or QWEN_MT_MODEL
        body = {
            "model": model,
            "messages": [{"role": "user", "content": source}],
            "translation_options": {"source_lang": "auto", "target_lang": "English"},
        }
        request = urllib.request.Request(
            DASHSCOPE_TRANSLATE_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
            method="POST",
        )
        payload = self._json(request)
        english = _dig_message_content(payload)
        if not english:
            raise TranslationError("通义千问没返回译文。")
        if _has_untranslated_text(english):
            raise TranslationError("通义千问的译文仍含未翻译文字。")
        return Translation(source, english, "auto", provider="阿里云通义千问")

    def _via_baidu(self, source: str) -> Translation:
        appid = str(self._environ.get(BAIDU_APPID_ENV, "")).strip()
        key = str(self._environ.get(BAIDU_KEY_ENV, "")).strip()
        if not appid or not key:
            raise TranslationError(f"缺 {BAIDU_APPID_ENV} 或 {BAIDU_KEY_ENV}。")
        salt = str(random.randint(10**7, 10**8 - 1))
        raw = f"{appid}{source}{salt}{key}".encode()
        sign = hashlib.md5(raw).hexdigest()
        query = urllib.parse.urlencode(
            {"q": source, "from": "auto", "to": "en", "appid": appid, "salt": salt, "sign": sign}
        )
        request = urllib.request.Request(
            f"{BAIDU_TRANSLATE_URL}?{query}", headers={"User-Agent": "Mozilla/5.0"}
        )
        payload = self._json(request)
        if not isinstance(payload, Mapping):
            raise TranslationError("百度返回格式异常。")
        if payload.get("error_code"):
            raise TranslationError(f"百度返回错误 {payload.get('error_code')}。")
        segments = payload.get("trans_result")
        if not isinstance(segments, list) or not segments:
            raise TranslationError("百度没返回译文。")
        english = " ".join(
            str(seg.get("dst", "")).strip() for seg in segments if isinstance(seg, Mapping)
        ).strip()
        if not english:
            raise TranslationError("百度译文为空。")
        if _has_untranslated_text(english):
            raise TranslationError("百度的译文仍含未翻译文字。")
        return Translation(source, english, str(payload.get("from") or "auto"), provider="百度翻译")


def _dig_message_content(payload: object) -> str:
    """从 OpenAI 兼容返回里取 ``choices[0].message.content``，取不到给空串。"""
    if not isinstance(payload, Mapping):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, Mapping):
            message = first.get("message")
            if isinstance(message, Mapping):
                return str(message.get("content") or "").strip()
    return ""


# (名字, 是否可用, 调用) —— 加服务商只往这里添一条，链会自动按顺序兜底。
PROVIDERS: tuple[
    tuple[str, Callable[[Mapping[str, str]], bool], Callable[..., Translation]], ...
] = (
    (
        "通义千问",
        lambda env: bool(str(env.get(DASHSCOPE_KEY_ENV, "")).strip()),
        PromptTranslator._via_qwen_mt,
    ),
    (
        "百度翻译",
        lambda env: bool(
            str(env.get(BAIDU_APPID_ENV, "")).strip() and str(env.get(BAIDU_KEY_ENV, "")).strip()
        ),
        PromptTranslator._via_baidu,
    ),
)


__all__ = [
    "BAIDU_APPID_ENV",
    "BAIDU_KEY_ENV",
    "DASHSCOPE_KEY_ENV",
    "PromptTranslator",
    "Translation",
    "TranslationError",
    "Translator",
]
