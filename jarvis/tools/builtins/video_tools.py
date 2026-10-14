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
        self, prompt: str, *, frames: object = None, steps: object = None
    ) -> Mapping[str, object]: ...


def build(service: VideoSurface | None) -> list[tuple[ToolSpec, ToolHandler]]:
    if service is None or not service.available():
        return []

    def generate(arguments: Mapping[str, object]) -> str:
        answer = service.generate(
            str(arguments.get("prompt") or ""),
            frames=arguments.get("frames"),
            steps=arguments.get("steps"),
        )
        if not answer.get("ok"):
            return f"视频没有生成：{answer.get('error')}"
        return (
            f"视频生成好了，存在：{answer.get('path')}"
            f"（本地 AnimateDiff，{answer.get('duration')} 秒短片，"
            f"生成用了 {answer.get('seconds')} 秒）。\n"
            f"英文描述：{answer.get('prompt')}\n{answer.get('prompt_note')}"
        )

    def status(arguments: Mapping[str, object]) -> str:
        del arguments
        return service.capabilities()

    return [
        (
            ToolSpec(
                name="generate_video",
                description=(
                    "生成本地短视频并返回 MP4。用户请求视频/动画时使用。prompt 必须传用户原始"
                    "描述，保持原语言和所有细节；软件自动整句翻译为英文，不要提前改写或添人物。"
                    "默认 2 秒，256×256；frames 可选 16/24/32（2/3/4 秒）。"
                ),
                parameters=object_schema(
                    {
                        "prompt": string_property("用户原始视频描述，支持任意语言。"),
                        "frames": integer_property("16/24/32 帧，可留空。", minimum=16),
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
