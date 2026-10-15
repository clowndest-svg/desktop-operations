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
from functools import lru_cache
from typing import Any, Final, Protocol

from jarvis.llm import ChatMessage, GenerationOptions
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

_WEIGHT_SPAN: Final = re.compile(r"\([^()\n]{1,80}\)")
_WEIGHT_SUFFIX: Final = re.compile(r"(:\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*)$")
"""生成器那套权重写法：``(red car:1.2)``、``(cityscape)``。

对 SD/ComfyUI 来说这不是散文，是带权重的指令。r119 的 120 条矩阵量出来：模型会把它当句子
翻成 ``A red car (1.2)`` —— 括号里的主体被挪走、权重变成孤立小数，**12 语种里 11 条都这样**，
唯一没坏的那条是"输入本来就是英文、根本没送翻译"。

试过用占位符摘出来再放回去，量下来更糟：模型把 ``__W0__`` 直接吃掉了 11/12 次，于是那一格
从"交一句歪的"变成"什么都交不出去"。所以这一层不指望模型保住任何记号 —— **按权重段把原句
切开，只翻人话那几段，权重段原样留在原来的位置**。多出来的代价是段与段之间各打一发模型，
而这只在用户自己写了括号时发生。
"""


def _split_weight_spans(text: str) -> list[tuple[str, str]]:
    """``("text", 人话)`` / ``("span", "(red car:1.2)")`` 交替，顺序就是原句顺序。"""
    pieces: list[tuple[str, str]] = []
    cursor = 0
    for match in _WEIGHT_SPAN.finditer(text):
        if match.start() > cursor:
            pieces.append(("text", text[cursor : match.start()]))
        pieces.append(("span", match.group(0)))
        cursor = match.end()
    if cursor < len(text):
        pieces.append(("text", text[cursor:]))
    return pieces


@lru_cache(maxsize=1)
def _identifier() -> Any:
    from langid.langid import LanguageIdentifier, model  # type: ignore[import-untyped]

    return LanguageIdentifier.from_modelstring(model, norm_probs=True)


def _language(text: str) -> tuple[str, float]:
    language, confidence = _identifier().classify(text[:2000])
    return str(language), float(confidence)


def _looks_english(source: str) -> bool:
    """纯 ASCII 且带一个明确的英文虚词，才当作"已经是英文"（不误伤无重音的法/西语）。"""
    if not source.isascii():
        return False
    return (
        bool({word.lower() for word in _WORD.findall(source)} & _ENGLISH_MARKERS)
        and _language(source)[0] == "en"
    )


def _has_untranslated_text(text: str) -> bool:
    """译文里还留着非拉丁字母（CJK 等）＝没翻干净。"""
    if any(ch.isalpha() and "LATIN" not in unicodedata.name(ch, "") for ch in text):
        return True
    language, confidence = _language(text)
    return len(_WORD.findall(text)) >= 3 and language != "en" and confidence >= 0.9


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


_TRANSLATE_SYSTEM: Final = (
    "你是翻译引擎。把用户给的画面描述翻译成自然、地道的英文，"
    "只输出译文本身：不要解释、不要加引号、不要前后缀、不要思考过程。"
    "数量、动作、否定、颜色、位置关系必须原样保留。"
    "用户描述的是可见的画面和动作，按这个语境判断多义词，不增加主体或改变物体。"
)


class LocalLlmTranslator:
    """用 App 当前配的那个模型（随包就是本地 Qwen）翻一句，关掉思考求快。

    这是"零额外 key"的那条路：买家要用小夜本来就得配一个聊天模型，翻译直接借它，
    不再单独要 DASHSCOPE/百度钥匙。思考通过 ``chat_template_kwargs.enable_thinking=False``
    关掉（llama.cpp 只认这一处，顶层 enable_thinking 会被无视）。
    """

    def __init__(self, client_provider: Callable[[], Any] | None) -> None:
        self._provider = client_provider

    def available(self) -> bool:
        return self._provider is not None

    def translate(self, text: str) -> Translation:
        try:
            client = self._provider() if self._provider is not None else None
        except Exception as exc:
            raise TranslationError(f"本地翻译模型不可用（{type(exc).__name__}）") from exc
        if client is None:
            raise TranslationError("当前没有可用的对话模型来做本地翻译（先在设置里配一个模型）")
        messages = [ChatMessage.system(_TRANSLATE_SYSTEM), ChatMessage.user(text)]
        options = GenerationOptions(
            temperature=0.0,
            max_tokens=min(1400, max(256, len(text) * 2)),
            template_kwargs={"enable_thinking": False},
        )
        try:
            response = client.complete(messages, options=options)
        except Exception as exc:  # 模型没起、超时、返回结构不对——都收成一句能照着查的话
            raise TranslationError(f"本地模型翻译失败（{type(exc).__name__}）") from exc
        english = str(getattr(response, "content", "") or "").strip()
        if getattr(response, "finish_reason", None) == "length":
            raise TranslationError("译文被模型截断，尚未提交生成；请缩短描述后重试")
        if not english:
            raise TranslationError("本地模型没给出译文")
        if _has_untranslated_text(english):
            raise TranslationError("本地模型的译文仍含未翻译文字")
        return Translation(text, english, "auto", provider="本地模型")


