"""生成文档：一份内容，四种壳（xlsx / docx / pptx / pdf）。

为什么是一种输入而不是四种：模型只需要学会一个结构，用户也只需要说一次"里面有什么"。
四种格式的差异全部收在这一个文件里，工具那一侧只有一条 ``write_document``。

三条硬规矩，都是被别的坑教出来的：

* **缺库就不广告这条工具**（和生图同一条规矩）。宁可清单里少一条，也不要模型答应得
  很自然、然后每一次都说"我做不到"。
* **绝不覆盖已有文件**。名字撞了就加 ``-2``、``-3``。用户的一份周报没了，比文件名难看严重。
* **中文 PDF 要有真字体**。reportlab 的 CID 字体（``STSong-Light``）不需要字体文件，
  但那样产出的 PDF 里的中文在文本提取时是拿不回来的 —— 所以优先用系统里已有的
  黑体/雅黑/宋体（``.ttf``/``.ttc``），有才用，没有才退回 CID，并在结果里说清楚用的哪个。
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

#: 一份文档最多带多少节 / 每节多少段、多少行 —— 模型一次说太多通常是在编。
MAX_SECTIONS: Final = 40
MAX_PARAGRAPHS: Final = 60
MAX_TABLE_ROWS: Final = 300


class DocumentError(RuntimeError):
    """一句能直接给用户看的话。抛到工具那一层会被原样转成拒绝。"""


@dataclass(frozen=True, slots=True)
class Section:
    """一节：一个标题 + 若干段/要点 + 可选一张表。四种壳都从这一个形状出发。"""

    heading: str = ""
    paragraphs: tuple[str, ...] = ()
    bullets: tuple[str, ...] = ()
    headers: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()

    @property
    def has_table(self) -> bool:
        return bool(self.headers or self.rows)


@dataclass(frozen=True, slots=True)
class DocKind:
    id: str
    label: str
    extension: str
    note: str


KINDS: Final[tuple[DocKind, ...]] = (
    DocKind(
        id="xlsx",
        label="Excel 表格",
        extension=".xlsx",
        note="每一节开一张工作表；表头/数据行按表格落，段落当成附注行。适合台账、清单、统计。",
    ),
    DocKind(
        id="docx",
        label="Word 文档",
        extension=".docx",
        note="标题 + 各级小节 + 段落 + 项目符号 + 表格。适合报告、方案、说明书。",
    ),
    DocKind(
        id="pptx",
        label="PowerPoint 演示",
        extension=".pptx",
        note="一张封面 + 每节一张幻灯片（标题 + 要点）。表格会单独排一张。适合汇报。",
    ),
    DocKind(
        id="pdf",
        label="PDF",
        extension=".pdf",
        note="和 Word 同一套排版，但定版不可改。适合发给别人看的那一份。",
    ),
)

#: 中文字体候选：按"有就用、没有就退 CID"的顺序找，绝不从包里塞字体文件（许可与体积都不干）。
CJK_FONTS: Final[tuple[str, ...]] = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
)


def kind_by_id(name: object) -> DocKind | None:
    text = str(name or "").strip().lower().lstrip(".")
    for kind in KINDS:
        if text in (kind.id, kind.extension.lstrip(".")):
            return kind
    return None


def missing_libraries() -> tuple[str, ...]:
    """这条能力现在缺哪几个包。空元组 = 四种都能做。"""
    needed = ("openpyxl", "docx", "pptx", "reportlab")
    missing: list[str] = []
    for name in needed:
        try:
            __import__(name)
        except Exception:  # 半个包装坏了也算缺
            missing.append(name)
    return tuple(missing)


# ----------------------------------------------------------------------
# 内容规范化：模型给的东西一律先洗一遍，洗不动就明确拒绝
# ----------------------------------------------------------------------


def _text(value: object) -> str:
    return str(value if value is not None else "").strip()


def _lines(value: object) -> list[str]:
    if isinstance(value, str):
        return [row.strip() for row in value.splitlines() if row.strip()]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_text(item) for item in value if _text(item)]
    text = _text(value)
    return [text] if text else []


def _table(value: object) -> tuple[list[str], list[list[str]]] | None:
    """接受 ``{"headers": [...], "rows": [[...], ...]}``，也接受裸的二维数组。"""
    if isinstance(value, Mapping):
        headers = _lines(value.get("headers"))
        rows = value.get("rows")
    else:
        headers, rows = [], value
    if isinstance(rows, str):
        rows = [row.split("\t") for row in rows.splitlines() if row.strip()]
    if not isinstance(rows, Sequence) or isinstance(rows, (bytes, bytearray)):
        return None
    grid: list[list[str]] = []
    for row in rows:
        cells = (
            _lines(row)
            if isinstance(row, (Sequence, str)) and not isinstance(row, dict)
            else [_text(row)]
        )
        if cells:
            grid.append(cells)
    if not headers and not grid:
        return None
    if not headers and grid:
        headers, grid = grid[0], grid[1:]
    width = max([len(headers)] + [len(row) for row in grid])
    if width == 0:
        return None
    filled = headers + [""] * (width - len(headers))
    rows = [row + [""] * (width - len(row)) for row in grid[:MAX_TABLE_ROWS]]
    return filled[:26], rows


def clean_sections(value: object) -> list[Section]:
    """把 ``sections`` 洗成一串 :class:`Section`。

    只认这一种结构，其余形状（字符串、单个 dict）在这里收进来 —— 模型经常给一个 dict
    而不是 list，硬要它每次都对只会换来一次"生成失败"。
    """
    if isinstance(value, Mapping):
        raw: list[object] = [value]
    elif isinstance(value, str):
        raw = [{"heading": "", "paragraphs": _lines(value)}]
    elif isinstance(value, Sequence):
        raw = list(value)
    else:
        raw = []
    out: list[Section] = []
    for item in raw:
        if isinstance(item, str):
            item = {"heading": "", "paragraphs": [item]}
        if not isinstance(item, Mapping):
            continue
        headers, rows = _table(item.get("table") or item.get("rows") or item.get("data")) or (
            (),
            (),
        )
        row = Section(
            heading=_text(item.get("heading") or item.get("title")),
            paragraphs=tuple(
                _lines(item.get("paragraphs") or item.get("text") or item.get("body"))
            ),
            bullets=tuple(_lines(item.get("bullets") or item.get("points") or item.get("items"))),
            headers=tuple(headers),
            rows=tuple(tuple(cells) for cells in rows),
        )
        if not (row.heading or row.paragraphs or row.bullets or row.has_table):
            continue
        out.append(row)
        if len(out) >= MAX_SECTIONS:
            break
    if not out:
        raise DocumentError(
            "没有可用的内容：sections 要是一串小节，每节能带 heading、paragraphs、bullets、"
            'table（形如 {"headers": [...], "rows": [[...]]}）中的任意几样。'
        )
    return out


def safe_name(title: str, fallback: str = "文档") -> str:
    """文件名只留能安全出现在任何目录里的字符。"""
    text = _text(title) or fallback
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return (text[:60] or fallback) + f"_{time.strftime('%H%M%S')}"


def unique_path(directory: Path, base: str, extension: str) -> Path:
    """撞名就往后编号，**绝不覆盖**。覆盖一个用户写了半天的表是不可逆的。"""
    candidate = directory / f"{base}{extension}"
    index = 2
    while candidate.exists():
        candidate = directory / f"{base}-{index}{extension}"
        index += 1
    return candidate


def resolve_directory(value: object, exports_dir: Path) -> Path:
    """用户指了目录就用他的，没指就用「数据/文档」。

    相对路径一律按 ``exports_dir`` 解释 —— 不这么定，同一个"reports"会随进程的当前目录
    漂到不知道哪里去。
    """
    exports_dir.mkdir(parents=True, exist_ok=True)
    text = _text(value)
    if not text:
        return exports_dir
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    if not path.is_absolute():
        path = exports_dir / path
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DocumentError(f"那个目录建不出来：{path}（{exc}）") from exc
    if not path.is_dir():
        raise DocumentError(f"要存文档的地方不是一个目录：{path}")
    return path


# ----------------------------------------------------------------------
# 四种壳。每个函数都只做一件事：把洗过的内容写进 path。
# ----------------------------------------------------------------------


def _write_docx(path: Path, title: str, sections: list[Section]) -> None:
    from docx import Document

    document = Document()
    document.add_heading(title, level=0)
    for section in sections:
        if section.heading:
            document.add_heading(section.heading, level=1)
        for paragraph in section.paragraphs:
            document.add_paragraph(paragraph)
        for bullet in section.bullets:
            document.add_paragraph(bullet, style="List Bullet")
        if section.has_table:
            _add_docx_table(document, section)
    document.save(str(path))


def _add_docx_table(document: Any, section: Section) -> None:
    width = max(1, len(section.headers))
    handle = document.add_table(rows=1 + len(section.rows), cols=width)
    handle.style = "Table Grid"
    for index, cell in enumerate(handle.rows[0].cells):
        cell.text = section.headers[index] if index < len(section.headers) else ""
    for offset, values in enumerate(section.rows):
        for index, cell in enumerate(handle.rows[offset + 1].cells):
            cell.text = values[index] if index < len(values) else ""


def _write_xlsx(path: Path, title: str, sections: list[Section]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    book = Workbook()
    book.remove(book.active)
    used: set[str] = set()
    for index, section in enumerate(sections, start=1):
        heading = section.heading or title or f"第{index}节"
        sheet = book.create_sheet(_sheet_name(heading, index, used))
        row = 1
        for paragraph in section.paragraphs:
            sheet.cell(row=row, column=1, value=paragraph)
            row += 1
        for bullet in section.bullets:
            sheet.cell(row=row, column=1, value=f"• {bullet}")
            row += 1
        if section.has_table:
            row += 1 if row > 1 else 0
            for column, name in enumerate(section.headers, start=1):
                sheet.cell(row=row, column=column, value=name).font = Font(bold=True)
            for offset, values in enumerate(section.rows, start=1):
                for column, value in enumerate(values, start=1):
                    sheet.cell(row=row + offset, column=column, value=_number(value))
    book.save(str(path))


def _sheet_name(heading: str, index: int, used: set[str]) -> str:
    """Excel 的表名不能带方括号、冒号、星号、问号、斜杠和反斜杠，最长 31 个字符，还不能重名。"""
    cleaned = re.sub(r"[\\[\\]:*?/\\]", "_", heading).strip() or f"第{index}节"
    name = cleaned[:31]
    while name in used:
        tail = f"-{index}"
        name = name[: 31 - len(tail)] + tail
    used.add(name)
    return name


def _number(value: object) -> object:
    """像数字的写成数字：表格里一列 1,2,3 是文本还是数值，用户下一眼就看得出来。"""
    text = _text(value)
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d*\.\d+", text):
        return float(text)
    return text


def _write_pptx(path: Path, title: str, sections: list[Section]) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    cover = deck.slides.add_slide(deck.slide_layouts[0])
    cover.shapes.title.text = title
    if len(cover.placeholders) > 1:
        cover.placeholders[1].text = time.strftime("生成于 %Y-%m-%d %H:%M")
    for section in sections:
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = section.heading or title
        lines = [*section.bullets, *section.paragraphs]
        body = slide.placeholders[1] if len(slide.placeholders) > 1 else None
        if body is not None and lines:
            body.text_frame.text = "\n".join(lines[:12])
        if section.has_table:
            _add_pptx_table(slide, section, Inches, len(lines))
    deck.save(str(path))


def _add_pptx_table(slide: Any, section: Section, inches: Any, used: int) -> None:
    width = max(1, len(section.headers))
    height = min(len(section.rows) + 1, 12)
    left, top = inches(0.6), inches(min(5.2, 1.9 + 0.32 * used))
    box = slide.shapes.add_table(height, width, left, top, inches(8.8), inches(0.4 * height))
    table = box.table
    for column, name in enumerate(section.headers[:width]):
        table.cell(0, column).text = name
    for row_index, values in enumerate(section.rows[: height - 1], start=1):
        for column in range(width):
            table.cell(row_index, column).text = values[column] if column < len(values) else ""


def _write_pdf(path: Path, title: str, sections: list[Section]) -> str:
    """返回实际用的字体名，好让结果里能说清"中文靠谁显示"。"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = _register_pdf_font(pdfmetrics, TTFont, UnicodeCIDFont)
    base = getSampleStyleSheet()["Normal"]
    body = ParagraphStyle("xiaoye", parent=base, fontName=font, fontSize=10.5, leading=16)
    heading = ParagraphStyle(
        "xiaoye_h", parent=body, fontName=font, fontSize=14, leading=20, spaceBefore=8
    )
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm)
    story: list[object] = [
        Paragraph(
            _escape(title), ParagraphStyle("xiaoye_t", parent=heading, fontSize=19, leading=26)
        )
    ]
    for section in sections:
        if section.heading:
            story.append(Paragraph(_escape(section.heading), heading))
        for paragraph in section.paragraphs:
            story.append(Paragraph(_escape(paragraph), body))
            story.append(Spacer(1, 3 * mm))
        for bullet in section.bullets:
            story.append(Paragraph("• " + _escape(bullet), body))
        if section.has_table:
            grid = [
                [_escape(cell) for cell in row]
                for row in [list(section.headers), *[list(row) for row in section.rows]]
                if row
            ]
            if grid:
                widget = Table(grid)
                widget.setStyle(
                    TableStyle(
                        [
                            ("FONTNAME", (0, 0), (-1, -1), font),
                            ("FONTSIZE", (0, 0), (-1, -1), 9),
                            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9aa4b2")),
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ]
                    )
                )
                story.append(Spacer(1, 2 * mm))
                story.append(widget)
                story.append(Spacer(1, 4 * mm))
    doc.build(story)
    return font


