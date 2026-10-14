"""Chat tools for the same local video service used by the desktop form."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from jarvis.tools.types import (
    PERMISSION_NETWORK,
    RiskLevel,
    ToolHandler,
    ToolSpec,
    integer_property,
    object_schema,
    string_property,
)


class VideoSurface(Protocol):
    def available(self) -> bool: ...
    def capabilities(self) -> str: ...
    def generate(
        self,
        prompt: str,
        *,
        frames: object = None,
        steps: object = None,
        size: object = None,
        fps: object = None,
        seconds: object = None,
    ) -> Mapping[str, object]: ...


def build(service: VideoSurface | None) -> list[tuple[ToolSpec, ToolHandler]]:
    if service is None or not service.available():
        return []

    def generate(arguments: Mapping[str, object]) -> str:
        answer = service.generate(
            str(arguments.get("prompt") or ""),
            frames=arguments.get("frames"),
            steps=arguments.get("steps"),
            size=arguments.get("size"),
            fps=arguments.get("fps"),
            seconds=arguments.get("seconds"),
        )
        if not answer.get("ok"):
            return f"视频没有生成：{answer.get('error')}"
        engine = str(answer.get("engine") or "local")
        where = "云端" if engine == "cloud" else "本地 AnimateDiff"
        words = (
            f"视频生成好了，存在：{answer.get('path')}"
            f"（{where}，{answer.get('size')}、{answer.get('fps')} 帧/秒、"
            f"{answer.get('duration')} 秒，生成用了 {answer.get('seconds')} 秒）。\n"
        )
        if engine == "cloud":
            words += f"发给云端的原话：{answer.get('prompt')}\n{answer.get('prompt_note')}"
        else:
            words += f"英文描述：{answer.get('prompt')}\n{answer.get('prompt_note')}"
        if answer.get("capped"):
            # 截帧必须说：用户要 32 秒、拿到 2 秒，不写这句就是悄悄改写。
            words += f"\n注意：{answer.get('note')}"
        return words

    def status(arguments: Mapping[str, object]) -> str:
        del arguments
        return service.capabilities()

    return [
        (
            ToolSpec(
                name="generate_video",
                description=(
                    "生成本地视频并返回 MP4。用户请求视频/动画时使用。prompt 必须传用户原始"
                    "描述，保持原语言和所有细节；软件自动整句翻译为英文，不要提前改写或添人物。"
                    "分辨率/帧率/时长全部留空＝按本机显存自动选（默认短视频档）。"
                    "size 可选 256x256/384x384/512x512；fps 可选 8/12/16/24；"
                    "seconds 可选 2/3/4/8/16/32（超出显存上限会截到能装下的帧数并在回话里说明）。"
                    "用户要长视频就给大 seconds，别自己劝退。"
                ),
                parameters=object_schema(
                    {
                        "prompt": string_property("用户原始视频描述，支持任意语言。"),
                        "size": string_property("分辨率，如 512x512；留空＝自动。"),
                        "fps": integer_property("帧率 8/12/16/24；留空＝自动。", minimum=8),
                        "seconds": integer_property(
                            "时长秒数 2/3/4/8/16/32；留空＝自动（短视频档）。", minimum=2
                        ),
                        "steps": integer_property("12–30 步，默认 16。", minimum=12),
                    },
                    required=("prompt",),
                ),
                risk=RiskLevel.CAUTION,
                permissions=frozenset({PERMISSION_NETWORK}),
            ),
            generate,
        ),
        (
            ToolSpec(
                name="video_status",
                description="查询本地视频模型的能力和限制。",
                parameters=object_schema({}),
                risk=RiskLevel.SAFE,
            ),
            status,
        ),
    ]
