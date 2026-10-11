"""``write_document`` / ``document_kinds`` —— 让她在对话里就能产出一份真文件。

这一层只做三件事：把参数交给服务、把结果翻成一句人话、**在库不全的时候一条都不广告**。
文档的排版全在 :mod:`jarvis.app.documents`，这一层不认识 openpyxl 也不认识 reportlab
（分层规矩：tools 不许 import app，靠 Protocol 接）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Protocol

from jarvis.tools.types import RiskLevel, ToolHandler, ToolSpec, object_schema, string_property

_WRITE: Final = "write_document"
_KINDS: Final = "document_kinds"


class DocumentSurface(Protocol):
    """What :mod:`jarvis.app.documents` offers the tool layer.

    ``write`` answers with a mapping carrying ``path``/``kind``/``bytes`` on success and
    ``error`` on failure -- never an exception, because a stack trace inside a tool
    result teaches the model nothing about what to send instead.
    """

    def available(self) -> bool: ...

    def write(
        self, kind: str, title: str, sections: object, directory: str = ""
    ) -> Mapping[str, object]: ...

    def capabilities(self) -> str: ...


def _words(answer: Mapping[str, object]) -> str:
    if not answer.get("ok"):
        return f"这份文档没生成：{answer.get('error') or '没给出原因'}"
    size = int(str(answer.get("bytes") or 0))
    font = str(answer.get("font") or "")
    tail = f"，中文字体 {font}" if font else ""
    return (
        f"已生成 {answer.get('kind')} 文档，{size:,} 字节，存在：{answer.get('path')}"
        f"{tail}。要打开那个目录就说一声。"
    )


def _write(service: DocumentSurface) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        kind = str(arguments.get("kind") or "").strip()
        title = str(arguments.get("title") or "").strip()
        sections = arguments.get("sections")
        directory = str(arguments.get("directory") or "").strip()
        if not kind:
            return "要说 kind：xlsx、docx、pptx、pdf 里挑一个。"
        if not title:
            return "要给文档起个 title（它会变成文件名）。"
        if not sections:
            return "sections 不能空着：至少一节，里面写 heading / paragraphs / bullets / table。"
        return _words(service.write(kind, title, sections, directory))

    return handler


def _kinds(service: DocumentSurface) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        del arguments
        return service.capabilities()

    return handler


_SECTIONS: Final = {
    "type": "array",
    "description": (
        "一串小节，每节是一个对象：heading（小节标题，可空）、paragraphs（字符串数组，"
        "整段的话）、bullets（字符串数组，要点）、table（对象，形如 "
        '{"headers": ["事项","状态"], "rows": [["修控制条","完成"]]}）。'
        "四样至少给一样。xlsx 每节开一张表；pptx 每节一张幻灯片；docx/pdf 按小节排版。"
    ),
}

_SPECS: Final[tuple[tuple[ToolSpec, str], ...]] = (
    (
        ToolSpec(
            name=_WRITE,
            description=(
                "生成一份**真文件**存到本机：Excel(xlsx)、Word(docx)、PPT(pptx)、PDF(pdf) 四选一。"
                "用户说「做一份周报/台账/清单/方案/说明书/汇报 PPT」「导出成 Excel」「生成 PDF」"
                "这类话就用它 —— 不要改用 write_file 去手写一个 .csv 或 .md，那不是他要的东西。"
                "内容你自己组织（用户只说'本周工作'时，把他说过的要点整理成小节，别留空壳）。"
                "directory 可留空，留空存到软件目录下的「文档」里；用户指定了放哪就填他的路径。"
                "成功会回一个绝对路径，**要把它原样告诉用户**。"
            ),
            parameters=object_schema(
                {
                    "kind": string_property("xlsx / docx / pptx / pdf 中的一个"),
                    "title": string_property("文档标题，同时当文件名"),
                    "sections": _SECTIONS,
                    "directory": string_property("要存到哪个目录，可留空"),
                },
                required=("kind", "title", "sections"),
            ),
            risk=RiskLevel.CAUTION,
        ),
        _WRITE,
    ),
    (
        ToolSpec(
            name=_KINDS,
            description=(
                "问文档生成现在能不能用、支持哪几种、每种能装什么。用户问"
                "「你能生成 Excel 吗」「能不能出 PDF」先问这个，别猜。"
            ),
            parameters={"type": "object", "properties": {}},
            risk=RiskLevel.SAFE,
        ),
        _KINDS,
    ),
)


def build(service: DocumentSurface | None) -> list[tuple[ToolSpec, ToolHandler]]:
    """文档工具，或者什么都没有。

    库没装齐（``pip install jarvis-assistant[docs]`` 那一步没做）时一条都不广告：
    模型答应得极自然、然后每次回一句"缺包"，比清单上少两条难看得多。
    """
    if service is None or not service.available():
        return []
    handlers: dict[str, ToolHandler] = {_WRITE: _write(service), _KINDS: _kinds(service)}
    return [(spec, handlers[key]) for spec, key in _SPECS]


__all__ = ["DocumentSurface", "build"]