def _register_pdf_font(pdfmetrics: Any, ttfont: Any, cidfont: Any) -> str:
    for candidate in CJK_FONTS:
        if not Path(candidate).is_file():
            continue
        try:
            pdfmetrics.registerFont(ttfont("XiaoYeCJK", candidate))
            return "XiaoYeCJK"
        except Exception:  # 一个 .ttc 报错了别连带放弃后面三个
            continue
    pdfmetrics.registerFont(cidfont("STSong-Light"))
    return "STSong-Light"


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _emit(kind: str, path: Path, title: str, sections: list[Section]) -> str:
    """写一份，回"这一份用了什么字体" —— 只有 PDF 需要说这件事。"""
    if kind == "xlsx":
        _write_xlsx(path, title, sections)
    elif kind == "docx":
        _write_docx(path, title, sections)
    elif kind == "pptx":
        _write_pptx(path, title, sections)
    else:
        return _write_pdf(path, title, sections)
    return ""


def write_document(
    kind: object,
    title: object,
    sections: object,
    *,
    directory: object = None,
    exports_dir: Path,
) -> dict[str, object]:
    """生成一份文档，返回 ``{"ok", "path", "kind", "font"}``。失败抛 :class:`DocumentError`。"""
    chosen = kind_by_id(kind)
    if chosen is None:
        raise DocumentError(
            "没有这种文档：可选 " + "、".join(f"{item.id}（{item.label}）" for item in KINDS)
        )
    missing = missing_libraries()
    if missing:
        raise DocumentError(
            f"这台机器上没装 {'、'.join(missing)}，做不了文档。"
            "装法：pip install -r requirements.txt 之后再 pip install jarvis-assistant[docs]"
        )
    heading = _text(title) or "未命名文档"
    body = clean_sections(sections)
    folder = resolve_directory(directory, exports_dir)
    path = unique_path(folder, safe_name(heading), chosen.extension)
    try:
        font = _emit(chosen.id, path, heading, body)
    except DocumentError:
        raise
    except Exception as exc:  # 库自己报的栈对用户没有意义，翻成一句人话
        raise DocumentError(f"{chosen.label}没写成功：{type(exc).__name__}: {exc}") from exc
    if not path.is_file() or path.stat().st_size == 0:
        raise DocumentError(f"{chosen.label}说写完了，盘上却没有：{path}")
    return {
        "ok": True,
        "path": str(path),
        "kind": chosen.id,
        "font": font or "",
        "bytes": path.stat().st_size,
    }


