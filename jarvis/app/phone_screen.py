"""她看手机那一屏：先问手机自己，再决定要不要动模型。

2026-10-10 在 vivo V2309A 上实测过三条路的形状和代价：

* ``dumpsys window`` 给当前 App：``mCurrentFocus=Window{... 包名/Activity ...}``；
* ``uiautomator dump /dev/tty`` 约 2.5 秒、43 KB XML，那一屏读到 20 条文字；
* ``screencap -p`` 约 0.8 秒、1260x2800、596 KB PNG。

顺序是有意排的：**手机自己报上来的字**先拿 —— 不花钱、不联网、不出这台机器，
「我手机上现在是什么」绝大多数时候它就答得上来。截图只在真有人要读它的时候才拿
（本地 OCR 装了、或者那格开关允许发给能看图的模型），否则一次读屏白背 0.8 秒
和一张 600 KB 的图，还把手机画面留在了盘上。
"""

from __future__ import annotations

import base64
import dataclasses
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Final, Protocol

from jarvis.app.preferences import MIRROR_ALLOW_SCREEN_VISION, Preferences
from jarvis.llm import ChatMessage, LlmClient, Role

logger = logging.getLogger("jarvis.app.phone_screen")

_MAX_WIDGETS: Final = 40
"""喂给模型的文字条数上限。一屏无障碍树能有 60 条，全塞进去只是把上下文吃掉。"""

_MAX_FRAME_BYTES: Final = 4 * 1024 * 1024
"""超过这个尺寸就不发图。Base64 之后还要再胀三分之一，不该拿它去撞请求上限。"""

_VISION_PROMPT: Final = (
    "这是我手机屏幕的一整屏截图（电脑通过投屏连接读到的）。"
    "用中文说清：这是什么界面、上面最重要的内容是什么、用户在这儿能做什么。"
    "截图里没有的东西不要猜。"
)


class PhoneMirror(Protocol):
    """The mirror service, as far as this reader needs it."""

    def read_screen(self) -> Mapping[str, object]: ...

    def capture_frame(self) -> tuple[bytes, str]: ...


class OcrEngine(Protocol):
    """``jarvis.ocr.OcrService``: bytes in, recognised text out.

    ``running`` is that service's real spelling (an engine that was never built, or
    turned off in ``config.yaml``, leaves it ``False``) -- a protocol named
    ``available`` would have passed the tests here and raised ``AttributeError`` in
    the product, which is the failure mode this layer keeps getting billed for.
    """

    @property
    def running(self) -> bool: ...

    def recognize(self, image: bytes) -> object: ...


@dataclasses.dataclass(frozen=True, slots=True)
class PhoneScreen:
    """One reading of the phone's screen, with every source accounted for."""

    app: str
    activity: str
    widgets: tuple[str, ...]
    ocr: str
    caption: str
    sources: tuple[str, ...]
    error: str

    @property
    def usable(self) -> bool:
        """Anything at all came back?"""
        return bool(self.app or self.widgets or self.ocr or self.caption)

    def to_text(self) -> str:
        """The paragraph she gets from the tool: plain Chinese, no JSON."""
        lines: list[str] = []
        if self.app:
            where = self.app + (f"/{self.activity}" if self.activity else "")
            lines.append(f"手机上现在是：{where}")
        if self.widgets:
            lines.append("屏幕上的文字（手机自己报的，按出现顺序）：")
            lines.append("｜".join(self.widgets))
        if self.ocr:
            lines.append(f"截图上识到的字：{self.ocr}")
        if self.caption:
            lines.append(f"看图模型说的：{self.caption}")
        if not lines:
            lines.append(f"这一屏我没读出来：{self.error or self.note()}")
        lines.append("来源：" + "；".join(self.sources) if self.sources else "来源：没有可读的来源")
        if self.error:
            lines.append(f"注意：{self.error}")
        elif not self.widgets and self.app:
            lines.append("（这一屏没报出文字：可能是视频、游戏或自己画的界面）")
        return "\n".join(lines)

    def note(self) -> str:
        return "；".join(self.sources) if self.sources else "没有消息"


