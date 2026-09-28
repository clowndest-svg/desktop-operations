"""LangGraph supervisor graph wiring the worker agents together.

The graph is the *brain* of phase 10: a supervisor node classifies the user's
intent with the LLM and routes to one of the worker agents (chat / tools).
Workers run, then control returns to the supervisor, which finishes the turn
(or falls back to the default agent when a worker declines).
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from jarvis.agent import Agent, AgentContext, AgentResult
from jarvis.agent.conversational import ConversationalAgent
from jarvis.agent.tools import ToolAgent
from jarvis.llm.client import LlmClient
from jarvis.llm.types import ChatMessage
from jarvis.orchestration.types import GraphState

logger = logging.getLogger("jarvis.orchestration.graph")

_SUPERVISOR_PROMPT = (
    "你是 JARVIS 的调度器。判断用户这句话应由哪个助手处理，"
    "只回复一个词：chat（闲聊/问答/通用对话）或 tools（查时间、算数等工具类请求）。"
    "示例：'现在几点' -> tools；'讲个笑话' -> chat。"
)

_ROUTE_CHAT = "chat"
_ROUTE_TOOLS = "tools"
_ROUTE_FINISH = "finish"


class AgentGraph:
    """Supervisor-routed multi-agent graph (LangGraph ``StateGraph``)."""

    def __init__(self, llm: LlmClient, *, default_agent: str = "chat") -> None:
        self._llm = llm
        self._default_agent = default_agent
        self._agents: dict[str, Agent] = {
            "chat": ConversationalAgent(llm),
            "tools": ToolAgent(),
        }
        self._compiled: Any = self._build()

    # -- public API --------------------------------------------------------

    def run(self, user_text: str, history: list[ChatMessage] | None = None) -> str:
        """Route ``user_text`` through the agents; return the reply text."""
        initial: GraphState = {
            "user_text": user_text,
            "history": list(history or []),
            "messages": list(history or []),
            "next": "",
            "trace": [],
            "final_answer": None,
        }
        result = self._compiled.invoke(initial)
        answer = result.get("final_answer")
        if not answer:
            # Last-resort fallback so the pipeline never goes silent.
            logger.warning("agent graph produced no answer; using default agent")
            answer = (
                self._agents[self._default_agent]
                .run(
                    AgentContext(
                        user_text=user_text,
                        history=history or [],
                        default_agent=self._default_agent,
                    )
                )
                .text
            )
        return answer or ""

    # -- graph construction ------------------------------------------------

    def _build(self) -> Any:
        workflow = StateGraph(GraphState)
        workflow.add_node("supervisor", self._supervisor)
        workflow.add_node("chat", self._chat)
        workflow.add_node("tools", self._tools)
        workflow.add_edge(START, "supervisor")
        workflow.add_conditional_edges(
            "supervisor",
            self._route_from_supervisor,
            {"chat": "chat", "tools": "tools", "finish": END},
        )
        workflow.add_conditional_edges(
            "chat",
            self._route_after_worker,
            {"finish": END, "tools": "tools"},
        )
        workflow.add_conditional_edges(
            "tools",
            self._route_after_tools,
            {"finish": END, "chat": "chat"},
        )
        return workflow.compile()

    # -- nodes -------------------------------------------------------------

    def _supervisor(self, state: GraphState) -> dict[str, Any]:
        if state.get("final_answer") is not None:
            return {"next": _ROUTE_FINISH}
        user_text = state.get("user_text", "")
        messages = [
            ChatMessage.system(_SUPERVISOR_PROMPT),
            ChatMessage.user(user_text),
        ]
        reply = self._llm.complete(messages).content.lower()
        if "tool" in reply:
            decision = _ROUTE_TOOLS
        elif "chat" in reply:
            decision = _ROUTE_CHAT
        else:
            decision = self._default_agent
        logger.debug("supervisor routed to: %s", decision)
        return {"next": decision}

    def _chat(self, state: GraphState) -> dict[str, Any]:
        result = self._agents["chat"].run(
            AgentContext(
                user_text=state.get("user_text", ""),
                history=state.get("history", []),
            )
        )
        return self._worker_done(state, result, "chat")

    def _tools(self, state: GraphState) -> dict[str, Any]:
        result = self._agents["tools"].run(
            AgentContext(
                user_text=state.get("user_text", ""),
                history=state.get("history", []),
            )
        )
        if not result.text:
            # Tools agent declined; the router falls back to chat.
            return {"next": _ROUTE_CHAT, "trace": [*state.get("trace", []), "tools"]}
        return self._worker_done(state, result, "tools")

    def _worker_done(self, state: GraphState, result: AgentResult, agent: str) -> dict[str, Any]:
        return {
            "final_answer": result.text,
            "next": _ROUTE_FINISH,
            "trace": [*state.get("trace", []), agent],
        }

    # -- routing -----------------------------------------------------------

    def _route_from_supervisor(self, state: GraphState) -> str:
        return state.get("next") or self._default_agent

    def _route_after_worker(self, state: GraphState) -> str:
        return state.get("next") or _ROUTE_FINISH

    def _route_after_tools(self, state: GraphState) -> str:
        return state.get("next") or _ROUTE_FINISH
