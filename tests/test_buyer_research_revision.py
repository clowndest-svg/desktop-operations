"""Behavior regressions for the buyer/chat changes; no live network or private data."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.app.image_gen import ImageService
from jarvis.app.media_prompt import LocalLlmTranslator, TranslationError, _looks_english
from jarvis.app.tool_routing import classify_tool_intent
from jarvis.app.transcript_service import TranscriptService
from jarvis.app.web_research import public_sources, wants_web
from jarvis.config.edition import prepare_buyer_environment
from jarvis.database import SqliteStore
from jarvis.llm.errors import LlmConnectionError
from jarvis.tools.builtins.web_tools import _parse_rss


@pytest.mark.parametrize(
    "question",
    [
        "What are the latest US university rankings?",
        "Please search the web for current university rankings.",
        "今日の天気を調べて",
        "최신 대학 순위 검색",
        "Rechercher les actualités et le classement des universités",
        "Buscar en internet la clasificación de universidades",
        "Aktuelle Nachrichten und Wetter",
        "Новости и рейтинг университетов",
        "今年美国大学有哪些排名",
    ],
)
def test_online_requests_in_other_languages_get_search_and_page_tools(question: str) -> None:
    assert wants_web(question)
    assert "network" in classify_tool_intent(question)


@pytest.mark.parametrize(
    "question",
    [
        "Offline: explain university rankings",
        "Don't browse, explain the latest document I uploaded",
        "Without internet, tell me about weather patterns",
        "検索しないで天気の仕組みを説明して",
        "不要联网，只解释大学排名是什么",
        "只用知识库回答最新大学排名",
        "Only use my knowledge base for the latest rankings",
    ],
)
def test_explicit_offline_instructions_prevent_automatic_search(question: str) -> None:
    assert not wants_web(question)


def test_edit_branch_keeps_prefix_and_original_and_refuses_cross_session(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "history.db")
    store.start()
    history = TranscriptService(store)
    history.start()
    original = history.new_session("原记录")
    history.append(original, "user", "前文")
    history.append(original, "assistant", "前答")
    history.append(original, "user", "要修改")
    history.append(original, "assistant", "旧回答")
    rows = history.whole_session(original)
    branch = history.fork_before(original, rows[2]["id"], "新问题")
    assert branch and branch != original
    assert [row["content"] for row in history.whole_session(branch)] == ["前文", "前答"]
    assert len(history.whole_session(original)) == 4
    assert history.fork_before(branch, rows[2]["id"], "越界") == ""
    assert history.fork_before(original, rows[1]["id"], "不能改助手") == ""
    store.stop()


def test_rejected_generation_does_not_revoke_stop_or_change_active_phase() -> None:
    service = ImageService(None, None)  # type: ignore[arg-type]
    service.generation_lock.acquire()
    service._cancel.set()
    service._phase = "running"
    try:
        assert not service.generate("the cat")["ok"]
        assert service._cancel.is_set()
        assert service._phase == "running"
    finally:
        service.generation_lock.release()


def test_cancel_from_an_old_chat_cannot_cancel_another_owner() -> None:
    service = ImageService(None, None)  # type: ignore[arg-type]
    service._generation_owner = 42
    service.generation_lock.acquire()
    try:
        assert "不属于" in service.cancel(expected_owner=7)
        assert not service._cancel.is_set()
    finally:
        service.generation_lock.release()


def test_owned_gpu_lease_serializes_chat_and_media(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from jarvis.llm import local_server

    events: list[str] = []
    model = SimpleNamespace(
        _lock=threading.RLock(),
        _busy=0,
        stop=lambda **_kwargs: events.append("release-owned-model"),
    )
    monkeypatch.setattr(local_server, "shared_server", lambda: model)
    attempted, entered = threading.Event(), threading.Event()

    def infer() -> None:
        attempted.set()
        with local_server.resource_lock():
            entered.set()

    with local_server.media_session():
        worker = threading.Thread(target=infer)
        worker.start()
        assert attempted.wait(1)
        assert not entered.wait(0.1)
    assert entered.wait(1)
    worker.join(1)
    assert events == ["release-owned-model"]


def test_chat_stop_reaches_the_running_media_tool() -> None:
    import threading

    from jarvis.app.chat_service import ChatService

    started, cancelled = threading.Event(), threading.Event()
    owners = []

    class Tools:
        def invoke(self, _name: str, _arguments: object) -> SimpleNamespace:
            started.set()
            assert cancelled.wait(2), "chat stop never reached generation"
            return SimpleNamespace(as_text=lambda: "已停止生成")

        def cancel_media_for_owner(self, owner: int) -> None:
            owners.append(owner)
            cancelled.set()

    tools = Tools()
    service = ChatService(lambda: None, tool_provider=lambda: tools)  # type: ignore[arg-type,return-value]
    assert (
        service._invoke("generate_image", '{"prompt":"a cat"}', should_stop=started.is_set)
        == "已停止生成"
    )
    assert owners == [threading.get_ident()]


def test_german_and_french_are_not_accepted_as_english() -> None:
    assert not _looks_english("Eine Katze sitzt an der Wand")
    client = SimpleNamespace(
        complete=lambda *_a, **_k: SimpleNamespace(content="trois chats rouges")
    )
    with pytest.raises(TranslationError):
        LocalLlmTranslator(lambda: client).translate("三只红猫")


def test_truncated_translation_is_not_submitted() -> None:
    client = SimpleNamespace(
        complete=lambda *_a, **_k: SimpleNamespace(content="A cat", finish_reason="length")
    )
    with pytest.raises(TranslationError, match="截断"):
        LocalLlmTranslator(lambda: client).translate("一只猫")


def test_chat_gives_up_explaining_instead_of_waiting_on_the_media_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生图那一段整件持有资源锁；聊天那一发不能无超时地等它。

    同一把 RLock 在**同一个线程**里是可重入的，所以这一发必须真的来自另一个线程 ——
    在主线程里调用只会立刻拿到锁，测出来的是个假绿。
    """
    import threading
    import time

    from jarvis.llm import local_server, openai_compat
    from jarvis.llm.openai_compat import OpenAiCompatClient, OpenAiCompatSettings
    from jarvis.llm.types import ChatMessage

    monkeypatch.setattr(local_server, "shared_server", lambda: SimpleNamespace())
    monkeypatch.setattr(openai_compat, "_MEDIA_HANDOFF_SECONDS", 0.2)
    client = OpenAiCompatClient(
        OpenAiCompatSettings(
            provider_name="local",
            base_url="http://127.0.0.1:1/v1",
            model="qwen",
            api_key_env="",
            timeout_seconds=5.0,
            max_retries=0,
            retry_backoff_seconds=0.0,
            bundled=True,
            managed_resources=True,
        ),
        transport=SimpleNamespace(post_json=lambda *_a, **_k: pytest.fail("不该发出请求")),
        environ={},
    )

    caught: list[BaseException] = []
    started = time.monotonic()
    with local_server.resource_lock():  # 模拟媒体那一件正在占着这张卡

        def ask() -> None:
            try:
                client.complete([ChatMessage.user("你好")])
            except BaseException as exc:  # 断言在主线做，工作线程只负责把异常带回来
                caught.append(exc)

        worker = threading.Thread(target=ask)
        worker.start()
        worker.join(10)

    assert caught and not worker.is_alive(), "那一发还卡在锁上，没退出来"
    assert isinstance(caught[0], LlmConnectionError)
    assert "显卡" in str(caught[0]), "话要说清是谁占着，光一句失败没法照着做"
    assert time.monotonic() - started < 5, "等满 20 秒才算失败的话，用户只会当成断线"


