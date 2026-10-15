"""Real local inference + key-free web + persistent knowledge, in an isolated D: home."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME = Path("D:/AI/XiaoYe/qa/buyer-chat-r119") / time.strftime("%Y%m%d-%H%M%S")
HOME.mkdir(parents=True, exist_ok=True)
os.environ["JARVIS_HOME"] = str(HOME)

from jarvis.app.chat_service import ChatService, ConversationTuning, ThinkingRequest  # noqa: E402
from jarvis.app.knowledge_growth import KnowledgeGrowth  # noqa: E402
from jarvis.app.media_prompt import PromptTranslator  # noqa: E402
from jarvis.app.preferences import Preferences  # noqa: E402
from jarvis.app.transcript_service import TranscriptService  # noqa: E402
from jarvis.config.loader import load_defaults  # noqa: E402
from jarvis.config.schema import AppConfig  # noqa: E402
from jarvis.database import SqliteStore  # noqa: E402
from jarvis.knowledge.service import KnowledgeService  # noqa: E402
from jarvis.llm import local_server  # noqa: E402
from jarvis.llm.service import LlmService  # noqa: E402
from jarvis.tools.disk_cleaner import DiskCleaner  # noqa: E402
from jarvis.tools.monitor import SystemMonitor  # noqa: E402
from jarvis.tools.service import ToolService  # noqa: E402
from jarvis.vector import HashingEmbedder, VectorService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", nargs="?", default="Qwen3-0.6B-Q8_0.gguf")
    parser.add_argument(
        "--question",
        default="请联网查询美国前十的大学有哪些，说明榜单来源和年份，不确定的部分直说。",
    )
    parser.add_argument("--skip-translation", action="store_true")
    args = parser.parse_args()
    config = AppConfig.from_mapping(load_defaults())
    model = args.model
    local_server.configure(local_server.Tuning(context=4096, idle_seconds=0, model=model))
    store = SqliteStore(HOME / "chat.db")
    store.start()
    vector = VectorService(store, lambda: config.vector, embedder=HashingEmbedder(dimension=512))
    vector.start()
    notes = HOME / "notes"
    notes.mkdir(exist_ok=True)
    knowledge = KnowledgeService(store, vector, lambda: config.knowledge, notes_dir=notes)
    knowledge.start()
    prefs = Preferences(HOME / "preferences.json")
    growth = KnowledgeGrowth(knowledge, prefs, min_answer_chars=30)
    tools = ToolService(
        lambda: config.tools,
        monitor_factory=lambda: SystemMonitor(top_processes=0),
        cleaner_factory=lambda: DiskCleaner(HOME / "audit.jsonl"),
    )
    tools.start()
    transcript = TranscriptService(store)
    transcript.start()
    llm = LlmService(lambda: config.llm)
    llm.start()
    started = time.monotonic()
    try:
        client = llm.client_for("local")
        chat = ChatService(
            lambda: client,
            tool_provider=lambda: tools,
            knowledge_provider=lambda: knowledge,
            transcript=transcript,
            pair_provider=lambda: ("local", client.model),
            tuning_for=lambda _p, _m: ConversationTuning(thinking=ThinkingRequest(False, 0)),
            growth_provider=lambda: growth,
            learn_spawn=lambda action: action(),
            max_tool_rounds=0,
        )
        chat.start()
        reply = chat.ask(args.question)
        rows = transcript.whole_session(chat.session_id)
        branch = (
            chat.fork_question(chat.session_id, rows[0]["id"], "只解释什么是大学排名")
            if rows
            else ""
        )
        translator = PromptTranslator(
            environ={},
            client_provider=lambda: client,
        )
        translations = []
        for text in (
            ()
            if args.skip_translation
            else (
                "三只红猫坐在蓝色长椅上，没有帽子",
                "Trois chats rouges sur un banc bleu, sans chapeau",
                "(三只红猫:1.20) 在蓝色长椅上，没有帽子",
            )
        ):
            try:
                result = translator.translate(text)
                translations.append(
                    {"input": text, "english": result.english, "provider": result.provider}
                )
            except Exception as exc:
                translations.append({"input": text, "error": str(exc)})
        # Reopen the actual database and index, then use a fresh conversation:
        # a row count alone cannot prove the assistant can use what it learned.
        chat.stop()
        knowledge.stop()
        vector.stop()
        store.stop()
        store.start()
        vector.start()
        knowledge.start()
        persisted = knowledge.retrieve("美国大学排名")
        followup_chat = ChatService(
            lambda: client,
            knowledge_provider=lambda: knowledge,
            pair_provider=lambda: ("local", client.model),
            tuning_for=lambda _p, _m: ConversationTuning(thinking=ThinkingRequest(False, 0)),
            max_tool_rounds=0,
        )
        followup_chat.start()
        followup = followup_chat.ask(
            "不要联网，仅根据知识库，列出美国大学排名资料的出处和查询时间。"
        )
        followup_chat.stop()
        report = {
            "model": model,
            "seconds": round(time.monotonic() - started, 1),
            "reply": reply.to_dict(),
            "knowledge_documents": knowledge.documents(),
            "branch": branch,
            "branch_messages": transcript.whole_session(branch) if branch else [],
            "translations": translations,
            "reopened_hits": [hit.to_dict() for hit in persisted],
            "offline_followup": followup.to_dict(),
            "offline_sources": list(followup.sources),
        }
        (HOME / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        ok = (
            not reply.error
            and "search_web" in reply.tools_used
            and bool(branch)
            and not report["branch_messages"]
            and bool(report["knowledge_documents"])
            and bool(persisted)
            and not followup.error
            and bool(followup.sources)
            and "search_web" not in followup.tools_used
        )
        print(
            json.dumps(
                {"ok": ok, "seconds": report["seconds"], "report": str(HOME / "result.json")},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0 if ok else 1
    finally:
        local_server.shared_server().stop(reason="isolated buyer verification complete")
        llm.stop()
        tools.stop()
        knowledge.stop()
        vector.stop()
        store.stop()


if __name__ == "__main__":
    raise SystemExit(main())
