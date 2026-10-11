"""她自学这一层：真知识库、真偏好文件、真工具分发器 —— 只有"模型说了什么"是假的。

这一层的风险不是算错，是**污染**：自动往资料库里写东西，写进去之后每一次相关提问都会
带着它。r88 那次「离线啥都问不了」的三条真因里就有一条是资料库提示污染。所以判据分两类：

- **该存的真存了**，而且落在 `_自学/` 里、带着出处和时间 —— 人能核对；
- **不该存的一条都不许存**，并且每一次"没存"都要有一句能念给人听的原因（静默跳过
  在这个项目里已经付过很多次账）。

接线那两条用真的 `ToolService` + 真的 `search_web`（只把网络那一层换成假 opener）：
手搓一份 dict 当分发器会跳过参数校验，而"她到底有没有真去搜"正是这条链的要害。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.app.chat_service import ChatService
from jarvis.app.knowledge_growth import (
    AUTO_COUNT,
    AUTO_LEARN,
    AUTO_SEEN,
    LEARNED_FOLDER,
    KnowledgeGrowth,
    TurnEvidence,
    fingerprint,
)
from jarvis.app.preferences import Preferences
from jarvis.config.schema import KnowledgeSection, ToolsSection, VectorSection
from jarvis.database import SqliteStore
from jarvis.knowledge.service import KnowledgeService
from jarvis.llm.types import ChatMessage, ChatResponse, GenerationOptions, ToolCall
from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor
from jarvis.tools.service import ToolService
from jarvis.vector import HashingEmbedder, VectorService

ANSWER = (
    "Qwen3-4B 原生支持 32768 token 的上下文，开启 thinking 之后可以扩到 131072；"
    "但随包的 llama.cpp 那一份在 defaults.yaml 里默认只给 4096，所以真正卡住的是界面上"
    "那一格，不是模型本身。要改就在设置里把上下文调到 16384 起，本地 4B 那一次大约要"
    "多等十几秒首字，4K 的窗口装不下整张工具清单（r88 量过：34 个工具时工具表就 4639 token）。"
)
HIT_A = "Qwen3 技术报告 https://qwen.readthedocs.io 摘要：原生 32K，可扩展到 131072。"


# --------------------------------------------------------------------------
# 假体：只替"网络"和"模型"，其余全是真的
# --------------------------------------------------------------------------


class _Response:
    def __init__(self, body: bytes, content_type: str = "text/html", url: str = "") -> None:
        self._body, self._ct, self._url = body, content_type, url

    def read(self, _limit: int = -1) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url

    @property
    def headers(self) -> Any:
        outer = self

        class _H:
            def get_content_type(self) -> str:
                return outer._ct

        return _H()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_a: object) -> None:
        return None


def _bing_page(titles: Sequence[str]) -> bytes:
    """像真页：出处在 ``<cite>`` 里（Bing 的 href 是加密跳转，解析器故意不读它），
    摘要在 ``class="b_…"`` 的 ``<p>`` 里。少一个，工具的出处那一栏就是空的。"""
    rows = "".join(
        f'<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?p={n}">{title}</a></h2>'
        f"<cite>https://example.org › doc{n}</cite>"
        f'<p class="b_lineclamp2">这是 {title} 的一段摘要，够长才不会被解析丢掉。</p></li>'
        for n, title in enumerate(titles, start=1)
    )
    return f"<html><body><ol id='b_results'>{rows}</ol></body></html>".encode()


class _Opener:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.urls: list[str] = []

    def open(self, request: Any, timeout: float = 0.0) -> _Response:
        url = getattr(request, "full_url", str(request))
        self.urls.append(url)
        return _Response(self.body, url=url.split("?")[0])


class _AsksTheWeb:
    """第一轮要 `search_web`，第二轮把工具给的回话复述一遍。"""

    provider_name = "fake"
    model = "fake"

    def __init__(self, answer: str = ANSWER) -> None:
        self._round = 0
        self._answer = answer

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> ChatResponse:
        del options
        self._round += 1
        if self._round == 1:
            return ChatResponse(
                content="",
                model="fake",
                tool_calls=(
                    ToolCall(id="c1", name="search_web", arguments='{"query": "Qwen3 上下文长度"}'),
                ),
            )
        evidence = messages[-1].content if messages else ""
        return ChatResponse(content=f"{self._answer}\n（依据：{evidence[:80]}）", model="fake")

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> Any:
        """这一层不流式：`stream` 存在只是为了满足 LlmClient 协议。"""
        del messages, options
        raise AssertionError("这条测试走的是 complete")


class _Silent:
    """一轮就答完，没上过网。"""

    provider_name = "fake"
    model = "fake"

    def __init__(self, answer: str = ANSWER) -> None:
        self._answer = answer

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> ChatResponse:
        del options, messages
        return ChatResponse(content=self._answer, model="fake")

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> Any:
        del messages, options
        raise AssertionError("这条测试走的是 complete")


# --------------------------------------------------------------------------
# 真件：知识库、偏好、工具分发器
# --------------------------------------------------------------------------


def _prefs(tmp_path: Path) -> Preferences:
    return Preferences(tmp_path / "preferences.json")


def _knowledge(tmp_path: Path) -> KnowledgeService:
    store = SqliteStore(tmp_path / "kb.db")
    store.start()
    section = VectorSection.from_mapping(
        {
            "embedding": {
                "engine": "hashing",
                "dimension": 512,
                "base_url": "https://x.test/v1",
                "model": "m",
                "api_key_env": "NOPE",
                "timeout_seconds": 5.0,
                "batch_size": 8,
            }
        }
    )
    vector = VectorService(store, lambda: section, embedder=HashingEmbedder(dimension=512))
    vector.start()
    notes = tmp_path / "知识库"
    notes.mkdir(parents=True, exist_ok=True)
    service = KnowledgeService(
        store,
        vector,
        lambda: KnowledgeSection.from_mapping(
            {
                "enabled": True,
                "chunk_size": 600,
                "chunk_overlap": 120,
                "top_k": 5,
                "min_score": 0.1,
                "max_file_mb": 20,
            }
        ),
        notes_dir=notes,
    )
    service.start()
    return service


def _tools(tmp_path: Path, opener: _Opener) -> ToolService:
    service = ToolService(
        lambda: ToolsSection.from_mapping(
            {
                "enabled": True,
                "confirm_dangerous": True,
                "allow_write": False,
                "allow_shell": False,
                "file_roots": [],
                "max_result_chars": 8000,
            }
        ),
        monitor_factory=lambda: SystemMonitor(top_processes=0),
        cleaner_factory=lambda: DiskCleaner(tmp_path / "deletions.jsonl"),
        web_opener=opener,
    )
    service.start()
    return service


def _evidence(
    tools: tuple[str, ...] = ("search_web",), hits: tuple[str, ...] = (HIT_A,)
) -> TurnEvidence:
    return TurnEvidence(
        question="Qwen3 的上下文有多长？", answer=ANSWER, tools=tools, web_results=hits
    )


# --------------------------------------------------------------------------


class TestWhatSheKeeps:
    def test_a_turn_that_never_went_online_teaches_her_nothing(self, tmp_path: Path) -> None:
        growth = KnowledgeGrowth(_knowledge(tmp_path), _prefs(tmp_path))
        outcome = growth.consider(_evidence(tools=(), hits=()))
        assert outcome["stored"] is False
        assert "没上网" in outcome["reason"], "拒绝的原因要能念给人听，不是 skipped"

    def test_a_searched_turn_becomes_a_note_with_its_source(self, tmp_path: Path) -> None:
        knowledge = _knowledge(tmp_path)
        growth = KnowledgeGrowth(knowledge, _prefs(tmp_path))
        outcome = growth.consider(_evidence())
        assert outcome["stored"] is True, outcome["reason"]
        rows = [dict(row) for row in knowledge.documents()]
        assert any(LEARNED_FOLDER in str(row["source"]) for row in rows), "自学条目没落在 _自学 里"
        page = next((tmp_path / "知识库").rglob("*.md"))
        body = page.read_text(encoding="utf-8")
        assert "来源：自学·联网搜索" in body, "没记来源，下次就没法判断该不该信它"
        assert HIT_A in body
        assert "可能已经过期" in body

    def test_the_same_question_asked_twice_is_stored_once(self, tmp_path: Path) -> None:
        knowledge = _knowledge(tmp_path)
        growth = KnowledgeGrowth(knowledge, _prefs(tmp_path))
        assert growth.consider(_evidence())["stored"] is True
        again = growth.consider(
            TurnEvidence(
                question="Qwen3 的上下文有多长",  # 少了个问号，别的话都一样
                answer=ANSWER,
                tools=("search_web",),
                web_results=(HIT_A,),
            )
        )
        assert again["stored"] is False
        assert "已经存过" in again["reason"]
        assert len(list((tmp_path / "知识库").rglob("*.md"))) == 1

    def test_a_short_answer_is_not_worth_a_note(self, tmp_path: Path) -> None:
        growth = KnowledgeGrowth(_knowledge(tmp_path), _prefs(tmp_path))
        outcome = growth.consider(
            TurnEvidence(
                question="Qwen3 的上下文有多长？",
                answer="太短了",
                tools=("search_web",),
                web_results=(HIT_A,),
            )
        )
        assert outcome["stored"] is False
        assert "太短" in outcome["reason"]

    def test_a_failed_turn_is_never_learned_as_fact(self, tmp_path: Path) -> None:
        """把"没答上"存进资料库，等于让下一次提问带着一个错前提。"""
        growth = KnowledgeGrowth(_knowledge(tmp_path), _prefs(tmp_path))
        outcome = growth.consider(
            TurnEvidence(
                question="Qwen3 的上下文有多长？",
                answer="[请求失败] 连不上 127.0.0.1:28181，本机那个 llama-server 没有答话；"
                "这不算一个回答，只是一次没做成的尝试。之所以要单独判这一条，是因为它的"
                "字数完全可能超过「太短」那道门槛（带上端口号、两个地址和一句下一步建议就"
                "超了），而把一次失败存进资料库，下一次同类提问就会带着这个错前提去回答。",
                tools=("search_web",),
                web_results=(HIT_A,),
            )
        )
        assert outcome["stored"] is False and "没答上" in outcome["reason"]

    def test_the_daily_cap_is_honoured_and_says_the_number(self, tmp_path: Path) -> None:
        prefs = _prefs(tmp_path)
        prefs.set(AUTO_COUNT, {"day": _today(), "count": 2})
        growth = KnowledgeGrowth(_knowledge(tmp_path), prefs, limit_per_day=2)
        outcome = growth.consider(_evidence())
        assert outcome["stored"] is False
        assert "2" in outcome["reason"] and "上限" in outcome["reason"]


class TestTheSwitchAndThePurge:
    def test_turning_the_switch_off_stores_nothing(self, tmp_path: Path) -> None:
        knowledge, prefs = _knowledge(tmp_path), _prefs(tmp_path)
        growth = KnowledgeGrowth(knowledge, prefs)
        assert growth.enabled() is True, "默认必须是开 —— 关掉的默认识别等于没有这条能力"
        growth.set_enabled(False)
        outcome = growth.consider(_evidence())
        assert outcome["stored"] is False and "关掉" in outcome["reason"]

    def test_forget_learned_leaves_human_material_alone(self, tmp_path: Path) -> None:
        knowledge = _knowledge(tmp_path)
        fed = tmp_path / "知识库" / "人喂的手册.md"
        fed.write_text(
            "# 人喂的手册\n\n这段是用户自己导入的资料，很长一段内容用来占位。", encoding="utf-8"
        )
        knowledge.ingest(fed, force=True)
        growth = KnowledgeGrowth(knowledge, _prefs(tmp_path))
        growth.consider(_evidence())
        assert len(list((tmp_path / "知识库").rglob("*.md"))) == 2
        removed = growth.forget_learned()
        assert removed == 1
        assert fed.is_file(), "一键清只清她自学的，人喂的一条都不许动"
        assert not list((tmp_path / "知识库" / LEARNED_FOLDER).rglob("*.md")), "索引删了文件还留着"
        first = dict(next(iter(knowledge.documents())))
        assert str(first.get("title") or "").startswith("人喂")

    def test_stats_says_what_the_panel_needs(self, tmp_path: Path) -> None:
        growth = KnowledgeGrowth(_knowledge(tmp_path), _prefs(tmp_path))
        growth.consider(_evidence())
        stats = growth.stats()
        assert stats["enabled"] is True and stats["today"] == 1
        assert stats["folder"] == LEARNED_FOLDER and stats["has_knowledge"] is True
        # 完整路径必须由后端给：界面自己拼会拼出「数据/知识库/_自学」，
        # 而真路径是「数据/笔记/_自学」—— 屏幕上印一个猜的目录就是新一笔谎。
        assert str(stats["directory"]).endswith(LEARNED_FOLDER)

    def test_a_folder_cannot_walk_out_of_the_notes_dir(self, tmp_path: Path) -> None:
        """`folder` 是后端内部用的参数，但它必须自己兜住 —— 不能信调用方。

        判据是**文件必须落在笔记目录里面**。第一版写的是"看 `tmp_path/外泄` 在不在"，
        而没清洗时 `知识库/../../外泄` 一路走到沙箱外面 —— 那个路径压根不在 `tmp_path`
        底下，于是断言白白通过、变异活着回来。
        """
        knowledge = _knowledge(tmp_path)
        notes = tmp_path / "知识库"
        knowledge.ingest_text(
            "越界试试", "一段够长的内容用来让索引真的跑起来。", folder="../../外泄"
        )
        assert list(
            notes.rglob("越界试试*.md")
        ), "文件没落在笔记目录里 = 清洗没生效，它写到外面去了"
        assert not list(tmp_path.parent.glob("外泄")), "数据根之外多出来一个目录"

    def test_a_write_failure_is_reported_not_swallowed(self, tmp_path: Path) -> None:
        knowledge = _knowledge(tmp_path)
        growth = KnowledgeGrowth(knowledge, _prefs(tmp_path))
        blocker = tmp_path / "挡路的文件"
        blocker.write_text("我不是目录", encoding="utf-8")
        knowledge._notes_dir = blocker  # 走 ingest_text 的写失败分支
        outcome = growth.consider(_evidence())
        assert outcome["stored"] is False and outcome["reason"]


class TestTheFingerprint:
    def test_punctuation_and_spaces_do_not_make_a_new_question(self) -> None:
        assert fingerprint("北京今天 天气如何？") == fingerprint("北京今天天气如何")

    def test_different_questions_stay_different(self) -> None:
        assert fingerprint("Qwen3 上下文长度") != fingerprint("Qwen3 4B 上下文长度")


class TestTheChatLoopHandsItOver:
    """端到端：真 ChatService + 真 ToolService + 真 search_web（只换掉网络）。"""

    def _service(
        self, tmp_path: Path, growth: KnowledgeGrowth, spawn: list[Any]
    ) -> tuple[ChatService, ToolService]:
        tools = _tools(tmp_path, _Opener(_bing_page(["Qwen3 上下文"])))
        chat = ChatService(
            lambda: _AsksTheWeb(),
            tool_provider=lambda: tools,
            growth_provider=lambda: growth,
            learn_spawn=spawn.append,
            session_id="t",
        )
        chat.start()
        return chat, tools

    def test_a_searched_answer_reaches_the_knowledge_base(self, tmp_path: Path) -> None:
        knowledge = _knowledge(tmp_path)
        growth = KnowledgeGrowth(knowledge, _prefs(tmp_path))
        queued: list[Any] = []
        chat, tools = self._service(tmp_path, growth, queued)
        try:
            reply = chat.ask("Qwen3 的上下文有多长？")
            assert reply.error == "", reply.error
            assert "search_web" in reply.tools_used, "她根本没去搜，这条测试就白写了"
            assert queued, "回答都回来了，自学却没被安排"
            queued[0]()  # 真去执行那条"后台"任务
            pages = list((tmp_path / "知识库").rglob("*.md"))
            assert pages, "搜过了却没存下来"
            body = pages[0].read_text(encoding="utf-8")
            assert "Qwen3" in body
            # 判据必须是"出处那一段在不在"，不能是"含不含 example.org" ——
            # 假模型第二轮会把工具回话复述进**回答本身**，那个词从回答里就能命中，
            # 于是"没收集回话"这条变异能活着（第一版就是这么绿的）。
            tail = body.split("依据（", 1)
            assert len(tail) == 2, "笔记里没有「依据」那一段 = 出处根本没进来"
            assert "search_web：" in tail[1] and "example.org" in tail[1]
        finally:
            tools.stop()
            chat.stop()

    def test_learning_never_delays_the_answer(self, tmp_path: Path) -> None:
        """回答先返回，学习排在后面 —— 索引一条笔记要做切块和向量化。"""
        knowledge = _knowledge(tmp_path)
        growth = KnowledgeGrowth(knowledge, _prefs(tmp_path))
        ran: list[str] = []
        tools = _tools(tmp_path, _Opener(_bing_page(["Qwen3 上下文"])))
        chat = ChatService(
            lambda: _AsksTheWeb(),
            tool_provider=lambda: tools,
            growth_provider=lambda: growth,
            learn_spawn=lambda run: ran.append("queued"),  # 不执行，只登记
            session_id="t",
        )
        chat.start()
        try:
            reply = chat.ask("Qwen3 的上下文有多长？")
            assert reply.answer
            assert ran == ["queued"] and len(list((tmp_path / "知识库").rglob("*.md"))) == 0
        finally:
            tools.stop()
            chat.stop()

    def test_a_turn_with_no_web_never_bothers_the_growth_layer(self, tmp_path: Path) -> None:
        calls: list[Any] = []

        def spy() -> Any:
            class _Spy:
                def consider(self, evidence: TurnEvidence) -> dict[str, object]:
                    calls.append(evidence)
                    return {"stored": False, "reason": "", "title": ""}

            return _Spy()

        tools = _tools(tmp_path, _Opener(_bing_page(["x"])))
        chat = ChatService(
            lambda: _Silent(),
            tool_provider=lambda: tools,
            growth_provider=spy,
            learn_spawn=lambda run: run(),
            session_id="t",
        )
        chat.start()
        try:
            chat.ask("今天几号")
            assert calls and calls[0].web_results == ()
            assert "search_web" not in calls[0].tools
        finally:
            tools.stop()
            chat.stop()


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def test_preferences_round_trip_is_plain_json(tmp_path: Path) -> None:
    """清单存的是原生 list/dict —— 自己 json.dumps 一遍再存，读出来会是字符串。"""
    prefs = _prefs(tmp_path)
    prefs.set(AUTO_SEEN, ["a", "b"])
    assert prefs.get(AUTO_SEEN, []) == ["a", "b"]
    prefs.set(AUTO_COUNT, {"day": _today(), "count": 3})
    assert json.dumps(prefs.get(AUTO_COUNT, {})).find('"count"') > 0


def test_the_switch_default_is_written_into_preferences_once(tmp_path: Path) -> None:
    prefs = _prefs(tmp_path)
    assert (
        prefs.get(AUTO_LEARN, None) is None
    ), "默认值不该提前落盘，落盘了用户就分不清'没设过'和'设成默认'"
    KnowledgeGrowth(None, prefs).set_enabled(False)
    assert prefs.get(AUTO_LEARN, None) is False
