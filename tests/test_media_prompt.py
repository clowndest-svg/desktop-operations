"""国内服务商链：英文跳过、按序兜底、失败说真话、绝不泄露提示词。"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from jarvis.app.media_prompt import (
    BAIDU_APPID_ENV,
    BAIDU_KEY_ENV,
    DASHSCOPE_KEY_ENV,
    PromptTranslator,
    TranslationError,
    _dig_message_content,
    _looks_english,
)


def _resp(payload: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode())


def _qwen_ok(english: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": english}}]}


def _baidu_ok(dst: str) -> dict[str, Any]:
    return {"from": "zh", "to": "en", "trans_result": [{"src": "x", "dst": dst}]}


def test_english_input_never_touches_the_network() -> None:
    def opener(request: Any, timeout: float) -> io.BytesIO:  # pragma: no cover
        raise AssertionError("英文输入不该发网络请求")

    result = PromptTranslator(opener, {DASHSCOPE_KEY_ENV: "sk"}).translate(
        "an orange cat running in the snow"
    )
    assert result.english == "an orange cat running in the snow"
    assert "未联网" in result.note


def test_unaccented_foreign_is_not_skipped() -> None:
    assert not _looks_english("Dos gatos negros")
    assert not _looks_english("Un chat orange court dans la neige")
    assert _looks_english("a cat with a hat behind the tree")


def test_qwen_mt_is_tried_first_and_parses_openai_shape() -> None:
    seen: list[str] = []

    def opener(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        seen.append(request.full_url)
        raw = request.data if isinstance(request.data, (bytes, bytearray)) else b"{}"
        body = json.loads(raw)
        assert body["translation_options"]["target_lang"] == "English"
        assert body["messages"][0]["content"] == "一只橘猫在跑步"
        return _resp(_qwen_ok("An orange cat runs"))

    result = PromptTranslator(
        opener, {DASHSCOPE_KEY_ENV: "sk", BAIDU_APPID_ENV: "a", BAIDU_KEY_ENV: "b"}
    ).translate("一只橘猫在跑步")
    assert result.english == "An orange cat runs"
    assert result.provider == "阿里云通义千问"
    assert "dashscope" in seen[0] and "baidu" not in "".join(seen)  # 第一家成功就不再试第二家


def test_falls_back_to_baidu_when_qwen_fails() -> None:
    order: list[str] = []

    def opener(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        order.append("qwen" if "dashscope" in request.full_url else "baidu")
        if "dashscope" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many", {}, None)  # type: ignore[arg-type]
        query = parse_qs(urlparse(request.full_url).query)
        assert query["appid"] == ["id1"] and "sign" in query
        return _resp(_baidu_ok("Two black cats without hats"))

    env = {DASHSCOPE_KEY_ENV: "sk", BAIDU_APPID_ENV: "id1", BAIDU_KEY_ENV: "secret"}
    result = PromptTranslator(opener, env).translate("两只黑猫没有帽子")
    assert result.english == "Two black cats without hats"
    assert result.provider == "百度翻译"
    assert order == ["qwen", "baidu"]


def test_no_key_configured_names_the_env_vars() -> None:
    def opener(request: Any, timeout: float) -> io.BytesIO:  # pragma: no cover
        raise AssertionError("没配钥匙就不该发请求")

    with pytest.raises(TranslationError) as error:
        PromptTranslator(opener, {}).translate("一只猫")
    message = str(error.value)
    assert DASHSCOPE_KEY_ENV in message and BAIDU_APPID_ENV in message


def test_all_providers_failing_surfaces_reason_without_the_prompt() -> None:
    secret = "一只戴着蓝色围巾的橘猫"

    def opener(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)  # type: ignore[arg-type]

    env = {DASHSCOPE_KEY_ENV: "sk", BAIDU_APPID_ENV: "id", BAIDU_KEY_ENV: "key"}
    with pytest.raises(TranslationError) as error:
        PromptTranslator(opener, env).translate(secret)
    message = str(error.value)
    assert "尚未提交" in message and "500" in message
    assert "通义千问" in message and "百度翻译" in message
    assert secret not in message  # 提示词绝不进错误


def test_baidu_error_code_is_not_treated_as_success() -> None:
    def opener(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        return _resp({"error_code": "54001", "msg": "Invalid Sign"})

    env = {BAIDU_APPID_ENV: "id", BAIDU_KEY_ENV: "key"}  # 只有百度配了钥匙
    with pytest.raises(TranslationError, match="百度"):
        PromptTranslator(opener, env).translate("猫")


def test_untranslated_output_stops_generation() -> None:
    def opener(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        return _resp(_qwen_ok("还是中文没有翻"))

    with pytest.raises(TranslationError, match="未翻译"):
        PromptTranslator(opener, {DASHSCOPE_KEY_ENV: "sk"}).translate("一只猫")


def test_result_is_cached_and_second_call_skips_network() -> None:
    calls: list[str] = []

    def opener(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        calls.append(request.full_url)
        return _resp(_qwen_ok("A cat"))

    translator = PromptTranslator(opener, {DASHSCOPE_KEY_ENV: "sk"})
    first = translator.translate("一只猫")
    second = translator.translate("一只猫")
    assert first.english == second.english == "A cat"
    assert len(calls) == 1


def test_dig_message_content_is_defensive() -> None:
    assert _dig_message_content(_qwen_ok("hi")) == "hi"
    assert _dig_message_content({}) == ""
    assert _dig_message_content({"choices": []}) == ""
    assert _dig_message_content({"choices": [{"message": {}}]}) == ""


# -- r116：本地/当前聊天模型翻译（零额外 key、关思考），失败退云端 --------------


class _Resp2:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeClient:
    def __init__(self, content: str, capture: dict[str, Any] | None = None) -> None:
        self._content = content
        self.capture = capture

    def complete(self, messages: Any, options: Any = None) -> _Resp2:
        if self.capture is not None:
            self.capture["messages"] = messages
            self.capture["options"] = options
        return _Resp2(self._content)


def _boom(request: Any, timeout: float) -> io.BytesIO:  # 一旦被调用就说明走错了路
    raise AssertionError("本地模型能翻时不该发网络请求")


def test_local_model_translates_without_any_key() -> None:
    cap: dict[str, Any] = {}
    client = _FakeClient("An orange cat runs", cap)
    translator = PromptTranslator(_boom, {}, client_provider=lambda: client)
    result = translator.translate("一只橘猫在跑步")
    assert result.english == "An orange cat runs"
    assert result.provider == "本地模型"
    # 思考必须关：llama.cpp 只认 chat_template_kwargs.enable_thinking
    assert cap["options"].template_kwargs == {"enable_thinking": False}


def test_local_unavailable_falls_back_to_cloud() -> None:
    def opener(request: Any, timeout: float) -> io.BytesIO:
        return _resp(_qwen_ok("Two cats"))

    translator = PromptTranslator(opener, {DASHSCOPE_KEY_ENV: "sk"}, client_provider=lambda: None)
    result = translator.translate("两只猫")
    assert result.provider == "阿里云通义千问"


def test_local_model_returning_chinese_falls_through_to_cloud() -> None:
    client = _FakeClient("还是中文没翻")  # 含 CJK，应判为没翻干净

    def opener(request: Any, timeout: float) -> io.BytesIO:
        return _resp(_qwen_ok("A cat"))

    translator = PromptTranslator(opener, {DASHSCOPE_KEY_ENV: "sk"}, client_provider=lambda: client)
    result = translator.translate("一只猫")
    assert result.provider == "阿里云通义千问"


def test_no_model_and_no_key_names_both_paths() -> None:
    translator = PromptTranslator(_boom, {}, client_provider=lambda: None)
    with pytest.raises(TranslationError) as error:
        translator.translate("猫")
    message = str(error.value)
    assert "聊天模型" in message and DASHSCOPE_KEY_ENV in message


# -- 权重语法 (red car:1.2) 不能被当散文翻掉 ---------------------------------
# r119 的 120 条矩阵量出来：12 语种里只有"输入本来就是英文"那条保住了括号，
# 其余全被改写成 "A red car (1.2)"。修法不是让模型"记住再吐回来"（占位符 11/12 次被吃掉），
# 而是按权重段切开、只翻人话段、原样拼回原位。


class _ScriptedClient:
    """按调用次序回不同的译文 —— 权重段会把一句话切成几段人话，每段各打一发。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.seen: list[str] = []

    def complete(self, messages: Any, options: Any = None) -> _Resp2:
        del options
        self.seen.append(messages[1].content)
        return _Resp2(self.replies[min(len(self.seen) - 1, len(self.replies) - 1)])


