"""Tests for the multi-agent layer: conversational / tool agents and the
LangGraph supervisor graph (all deterministic via ``FakeLlmClient``).
"""

from __future__ import annotations

from jarvis.agent import AgentContext
from jarvis.agent.conversational import ConversationalAgent
from jarvis.agent.tools import ToolAgent
from jarvis.llm.types import Role
from jarvis.orchestration.graph import AgentGraph
from tests._fakes import FakeLlmClient


def test_conversational_agent_uses_llm() -> None:
    llm = FakeLlmClient(reply="你好呀")
    agent = ConversationalAgent(llm)
    result = agent.run(AgentContext(user_text="hi"))
    assert result.text == "你好呀"
    assert len(llm.calls) == 1
    msgs = llm.calls[0]
    assert msgs[0].role == Role.SYSTEM
    assert msgs[-1].role == Role.USER
    assert msgs[-1].content == "hi"


def test_tool_agent_get_time() -> None:
    agent = ToolAgent(clock=lambda: 0.0)
    result = agent.run(AgentContext(user_text="现在几点"))
    assert "当前时间" in result.text
    assert result.used_tools == ("get_time",)


def test_tool_agent_calculate() -> None:
    agent = ToolAgent()
    result = agent.run(AgentContext(user_text="计算 1+1"))
    assert "2.0" in result.text
    assert result.used_tools == ("calculate",)


def test_tool_agent_declines_unknown() -> None:
    agent = ToolAgent()
    result = agent.run(AgentContext(user_text="讲个笑话"))
    assert result.text == ""
    assert result.used_tools == ()


def test_agent_graph_routes_to_tools() -> None:
    llm = FakeLlmClient(reply="tools")
    graph = AgentGraph(llm, default_agent="chat")
    answer = graph.run("现在几点")
    assert "当前时间" in answer
    # Only the supervisor called the LLM (tools worker executes locally).
    assert len(llm.calls) == 1


def test_agent_graph_routes_to_chat() -> None:
    llm = FakeLlmClient(reply="chat")
    graph = AgentGraph(llm, default_agent="chat")
    answer = graph.run("你好")
    assert answer == "chat"


def test_agent_graph_falls_back_when_tools_decline() -> None:
    llm = FakeLlmClient(reply="tools")
    graph = AgentGraph(llm, default_agent="chat")
    # "讲个故事" is not a tool request -> tools worker declines -> chat fallback.
    answer = graph.run("讲个故事")
    assert answer == "tools"