class DocumentService:
    """工具层看到的那一面（协议在 ``jarvis/tools/builtins/document_tools.py``）。

    失败一律翻成 ``{"ok": False, "error": 一句话}`` 而不是把异常抛过去：工具结果里出现
    Python 栈，模型学不到"该改哪个参数"，只会照着原样再发一次。
    """

    def __init__(self, exports_dir: Path) -> None:
        self._exports = exports_dir

    def available(self) -> bool:
        return not missing_libraries()

    def write(
        self, kind: str, title: str, sections: object, directory: str = ""
    ) -> dict[str, object]:
        try:
            return write_document(
                kind, title, sections, directory=directory, exports_dir=self._exports
            )
        except DocumentError as exc:
            return {"ok": False, "error": str(exc)}

    def capabilities(self) -> str:
        return capabilities()


def capabilities() -> str:
    """给模型/面板念的一句：能做哪四种、现在缺不缺东西。"""
    missing = missing_libraries()
    if missing:
        return f"文档生成不可用：缺 {'、'.join(missing)}。"
    lines = ["能生成四种文档，都写到本机磁盘并回一个绝对路径："]
    for kind in KINDS:
        lines.append(f"- {kind.id}（{kind.label}）：{kind.note}")
    lines.append("内容都是一串小节，每节可带 heading / paragraphs / bullets / table。")
    return "\n".join(lines)


__all__ = [
    "CJK_FONTS",
    "KINDS",
    "DocumentError",
    "DocumentService",
    "Section",
    "capabilities",
    "clean_sections",
    "kind_by_id",
    "missing_libraries",
    "resolve_directory",
    "safe_name",
    "unique_path",
    "write_document",
]
