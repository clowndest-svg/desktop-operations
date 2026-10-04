"""语音那条路的"图"：把一次提问交给和输入框**同一个** agent。

为什么要有这个文件
------------------
桌面上一度存在两套会回答的东西：输入框走 :class:`~jarvis.app.chat_service.ChatService`
（真工具表、function calling、33 个工具），而麦克风走
:class:`~jarvis.orchestration.graph.AgentGraph` —— 一个先让模型猜一次"这题要不要用工具"
的调度图，它那个 tools worker 只有两个硬编码能力（查时间、算算术）。
后果不是"语音偶尔不灵"，而是**同一个问题问两遍会得到两种能力**：打字问"本机有没有装
Java" 会真去跑命令，开口问同一句只会得到"我没法直接查看你电脑上的 Java 环境"——
因为那句话被分给了一个连工具都没有的 worker，而它说的确实是实话。

这里不再抄第二套工具循环（那正是当初造成分叉的做法）：语音问的那一句，
和打字问的那一句，进的是同一个 :meth:`ChatService.ask`。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from jarvis.app.chat_service import ChatService
from jarvis.llm.types import ChatMessage

logger = logging.getLogger("jarvis.app.voice_graph")


class ChatGraph:
    """The voice pipeline's ``graph`` port: ``run(text, history) -> answer``.

    ``history`` is accepted and deliberately ignored. The pipeline keeps a list of
    the turns it has seen, but so does ChatService -- and ChatService's copy is the
    one that survives a restart, is shown in 历史, and already contains the typed
    turns. Two histories answering the same question is how a conversation starts
    contradicting itself, so there is one: the store's.
    """

    def __init__(self, chat: ChatService) -> None:
        self._chat = chat

    def run(self, user_text: str, history: Sequence[ChatMessage] | None = None) -> str:
        """Answer one spoken turn. Never raises: the caller is a capture thread.

        An empty answer with an error beside it means the model was unreachable, the
        key is missing, or a tool refused. Saying that out loud is the right failure --
        silence is what the operator cannot debug, and the pipeline speaks whatever
        string comes back.
        """
        reply = self._chat.ask(user_text)
        if reply.answer.strip():
            return reply.answer
        if reply.error:
            logger.warning("voice turn had no answer (%s)", reply.error)
            return reply.error
        return ""