class PromptTranslator:
    """国内服务商链 + 有界内存缓存；不写凭据、不记提示词日志、不落盘缓存。"""

    def __init__(
        self,
        opener: Callable[..., Any] = open_request,
        environ: Mapping[str, str] | None = None,
        client_provider: Callable[[], Any] | None = None,
        cache_identity: Callable[[], str] | None = None,
    ) -> None:
        self._open = opener
        self._environ = environ if environ is not None else os.environ
        self._local = LocalLlmTranslator(client_provider)
        self._cache_identity = cache_identity or (lambda: "default")
        self._cache: OrderedDict[str, Translation] = OrderedDict()
        self._lock = threading.Lock()

    def translate(self, text: str) -> Translation:
        source = str(text or "").strip()
        if not source:
            raise TranslationError("请先填写要生成的内容。")
        if len(source) > 2000:
            raise TranslationError("描述太长，请控制在 2000 字符以内，避免翻译被截断。")
        cache_key = self._cache_identity() + "\0" + source
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache.move_to_end(cache_key)
                return cached
        pieces = _split_weight_spans(source)
        if not any(kind == "span" for kind, _ in pieces):
            translated = self._translate_plain(source)
        else:
            # 权重段留在原位，只翻它两边的人话。拼回去的顺序就是用户写的顺序。
            rendered: list[str] = []
            head: Translation | None = None
            for kind, piece in pieces:
                if kind == "span":
                    # Preserve the syntax, not untranslated prose inside it. A buyer
                    # can write (三只红猫:1.20); only its inner words go to the model.
                    inner = piece[1:-1]
                    weight = _WEIGHT_SUFFIX.search(inner)
                    words = inner[: weight.start()] if weight else inner
                    if _has_untranslated_text(words):
                        one = self._translate_plain(words.strip())
                        if head is None:
                            head = one
                        rendered.append("(" + one.english + (weight[0] if weight else "") + ")")
                    else:
                        rendered.append(piece)
                    continue
                prose = piece.strip(" ，,。;；")
                if not prose:
                    continue
                one = self._translate_plain(prose)
                if head is None:
                    head = one
                rendered.append(one.english)
            spans = sum(1 for kind, _ in pieces if kind == "span")
            translated = Translation(
                source,
                " ".join(rendered).strip(),
                head.language if head is not None else "auto",
                provider=head.provider if head is not None else "本地模型",
                note_text=f"{spans} 段权重写法原样保留，括号内非英文及其余描述分别翻译。",
            )
        with self._lock:
            self._cache[cache_key] = translated
            while len(self._cache) > 128:
                self._cache.popitem(last=False)
        return translated

    def _translate_plain(self, source: str) -> Translation:
        """一段人话 → 英文。先本地（零钥匙、离线可用），失败再退云端服务商。"""
        if _looks_english(source):
            return Translation(
                source, source, "en", provider="已是英文", note_text="输入已是英文，未联网翻译。"
            )
        # 先试本地/当前聊天模型（零额外 key、离线可用），失败再退到云端服务商。
        local_tried = False
        if self._local.available():
            local_tried = True
            try:
                return self._local.translate(source)
            except TranslationError as exc:
                tried_local = str(exc)
        else:
            tried_local = "没有可用的对话模型"
        tried: list[str] = [f"本地模型（{tried_local}）"] if local_tried else []
        for name, available, run in PROVIDERS:
            if not available(self._environ):
                continue
            try:
                return run(self, source)
            except Exception as exc:  # 一家不行换下一家，但把真实原因留着
                reason = str(exc) if isinstance(exc, TranslationError) else _failure_reason(exc)
                tried.append(f"{name}（{reason}）")
                continue
        if not tried:
            raise TranslationError(
                "中文描述需要翻译，但既没有可用的对话模型、也没配联网翻译钥匙，尚未提交给生成模型。"
                "任选其一：在设置里配一个聊天模型（随包本地模型即可，无需额外钥匙）、"
                f"或设 {DASHSCOPE_KEY_ENV}（阿里云通义千问）、"
                f"或 {BAIDU_APPID_ENV}+{BAIDU_KEY_ENV}（百度）。"
                "英文描述不需要翻译，可直接用。"
            )
        raise TranslationError(
            f"翻译暂时不可用（{'；'.join(tried)}），尚未提交给生成模型。"
            f"请检查随包聊天模型；可选联网翻译需 {DASHSCOPE_KEY_ENV} 或百度凭据。"
            "不支持的语言或未完整翻译的文字也会在这里停止。"
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
