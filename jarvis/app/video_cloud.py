"""多家可插拔的云端「文生视频」客户端：提交异步任务 → 轮询 → 下载 MP4。

为什么不做成"选一家、代码里写死它的契约"
--------------------------------------
生图那条云端能写成通用，是因为大家都兼容 OpenAI 的 ``/images/generations``。视频**没有**
这种公约数：通义万相、可灵、即梦、Vidu、MiniMax 各家的建任务地址、请求体字段名、任务 id
在哪一层、轮询地址怎么拼、成功状态叫什么、视频 URL 又藏在哪条路径下，全都不一样。写死一家，
换一家就是"界面上能选、点下去永远失败"——那是发一条假能力出去。

所以这一层把那条异步链路的**每个接口形状**都摊成可填的格子（见 :class:`CloudVideoConfig`），
默认值按阿里通义万相预置（项目里已有 ``DASHSCOPE_API_KEY``，是能真跑通的一家）。换成别家，
照着它自己的文档改这几格即可，代码一行不用动。

诚实边界：这条链路能"照着契约拼请求、按路径取值、把下载地址取回成 MP4"是通用的，
但**能不能在你的账号上跑通，取决于你填的那家是不是真是这个形状**。默认预置通义万相契约；
真实账号出片需另行实测，离线假端点通过不代表已验证真实服务商，界面按此说明。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

from jarvis.app.image_gen import _EngineError

USER_AGENT: Final = "JarvisDesktop/1.0 (+local-video)"

DEFAULT_SUBMIT_URL: Final = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis"
)
DEFAULT_KEY_ENV: Final = "DASHSCOPE_API_KEY"
DEFAULT_MODEL: Final = "wanx2.1-t2v-turbo"
# 占位符按 JSON 值插入、模板里不要再包引号：{{prompt}} 会变成 "一只橘猫在雪地跑步"，
# {{seconds}} 会变成 5。这样字符串里的引号/换行都由 json.dumps 负责转义，模板不会被撑坏。
DEFAULT_BODY: Final = (
    '{"model": {{model}}, "input": {"prompt": {{prompt}}},'
    ' "parameters": {"size": "{{width}}*{{height}}", "duration": {{seconds}}, "fps": {{fps}}}}'
)
DEFAULT_HEADERS: Final = '{"X-DashScope-Async": "enable"}'
DEFAULT_TASK_PATH: Final = "output.task_id"
DEFAULT_POLL_URL: Final = "https://dashscope.aliyuncs.com/api/v1/tasks/{{task_id}}"
DEFAULT_VIDEO_PATH: Final = "output.video_url"
DEFAULT_STATUS_PATH: Final = "output.task_status"
DEFAULT_SUCCEEDED: Final = "SUCCEEDED"
DEFAULT_RUNNING: Final = "PENDING,RUNNING"
DEFAULT_FAILED: Final = "FAILED,UNKNOWN"

CLOUD_VIDEO_DEADLINE: Final = 1800.0
POLL_INTERVAL_SECONDS: Final = 5.0


class Requester(Protocol):
    """``urlopen`` 的形状，测试注进来就不碰网络（和生图那条同一套接缝）。"""

    def __call__(self, request: urllib.request.Request, timeout: float) -> Any: ...


@dataclass(frozen=True)
class CloudVideoConfig:
    """一条异步云端视频的完整契约。字段都来自用户可填的偏好。"""

    submit_url: str
    key_env: str
    model: str
    body: str
    headers: str
    task_path: str
    poll_url: str
    video_path: str
    status_path: str
    succeeded: str
    running: str
    failed: str

    def ready(self) -> bool:
        """能开工的最小集合：有建任务地址、有取视频地址的路径。缺了就别说"配好了"。"""
        return bool(self.submit_url.strip()) and bool(self.video_path.strip())


def render_json_body(template: str, values: Mapping[str, object]) -> str:
    """把 ``{{key}}`` 替换成 ``json.dumps(values[key])``（JSON 值，不加外层引号）。

    数字、布尔直接落位；字符串带上该有的引号和转义。缺的键替换成 ``null``，让服务端
    去报"缺这个字段"，而不是在本地拼出一段坏 JSON 或在界面崩成"软件坏了"。
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return json.dumps(values.get(name), ensure_ascii=False)

    return re.sub(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", replace, template)


def render_url(template: str, values: Mapping[str, object]) -> str:
    """URL 模板：``{{key}}`` 按字面替换并做 URL 编码（这里塞的多是任务 id）。

    用 ``quote`` 而不是 ``quote_plus``：任务 id 落在**路径段**里（``/tasks/<id>``），
    空格必须编成 ``%20``；``quote_plus`` 会把空格编成 ``+``，那是查询串的规则，
    放进路径段服务端会当成字面加号，任务查不到。
    """

    def replace(match: re.Match[str]) -> str:
        return urllib.parse.quote(str(values.get(match.group(1).strip(), "")), safe="")

    return re.sub(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", replace, template)


_PATH_TOKEN = re.compile(r"\[(-?\d+)\]|([^.\[\]]+)")


def dig(payload: object, path: str) -> object:
    """按 ``output.results[0].url`` 这种点号+下标路径取值；取不到返回 ``None``，不抛。

    各家响应层级不同，硬写 ``payload["output"]["task_id"]`` 会在形状一变时抛 KeyError，
    那句又会在面板上看起来像"软件崩了"。这里一律"取不到就是没有"，交回调用方说人话。
    """
    current = payload
    for token in _PATH_TOKEN.finditer(path or ""):
        index, key = token.group(1), token.group(2)
        if index is not None:
            if not isinstance(current, (list, tuple)):
                return None
            position = int(index)
            if position < 0:
                position += len(current)
            if not 0 <= position < len(current):
                return None
            current = current[position]
        else:
            if not isinstance(current, Mapping):
                return None
            current = current.get(key or "")
            if current is None:
                return None
    return current


def _split_words(text: str) -> frozenset[str]:
    return frozenset(word.strip().upper() for word in text.split(",") if word.strip())


class CloudVideoClient:
    """用一份 :class:`CloudVideoConfig` 走完全程。网络与时间都能注入，便于离线验。"""

    def __init__(
        self,
        config: CloudVideoConfig,
        environ: Mapping[str, str],
        *,
        opener: Requester | None = None,
        sleep: Any = time.sleep,
        deadline: float = CLOUD_VIDEO_DEADLINE,
    ) -> None:
        self._config = config
        self._environ = environ
        self._opener: Requester = (
            opener if opener is not None else cast(Requester, urllib.request.urlopen)
        )
        self._sleep = sleep
        self._deadline = deadline

    def _headers(self, *, with_auth: bool = True) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        extra = self._config.headers.strip()
        if extra:
            try:
                parsed = json.loads(extra)
            except json.JSONDecodeError as exc:
                raise _EngineError(f"云端「额外请求头」不是合法 JSON：{exc}") from exc
            if isinstance(parsed, Mapping):
                headers.update({str(key): str(value) for key, value in parsed.items()})
        return headers

    def _key(self) -> str:
        key = str(self._environ.get(self._config.key_env, "")).strip()
        if not key:
            raise _EngineError(
                f"云端视频差钥匙：环境变量 {self._config.key_env} 没设"
                "（值只进环境变量，软件不落盘）。"
            )
        if not key.isascii():
            raise _EngineError(
                f"{self._config.key_env} 里有非 ASCII 字符（HTTP 头装不下）。"
                "多半是粘贴时带进了中文标点 —— 重新复制一次纯英文的钥匙。"
            )
        return key

    def _request(self, request: urllib.request.Request, timeout: float) -> object:
        try:
            with self._opener(request, timeout) as reply:
                raw = bytes(reply.read(20 * 1024 * 1024))
        except urllib.error.HTTPError as exc:
            exc.close()
            raise _EngineError(f"云端返回 HTTP {exc.code}，请检查账号、额度和接口配置") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _EngineError("连不上云端，请检查网络或稍后重试") from exc
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise _EngineError("云端响应不是有效 JSON，请检查接口配置") from exc

    def _download(self, url: str, should_stop: Callable[[], bool] | None = None) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise _EngineError(f"云端给的视频地址不是 http(s)：{url[:80]}")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self._opener(request, 120.0) as reply:
                chunks: list[bytes] = []
                total = 0
                while True:
                    if should_stop and should_stop():
                        raise _EngineError("已停止等待云端视频；服务商任务可能仍在执行")
                    chunk = bytes(reply.read(512 * 1024))
                    total += len(chunk)
                    if total > 400 * 1024 * 1024:
                        raise _EngineError("云端视频超过400MB限制，未保存残缺文件")
                    if chunk:
                        chunks.append(chunk)
                    if len(chunk) < 512 * 1024:
                        break
                blob = b"".join(chunks)
        except urllib.error.HTTPError as exc:
            raise _EngineError(f"取云端视频回了 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise _EngineError(f"取云端视频失败：{reason}") from exc
        if len(blob) < 32 or blob[4:8] != b"ftyp":
            raise _EngineError("云端下的文件不是有效 MP4（没有 ftyp 头）。")
        return blob

    def generate(
        self,
        prompt: str,
        *,
        width: int,
        height: int,
        seconds: int,
        fps: int,
        frames: int,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[bytes, dict[str, Any]]:
        """跑完整程，返回 (MP4 字节, 过程线索)。线索里带 task_id，出错时能照着查日志。"""
        config = self._config
        key = self._key()
        values: dict[str, object] = {
            "prompt": prompt,
            "model": config.model,
            "width": width,
            "height": height,
            "seconds": seconds,
            "fps": fps,
            "frames": frames,
        }
        headers = self._headers()
        headers["Authorization"] = f"Bearer {key}"
        body = render_json_body(config.body, values)
        submit = urllib.request.Request(
            config.submit_url, data=body.encode("utf-8"), headers=headers, method="POST"
        )
        submit_answer = self._request(submit, 60.0)
        trace: dict[str, Any] = {"submit": submit_answer, "task_id": "", "poll": None}

        # 同步型端点（不轮询）：提交响应里直接就能取到视频地址。
        poll_url = config.poll_url.strip()
        if not poll_url:
            video_url = dig(submit_answer, config.video_path)
            if not video_url:
                raise _EngineError(
                    f"按「视频地址取法」（{config.video_path}）在云端返回里没找到下载地址。"
                )
            return self._download(str(video_url), should_stop), trace

        task_id = dig(submit_answer, config.task_path)
        if not task_id:
            raise _EngineError(
                f"提交没拿到任务 id（取法 {config.task_path}）；" "请检查任务ID字段与接口配置。"
            )
        trace["task_id"] = str(task_id)
        poll = render_url(poll_url, {"task_id": task_id})
        succeeded = _split_words(config.succeeded)
        running = _split_words(config.running)
        failed = _split_words(config.failed)
        stop_after = time.monotonic() + self._deadline
        last_payload: object = submit_answer
        while True:
            if should_stop and should_stop():
                raise _EngineError("已停止等待云端视频；服务商任务可能仍在执行")
            last_payload = self._request(
                urllib.request.Request(poll, headers=headers, method="GET"), 60.0
            )
            trace["poll"] = last_payload
            video_url = dig(last_payload, config.video_path)
            status = dig(last_payload, config.status_path)
            status_word = str(status or "").strip().upper()
            if video_url:
                break
            if config.status_path.strip() and status_word:
                if status_word in failed:
                    raise _EngineError("云端任务失败，请在服务商控制台核对任务状态")
                if status_word in running:
                    pass  # 明确在跑：什么都不做，落到底部继续等下一次轮询
                elif not succeeded or status_word in succeeded:
                    break
            if time.monotonic() >= stop_after:
                raise _EngineError(
                    f"等云端这条视频超过 {int(self._deadline // 60)} 分钟（任务 {task_id}）。"
                )
            self._sleep(POLL_INTERVAL_SECONDS)
        if not video_url:
            raise _EngineError(f"云端任务结束却没给视频地址（取法 {config.video_path}）。")
        return self._download(str(video_url), should_stop), trace


__all__ = [
    "CloudVideoClient",
    "CloudVideoConfig",
    "dig",
    "render_json_body",
    "render_url",
]
