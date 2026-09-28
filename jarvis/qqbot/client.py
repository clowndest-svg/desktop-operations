"""Official Tencent QQ bot event bridge to JARVIS's LLM layer."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import cast

import botpy
from botpy.message import C2CMessage, DirectMessage, GroupMessage, Message

from jarvis.config.schema import LlmSection
from jarvis.llm import ChatMessage, LlmError, LlmService
from jarvis.qqbot.settings import QqBotSettings

logger = logging.getLogger("jarvis.qqbot")

_ReplyFn = Callable[..., Awaitable[object]]


class QqBotClient(botpy.Client):  # type: ignore[misc]
    """Receive QQ messages and answer through the configured LLM provider."""

    def __init__(self, settings: QqBotSettings) -> None:
        intents = botpy.Intents(
            public_messages=True,
            public_guild_messages=True,
            direct_message=True,
        )
        super().__init__(intents=intents)
        self._settings = settings
        self._llm_service = LlmService(
            lambda: self._llm_section(),
            environ={"QQBOT_LLM_API_KEY": settings.llm_api_key},
        )
        self._histories: OrderedDict[str, list[ChatMessage]] = OrderedDict()
        self._seen_ids: OrderedDict[str, None] = OrderedDict()

    def _llm_section(self) -> LlmSection:
        return LlmSection.from_mapping(
            {
                "default_provider": "qqbot",
                "timeout_seconds": self._settings.request_timeout_seconds,
                "max_retries": 2,
                "retry_backoff_seconds": 0.5,
                "providers": {
                    "qqbot": {
                        "base_url": self._settings.llm_base_url,
                        "model": self._settings.llm_model,
                        "api_key_env": "QQBOT_LLM_API_KEY",
                        "cost_input_per_1m": 0.0,
                        "cost_output_per_1m": 0.0,
                    }
                },
            }
        )

    @property
    def name(self) -> str:
        return "qqbot"

    async def on_ready(self) -> None:
        self._llm_service.start()
        logger.info(
            "QQ bot is ready (appid=%s, model=%s)",
            self._settings.appid,
            self._settings.llm_model,
        )

    async def on_at_message_create(self, message: Message) -> None:
        await self._handle(
            scope=f"guild:{message.guild_id}:channel:{message.channel_id}:user:{message.author.id}",
            content=message.content,
            reply=cast(_ReplyFn, message.reply),
            event_id=message.id,
        )

    async def on_direct_message_create(self, message: DirectMessage) -> None:
        await self._handle(
            scope=f"dm:{message.author.id}",
            content=message.content,
            reply=cast(_ReplyFn, message.reply),
            event_id=message.id,
        )

    async def on_c2c_message_create(self, message: C2CMessage) -> None:
        await self._handle(
            scope=f"c2c:{message.author.user_openid}",
            content=message.content,
            reply=cast(_ReplyFn, message.reply),
            event_id=message.id,
        )

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        await self._handle(
            scope=f"group:{message.group_openid}:{message.author.member_openid}",
            content=message.content,
            reply=cast(_ReplyFn, message.reply),
            event_id=message.id,
        )

    async def _handle(
        self,
        *,
        scope: str,
        content: str | None,
        reply: _ReplyFn,
        event_id: str | None,
    ) -> None:
        if not content or self._already_seen(event_id):
            return
        prompt = re.sub(r"<@!?[^>]+>\s*", "", content).strip()
        if not prompt:
            await reply(content="请在 @ 机器人后输入你的问题。")
            return
        if prompt.lower() in {"/清空", "/reset", "/clear"}:
            self._histories.pop(scope, None)
            await reply(content="已清空当前会话。")
            return
        if prompt.lower() in {"/帮助", "/help"}:
            await reply(content="直接发送问题即可聊天；发送 /清空 可清除当前会话。")
            return

        history = self._histories.setdefault(
            scope,
            [ChatMessage.system(self._settings.system_prompt)],
        )
        history.append(ChatMessage.user(prompt))
        history[:] = [history[0], *history[1:][-self._settings.max_history_messages :]]
        try:
            response = await asyncio.to_thread(self._llm_service.client.complete, history)
            answer = response.content.strip() or "我暂时没有生成有效回答。"
        except LlmError as exc:
            logger.warning("LLM request failed for %s: %s", scope, exc)
            answer = "模型服务暂时不可用，请稍后再试。"
        except Exception:
            logger.exception("unexpected QQ bot handler failure for %s", scope)
            answer = "机器人暂时遇到错误，请稍后再试。"

        history.append(ChatMessage.assistant(answer))
        history[:] = [history[0], *history[1:][-self._settings.max_history_messages :]]
        await reply(content=self._trim_reply(answer))

    def _already_seen(self, event_id: str | None) -> bool:
        if not event_id:
            return False
        if event_id in self._seen_ids:
            return True
        self._seen_ids[event_id] = None
        while len(self._seen_ids) > 2048:
            self._seen_ids.popitem(last=False)
        return False

    def _trim_reply(self, text: str) -> str:
        if len(text) <= self._settings.max_reply_chars:
            return text
        return text[: self._settings.max_reply_chars - 20].rstrip() + "\n……（内容过长，已截断）"

    async def close(self) -> None:
        self._llm_service.stop()
        await super().close()