@pytest.mark.parametrize(
    "total,free,refuses",
    [
        (4096, 3500, False),  # 本机 1650 空着的时候
        (4096, 900, True),  # 4GB 卡只剩 900MB：这一发大概率半途 OOM
        (16384, 1200, False),  # 大卡只留 1.2GB 也够，门槛是地板不是按比例抬
        (0, 0, False),  # 探针没报出显存 —— 不能拿"量不到"当"不够"来拒人
    ],
)
def test_the_vram_floor_only_refuses_when_it_actually_measured_one(
    total: float, free: float, refuses: bool
) -> None:
    """这一道是新加的拒绝路径：判错方向的表现是"以前能出图，现在不让出图"。"""
    probe = {"vram_total_mb": total, "vram_free_mb": free}
    message = ImageService.gpu_budget_error(probe)  # type: ignore[arg-type]
    assert bool(message) is refuses
    if refuses:
        assert "显存" in message and "小夜不会结束" in message


def test_buyer_environment_ignores_author_home_and_keys(tmp_path: Path) -> None:
    env = {
        "JARVIS_HOME": "X:/author",
        "OPENAI_API_KEY": "test-secret",
        "HF_HOME": "X:/cache",
        "SystemDrive": "Z:",
        "PATH": "system-runtime",
    }
    root = prepare_buyer_environment(env, tmp_path)
    assert root == tmp_path / "用户数据"
    assert env["JARVIS_HOME"] == str(root)
    assert env["HF_HOME"].startswith(str(root))
    assert "OPENAI_API_KEY" not in env
    assert env["PATH"] == "system-runtime"
    assert (root / "install.json").is_file()