def test_the_model_is_asked_for_the_prose_only() -> None:
    client = _ScriptedClient(["on a bridge, no pedestrians on the road"])
    PromptTranslator(_boom, {}, client_provider=lambda: client).translate(
        "(red car:1.2) 在桥上，路上没有行人"
    )
    assert len(client.seen) == 1, "权重段不该占一发请求"
    assert "(red car:1.2)" not in client.seen[0], "送进去的必须是不含权重段的那句话"
    assert "__W0__" not in client.seen[0], "不指望模型保住任何记号"


def test_weight_span_goes_back_to_where_it_was() -> None:
    client = _ScriptedClient(["on a bridge, no pedestrians on the road"])
    result = PromptTranslator(_boom, {}, client_provider=lambda: client).translate(
        "(red car:1.2) 在桥上，路上没有行人"
    )
    assert result.english == "(red car:1.2) on a bridge, no pedestrians on the road"
    assert "权重写法原样保留" in result.note


def test_two_spans_keep_their_own_positions() -> None:
    client = _ScriptedClient(["a cat sitting", "and a dog running"])
    result = PromptTranslator(_boom, {}, client_provider=lambda: client).translate(
        "(black cat:1.3) 坐着 和 (white dog:1.1) 跑着"
    )
    assert result.english.index("(black cat:1.3)") < result.english.index("(white dog:1.1)")
    assert client.seen == ["坐着 和", "跑着"], "送翻译的必须是被权重段切开的那几段人话"


def test_a_mangled_answer_still_cannot_lose_the_span() -> None:
    """模型答什么都行 —— 权重段是拼回去的，不依赖它吐回来。"""
    client = _ScriptedClient(["on a bridge"])
    result = PromptTranslator(_boom, {}, client_provider=lambda: client).translate(
        "(red car:1.2) 在桥上"
    )
    assert result.english.count("(red car:1.2)") == 1


def test_a_description_with_no_weights_is_a_single_call() -> None:
    client = _ScriptedClient(["An orange cat runs"])
    result = PromptTranslator(_boom, {}, client_provider=lambda: client).translate("一只橘猫在跑步")
    assert client.seen == ["一只橘猫在跑步"], "没有权重段就不该多打一发"
    assert result.english == "An orange cat runs"
    assert "权重" not in result.note