class PhoneScreenReader:
    """把手机那一屏读成一段她可以直接引用的话。绝不抛异常。"""

    name = "phone_screen"

    def __init__(
        self,
        mirror: PhoneMirror,
        *,
        ocr_provider: Callable[[], OcrEngine | None] | None = None,
        vision_provider: Callable[[], LlmClient | None] | None = None,
        vision_ready: Callable[[], bool] | None = None,
        prefs: Preferences | None = None,
    ) -> None:
        """Create the reader.

        Args:
            mirror: The same object the panel and the mirror tools drive.
            ocr_provider: Builds the local OCR engine, or ``None`` when this machine
                never installed ``rapidocr-onnxruntime``. Only consulted when a frame
                is actually going to be read.
            vision_provider: Builds the chat client that would look at the picture.
            vision_ready: Whether *that* client has been measured to have eyes (设置里
                的「测一下」写的)。和上面那格开关两个都成立才发图 —— 一个没有眼睛的
                provider 只会回一句关于图片的报错，那比不回更难懂。
            prefs: Where 「允许把这块屏发给能看图的模型」 lives. ``None`` means the
                switch cannot be on —— the safe default for a headless caller.
        """
        self._mirror = mirror
        self._ocr_provider = ocr_provider
        self._vision_provider = vision_provider
        self._vision_ready = vision_ready
        self._prefs = prefs

    def vision_allowed(self) -> bool:
        """那格开关现在的样子。没有偏好文件就是关着。"""
        if self._prefs is None:
            return False
        return bool(self._prefs.flag(MIRROR_ALLOW_SCREEN_VISION, default=False))

    def set_vision_allowed(self, allow: bool) -> str:
        """勾或撤那格开关，回一句错误（空串是成功）。"""
        if self._prefs is None:
            return "这个进程没接偏好文件，这格改不了"
        if not self._prefs.set(MIRROR_ALLOW_SCREEN_VISION, bool(allow)):
            return "这一格已经生效，但没写进磁盘（重启会回到原来那样）"
        return ""

    def available(self) -> bool:
        """Cheap: is there a mirror runtime at all. Probed once at registration."""
        probe = getattr(self._mirror, "available", None)
        return bool(probe()) if callable(probe) else True

    def read(self, question: str = "") -> PhoneScreen:
        """One read. Whatever did not work is named in ``sources``, with the reason."""
        sources: list[str] = []
        raw = self._mirror.read_screen()
        error = str(raw.get("error", "") or "")
        note = str(raw.get("note", "") or "")
        app = str(raw.get("app", "") or "")
        activity = str(raw.get("activity", "") or "")
        widgets = _texts(raw.get("widgets"))
        if app:
            sources.append(f"手机自报 App：{app}")
        if widgets:
            # 数量要说读到几条，不是喂了几条：那才是这一屏真实的形状。
            cut = f"，只喂了前 {_MAX_WIDGETS} 条" if len(widgets) > _MAX_WIDGETS else ""
            sources.append(f"无障碍树：读到 {len(widgets)} 条文字{cut}")
        elif note:
            sources.append(f"无障碍树：{note}")
        if error:
            sources.append(f"读屏幕：{error}")

        frame = b""
        want_ocr, ocr_reason = self._ocr_wanted()
        want_vision, vision_reason = self._vision_allowed()
        why_not = "；".join(x for x in (ocr_reason, vision_reason) if x)
        if (want_ocr or want_vision) and not error:
            frame, shot_error = self._mirror.capture_frame()
            if shot_error:
                sources.append(f"截图：{shot_error}")
            elif len(frame) > _MAX_FRAME_BYTES:
                sources.append(f"截图：{len(frame) // 1024} KB，太大没交给模型")
                frame = b""
            else:
                sources.append(f"截图：{len(frame) // 1024} KB")
        elif why_not:
            sources.append(f"截图：没拿（{why_not}）")

        ocr_text = self._run_ocr(frame, sources) if frame and want_ocr else ""
        caption = (
            self._run_vision(frame, question, sources)
            if frame and want_vision and not ocr_text
            else ""
        )
        return PhoneScreen(
            app=app,
            activity=activity,
            widgets=widgets[:_MAX_WIDGETS],
            ocr=ocr_text,
            caption=caption,
            sources=tuple(sources),
            error=error,
        )

    def _ocr_wanted(self) -> tuple[bool, str]:
        """装了本地引擎才值得去截那张图：一次 screencap 是 0.8 秒。"""
        if self._ocr_provider is None:
            return False, ""
        engine = self._ocr_provider()
        if engine is None or not engine.running:
            return False, "本地 OCR 没在跑（要装了 rapidocr-onnxruntime 且 ocr.enabled=true）"
        return True, ""

    def _vision_allowed(self) -> tuple[bool, str]:
        """默认关着：把手机屏幕发给模型，等于把微信会话和验证码一起发出去。"""
        if self._vision_provider is None:
            return False, "没配能看图的模型"
        if self._prefs is None:
            return False, "这个进程没接偏好，那格开关也就没处勾"
        if not self._prefs.flag(MIRROR_ALLOW_SCREEN_VISION, default=False):
            return False, "没勾「允许把这块屏发给能看图的模型」"
        if self._vision_ready is not None and not self._vision_ready():
            return False, "当前模型没被量出有眼睛（设置里对它点一次「测一下」）"
        return True, ""

    def _run_ocr(self, frame: bytes, sources: list[str]) -> str:
        assert self._ocr_provider is not None
        engine = self._ocr_provider()
        if engine is None:
            sources.append("本地 OCR：这台没装上（装了 rapidocr-onnxruntime 才会走这条）")
            return ""
        try:
            result = engine.recognize(frame)
        except Exception as exc:  # OCR 坏了不能把整条路带走
            logger.warning("手机截图 OCR 失败：%s", exc)
            sources.append(f"本地 OCR：失败了（{type(exc).__name__}）")
            return ""
        text = str(getattr(result, "text", "") or "").strip()
        sources.append(f"本地 OCR：读到 {len(text)} 字" if text else "本地 OCR：图上没认出字")
        return text[:2000]

    def _run_vision(self, frame: bytes, question: str, sources: list[str]) -> str:
        assert self._vision_provider is not None
        client = self._vision_provider()
        if client is None:
            sources.append("看图模型：没配到能看图的那一档")
            return ""
        url = "data:image/png;base64," + base64.b64encode(frame).decode("ascii")
        ask = _VISION_PROMPT + (f"\n用户问的是：{question}" if question else "")
        try:
            reply = client.complete([ChatMessage(role=Role.USER, content=ask, images=(url,))])
        except Exception as exc:  # 一次调用失败不该让这条问题没有答案
            logger.warning("看图模型没答上：%s", exc)
            sources.append(f"看图模型：没答上（{type(exc).__name__}）")
            return ""
        text = str(getattr(reply, "content", "") or "").strip()
        sources.append(f"看图模型：{client.provider_name}/{client.model} 答了 {len(text)} 字")
        return text[:2000]


def _texts(value: object) -> tuple[str, ...]:
    items: Sequence[object] = value if isinstance(value, (list, tuple)) else []
    return tuple(str(item) for item in items if str(item).strip())


__all__ = ["PhoneMirror", "PhoneScreen", "PhoneScreenReader"]