@pytest.mark.parametrize(
    "question", ["美国前十的大学有哪些", "请联网探索量子计算", "最新发布了哪些版本"]
)
def test_fresh_information_requests_are_researched(question: str) -> None:
    assert wants_web(question)


@pytest.mark.parametrize(
    "question", ["今天几号", "只用资料回答美国大学排名", "不要联网，解释排序算法"]
)
def test_offline_and_clock_questions_do_not_trigger_search(question: str) -> None:
    assert not wants_web(question)


def test_rss_keeps_real_links_and_refuses_dtd() -> None:
    page = (
        "<rss><channel><item><title>University ranking</title>"
        "<link>https://example.org/ranking</link>"
        "<description>Ten universities</description></item></channel></rss>"
    )
    assert _parse_rss(page, 3)[0][1] == "https://example.org/ranking"
    assert _parse_rss("<!DOCTYPE x [ ]>" + page, 3) == []
    assert public_sources("https://example.org/ranking https://example.org/?token=private") == (
        "https://example.org/ranking",
    )


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows DPAPI")
def test_buyer_credentials_are_encrypted_and_round_trip(tmp_path: Path) -> None:
    from jarvis.config.credentials import read_credentials, write_credential

    write_credential(tmp_path, "TEST_API_KEY", "synthetic-key-for-test-only")
    assert "synthetic-key-for-test-only" not in (tmp_path / "credentials.dpapi.json").read_text()
    assert read_credentials(tmp_path)["TEST_API_KEY"] == "synthetic-key-for-test-only"
    write_credential(tmp_path, "TEST_API_KEY", None)
    assert read_credentials(tmp_path) == {}


def test_video_stream_is_scoped_and_supports_ranges(tmp_path: Path) -> None:
    import urllib.error
    import urllib.request

    from jarvis.ui.static_server import BundleServer

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0123456789" * 10000)
    with BundleServer(tmp_path) as server:
        server.media.validate = lambda path: path == video
        url = server.url + server.media.register(video)
        request = urllib.request.Request(url, headers={"Range": "bytes=100-109"})
        with urllib.request.urlopen(request, timeout=3) as reply:
            assert reply.status == 206
            assert reply.headers["Content-Range"] == "bytes 100-109/100000"
            assert reply.read() == b"0123456789"
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(server.url + "media/unknown", timeout=3)
        assert error.value.code == 404
        server.media.validate = lambda _path: False
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(url, timeout=3)
