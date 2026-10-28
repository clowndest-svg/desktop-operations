"""桌面弹窗与对话区的"关闭在哪儿"源码级不变量。

起因是用户实拍：对话区放大成整窗之后，窗口右上角是空的，而关闭动作叫「收回」、
蹲在顶栏那一排按钮中间。顺手一查，十三扇弹窗里有三扇（生图、投屏、视频）的
「关闭」挤在左上角，另外十扇在右上角 —— 同一个动作在同一个软件里有三种位置。

这些判据只证明**声明**了什么，不证明屏幕上真在右上角：那一条要在打包版上用
elementFromPoint 点一下才算（见 scripts/verify_r122_ui.mjs 那一路）。能在源码里
判的先判掉，剩下的账写清楚，别把"有这条 CSS"说成"位置对"。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPONENTS = REPO / "frontend" / "src" / "components"

HEAD_CLASS = re.compile(r'<header[^>]*class="([a-z][a-z0-9]*__head)"')
#: `.xxx__head { ... }` —— 取到右花括号为止，不跨块。
RULE_BLOCK = re.compile(r"\.([a-z][a-z0-9]*__head)\s*\{([^}]*)\}", re.S)
RIGHT_ANCHOR = re.compile(r"justify-content:\s*(?:space-between|flex-end|end)")
LAST_CHILD_AUTO = re.compile(r"\.([a-z][a-z0-9]*__head)\s*>\s*:last-child\s*\{([^}]*)\}", re.S)


def _sources() -> list[Path]:
    return sorted(COMPONENTS.glob("*.vue"))


def test_every_component_with_a_head_actually_renders_a_close_button() -> None:
    """有头部的弹窗必须真有一颗写着「关闭」的按钮 —— 判据不能只测样式不测有没有东西。"""
    missing = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        if not HEAD_CLASS.search(text):
            continue
        head = text[text.index("<header") : text.index("</header>") + len("</header>")]
        if "关闭" not in head:
            missing.append(path.name)
    assert not missing, f"这些弹窗的头部没有「关闭」：{missing}"


def test_every_head_puts_close_at_the_right_edge() -> None:
    """十三扇弹窗的「关闭」必须在同一头：要么 space-between，要么 :last-child 贴右。

    自动扫而不是手写清单 —— 手写那份会在新加一扇弹窗时静默漏掉它，而那正是这一轮
    抓到的病（三扇的头部只有 display:flex;gap，于是关闭紧挨着标题挤在左上角）。
    """
    offenders = []
    checked = 0
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for cls in HEAD_CLASS.findall(text):
            checked += 1
            block = next((body for name, body in RULE_BLOCK.findall(text) if name == cls), None)
            if block is None:
                offenders.append(f"{path.name}:{cls} 没有对应的样式块")
                continue
            spaced = RIGHT_ANCHOR.search(block)
            pushed = any(
                "margin-left: auto" in body
                for name, body in LAST_CHILD_AUTO.findall(text)
                if name == cls
            )
            if not spaced and not pushed:
                offenders.append(
                    f"{path.name}:{cls} 的「关闭」没贴右"
                    "（既没 space-between 也没 margin-left:auto）"
                )
    assert checked >= 10, f"只扫到 {checked} 个头部，八成是正则没匹配上，不是真的都合格"
    assert not offenders, "这些弹窗的关闭位置不一致：\n  " + "\n  ".join(offenders)


def test_the_expanded_chat_closes_from_the_top_right_corner() -> None:
    """放大态的对话区：右上角那颗叫「关闭」，而且它得真的在最右端、不压任何按钮。

    它跟着工具条那一排走（排在「···」之后）：模型标签带着 margin-right:auto 把整排
    推到右边，所以排尾就是右上角。原先它绝对定位在容器 right:0 / top:1px，正好盖住
    「···」—— 屏幕上「关闭」两个字后面拖着半个省略号，就是两颗按钮抢同一批像素。
    真机几何（无重叠、elementFromPoint 点得到）由 scripts/audit_layout.mjs 量。
    """
    text = (COMPONENTS / "ChatPanel.vue").read_text(encoding="utf-8")

    assert 'class="hud-btn chat__simple chat__close"' in text, "放大态没有那颗关闭按钮"
    close_block = re.search(r"<button[^>]*chat__close[^>]*>(.*?)</button>", text, re.S)
    assert close_block and "关闭" in close_block.group(1), "右上角那颗不叫「关闭」"
    assert 'v-if="expanded"' in text[: text.index("chat__close")], "关闭按钮没跟着放大态出现"

    toolbar = re.search(r'aria-controls="chat-options"[^>]*>···</button>(.*?)</div>', text, re.S)
    assert toolbar and "chat__close" in toolbar.group(1), "关闭没有跟工具条那排走"
    rule = re.search(r"\.chat__close\s*\{([^}]*)\}", text)
    assert rule, "没有 .chat__close 的样式"
    assert "position" not in rule.group(1), "关闭又回到绝对定位，会再压住「···」"


def test_the_top_bar_no_longer_hides_the_action_behind_the_word_shouhui() -> None:
    """顶栏那颗只剩「放大」一个方向 —— 「收回」这个说法从界面上消失。

    一个按钮两态两个词（放大／收回）时，放大之后用户找不到"怎么关"，因为
    「收回」既不像关闭、也不在眼睛会去看的右上角。
    """
    text = (COMPONENTS / "ChatPanel.vue").read_text(encoding="utf-8")
    template = text[: text.index("</template>")]
    #: 剥掉 HTML 注释再判 —— 注释里写着"以前它叫「收回」"是在讲来历，不是界面上的字。
    visible = re.sub(r"<!--.*?-->", "", template, flags=re.S)

    assert "{{ expanded ? '收回' : '放大' }}" not in visible, "那颗按钮还在两态之间换词"
    leftovers = [line.strip() for line in visible.splitlines() if "收回" in line]
    assert not leftovers, f"界面上还留着「收回」：{leftovers}"
    assert "放大" in visible, "收起态的入口没了"

    #: 光查"没有收回"不够 —— 拿掉 v-if 之后界面上会同时出现「放大」和右上角「关闭」，
    #: 一个动作两个入口、还都写着不同的词，这正是这一轮要消掉的那种乱。
    expand_tag = re.search(r"<button[^>]*chat__expand[^>]*>", visible)
    assert expand_tag and 'v-if="!expanded"' in expand_tag.group(0), "顶栏那颗在放大态还会再冒出来"
