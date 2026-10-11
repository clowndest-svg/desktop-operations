"""面板文案：中文句子不许在模板里换行续写。

渲染时模板里的换行会变成一个空格，屏上就出现「鼠标要停在画面上 （黑边里不算）」
「不知道要发给哪台手机， 瞎按」这种 —— 用户说过这类排版难看就是难看（r97 收尾时
读 DOM 才看见，所以钉成测试）。写的是"两个正文行相邻"这个形状，不是某一句文案，
所以新加的段落也躲不过。
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "src"

CJK = re.compile(r"[\u3000-\u303f\u4e00-\u9fffA-Za-z0-9]")


#: 名字像"一整句话"的变量。数字、单位、id 都不在这个名单里，所以「{{ n }} 个模型」不会被误判。
PROSE_NAME = re.compile(r"note|label|text|hint|tip|说明|案", re.IGNORECASE)
#: 「{{ contextTierNote }} 这一档要约 …」—— 变量是一句中文，后面又顶一句中文。
#: 不锚行首：那个形状同样出现在 `<p class="…">{{ note }} 下一句` 里，屏上都是同一个空格。
PROSE_PREFIX = re.compile(r"\{\{\s*([\w$.]+)\s*\}\}\s+[\u3000-\u303f\u4e00-\u9fff]")
_INTERP = re.compile(r"\{\{[^{}]*\}\}")


def is_text_line(stripped: str) -> bool:
    """正文行：**把插值挖掉之后**剩下的还是给人读的字。

    整行因为含 ``{`` 就放过是不行的 —— 「对着麦克风念它，{{ seconds(rules.min_ms) }} 秒，」
    这种行以前正好躲开检查，而它和下一行之间的那个换行渲染出来就是「秒， 存下来之后」。
    """
    if not stripped:
        return False
    if stripped.startswith(("<!--", "-->", "/*", "*")):
        return False
    if stripped.startswith("<") and stripped.endswith(">"):
        return False  # 整行是一个标签
    body = _INTERP.sub("", stripped).strip()
    if not body:
        return False  # 整行就一个插值，没有正文
    return not re.search(r"[<>{}=\"'`;]", body)


def same_line_offender(stripped: str) -> bool:
    """一个"带句子的变量"顶在另一句中文前面（同一条判据的另一种形状）。

    逐个插值看，不是只看第一个：一行里往往先有「{{ row.models.length }} 个模型」再有一个
    句子变量，只看第一个就会把后者放过去。
    """
    return any(PROSE_NAME.search(match.group(1)) for match in PROSE_PREFIX.finditer(stripped))


def template_lines(raw: str) -> list[str]:
    """整段顶层 ``<template>`` 的行。

    以前用 ``find("<template>")`` 到 ``find("</template>")`` —— 那是**第一个**闭合标签，
    而模板里到处是 ``<template v-if=…>`` 这种内层块，它们的闭合写在缩进里。于是
    SettingsPanel 822 行模板只扫了前 42 行，这个检查**过半的界面根本没看到**
    （实测 14 个 .vue 合计 4510 行模板，旧写法只覆盖 2389 行）。
    顶层标签在这批文件里都从第 0 列开始：按"整行等于 ``<template>``"取起点，
    按"整行等于 ``</template>``"的最后一个取终点。
    """
    lines = raw.split("\n")
    start = next((i for i, line in enumerate(lines) if line == "<template>"), None)
    ends = [i for i, line in enumerate(lines) if line == "</template>"]
    if start is None or not ends:
        return []
    return lines[start + 1 : ends[-1]]


def offenders(path: Path) -> list[str]:
    body = template_lines(path.read_text(encoding="utf-8"))
    if not body:
        return []
    out: list[str] = []
    in_comment = False
    for index in range(len(body) - 1):
        line, nxt = body[index].strip(), body[index + 1].strip()
        if "<!--" in line and "-->" not in line:
            in_comment = True
        if "-->" in line:
            in_comment = False
            continue
        if in_comment or not line or not nxt:
            continue
        if not line.startswith("<") and line.endswith(">"):
            continue  # 单行标签
        # 前一行以 ASCII 结尾时那个空格是对的（「允许 USB 调试」），中文之后就不是。
        # `·` 也算：它是分隔符，两侧本来就该各留一个空格（「分钟内有效 · 还剩 3 分钟」）。
        if (
            is_text_line(line)
            and is_text_line(nxt)
            and not re.search(r"[A-Za-z0-9)·]$", line)
            and CJK.match(nxt[0])
        ):
            out.append(f"{path.name}:{index + 1}  {line[-18:]} ⏎ {nxt[:18]}")
        if same_line_offender(line):
            out.append(f"{path.name}:{index + 1}  变量句子顶着一句中文：{line[:44]}")
        if same_line_offender(nxt):
            out.append(f"{path.name}:{index + 2}  变量句子顶着一句中文：{nxt[:44]}")
    return out


def test_no_panel_wraps_a_chinese_sentence_across_template_lines() -> None:
    found: list[str] = []
    for vue in sorted(FRONTEND.rglob("*.vue")):
        found.extend(offenders(vue))
    assert not found, "这些正文在模板里换行续写，渲染出来会多一个空格：\n" + "\n".join(found)


def test_the_new_rule_itself_has_evidence() -> None:
    """规则也得自证：认得出"变量是一句话"，也别把"数字加单位"当成毛病。

    这条是变异验证逼出来的 —— 把 PROSE_NAME 换成一个永不命中的名单时，全仓库已经没有
    那种形状了，上面那条对撞测试照样绿：一个只在"有 bug 时才响"的判据，等于没有判据。
    """
    assert same_line_offender('<p class="x">{{ contextTierNote }} 这一档要约 5226 MB</p>')
    assert same_line_offender("{{ lastNote }} 投屏没开着的时候这几颗是灰的")
    assert not same_line_offender("{{ row.models.length }} 个模型 · 起始 {{ row.default_model }}")
    assert not same_line_offender("{{ seconds(rules.comfortable_ms) }} 秒上下最稳")
    assert not is_text_line("{{")  # 跨行插值的开头不是正文，别把 MobileApp 那个三元式判红
    assert is_text_line("对着麦克风念它，{{ seconds(rules.min_ms) }} 秒，")
