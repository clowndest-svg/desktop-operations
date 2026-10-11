"""Pick which tools go out with one request, from what the user actually said.

Why this exists rather than always sending the whole table: the built-in table
measured on this machine's own tokenizer is 35 tools / 3945 tokens, and the
bundled Qwen3-4B is commonly run at a 4096-token window. The table alone does not
fit, so a 4K local model either overflows or is left holding nothing. Narrowing
the table to the handful of tools a sentence could plausibly need is what makes
tool calling reachable offline at all -- it is not a cleverness feature.

Two rules keep this honest:

* It only ever *removes* tools from the request. Permissions, risk levels and
  confirmations stay in :class:`jarvis.tools.policy.ToolPolicy` and the registry;
  a routing hit grants nothing.
* A tool that is not in any pack is a bug, not a silent absence. ``CORE`` plus the
  guard test in ``tests/test_tool_routing.py`` are what make an un-routed new tool
  fail loudly instead of quietly never being advertised.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Final

GENERAL: Final[str] = "general"
"""Nothing matched. The conversational minimum, not the whole table."""

CORE: Final[tuple[str, ...]] = ("current_time", "calculate")
"""Sent with every pack.

Both are one-line answers the model otherwise fakes: it cannot see a clock, and it
is reliably wrong at arithmetic. 201 tokens measured, which buys back a class of
confident lies.
"""

PACKS: Final[dict[str, tuple[str, ...]]] = {
    "system": ("system_report", "system_trend", "list_processes", "disk_scan"),
    "runtime": ("runtime_versions", "system_report"),
    "network": ("search_web", "fetch_url"),
    "open_app": ("open_app",),
    "files": ("list_directory", "read_file", "search_files"),
    "desktop": (
        "computer_status",
        "key_press",
        "type_text",
        "mouse_click",
        "mouse_move",
        "mouse_scroll",
    ),
    "mirror": ("mirror_devices", "mirror_start", "mirror_stop", "phone_screen_read"),
    "image": ("generate_image", "image_status"),
    "documents": ("write_document", "document_kinds"),
    "process": ("list_processes", "propose_kill_process"),
    "settings": ("settings_read", "settings_apply", "voice_pick"),
    "reminders": ("add_reminder", "list_reminders", "cancel_reminder"),
    "memory": ("memory_search", "remember", "chat_search", "knowledge_search", "add_knowledge"),
    "voice": ("speak",),
    "usage": ("usage_stats", "text_stats"),
    "shell": ("run_powershell", "run_shell"),
    "write": ("write_file", "delete_path"),
    # ``remember`` is in the fallback on purpose. r89 measured it: 「记一下：我的部署端口
    # 是 8199」 matched no pack, fell here, and ``remember`` was not offered -- so she
    # answered 「小夜无法直接保存用户输入」 while a rule about which words mean
    # "remember this" decided whether she could be told anything. Every other pack is
    # about saving tokens; this one is about not being deaf to the single most
    # assistant-shaped sentence a person says.
    #
    # ``generate_image`` joined for the same reason (r105): 「生成一个绿色的哥布林」
    # names no picture-word at all, so no regex can catch it, and the model was handed
    # a tool table with no way to draw -- 「当前我无法直接生成图片」 on a machine where
    # ComfyUI was running two metres away.
    GENERAL: ("memory_search", "remember", "knowledge_search", "chat_search", "generate_image"),
}
"""Intent -> the tools that can answer it.

Names are listed even when a build omits them (no desktop service, no shell level,
``tools.allow_write`` off): :func:`select_tool_names` filters against what is
actually registered, because advertising a tool the registry cannot reach is how
she ends up promising something that can only ever be refused.
"""

# Each pack is one compiled alternation. Chinese has no word boundaries, so these
# are substring matches -- which means the *shape* of the phrase is what carries the
# signal. Bare 系统 is deliberately absent: it matched 「系统设计」 and 「这个系统的
# 架构」 in testing, and a question about software design has no business loading
# telemetry into a 4K window.
_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "system": re.compile(
        r"cpu|内存|显存|磁盘|硬盘|u盘|显卡|gpu|进程|占用|负载|卡不卡|卡顿|流畅|"
        r"运行状态|系统状态|系统盘|开机多久|开机时间|风扇|温度|"
        r"这台电脑|我的电脑|本机|本系统|电脑上|机器怎么样|性能|剩余空间|空间不够|满了|"
        r"检查.{0,4}(系统|电脑|机器|环境)|系统信息|硬件配置|配置怎么样",
        re.IGNORECASE,
    ),
    "runtime": re.compile(
        r"jdk|jre|javac|java|python|pip|node|node\.js|npm|yarn|pnpm|git|dotnet|"
        r"golang|go 版本|rust|cuda|sdk|环境变量|路径变量|\bpath\b|装了什么|装了哪些|"
        r"什么版本|版本是|版本号|版本信息|多少版本|有没有装",
        re.IGNORECASE,
    ),
    "network": re.compile(
        r"联网|上网|网上|网页|网址|链接|http://|https://|搜索|搜一下|搜下|查一下|查下|"
        r"最新|新闻|实时|行情|天气|股价|汇率|热搜",
        re.IGNORECASE,
    ),
    "open_app": re.compile(
        r"(打开|启动|运行|调出|唤起)\s*(一下)?\s*(记事本|计算器|浏览器|资源管理器|任务管理器|"
        r"控制面板|设置|终端|命令行|powershell|cmd|notepad|calc|explorer|chrome|edge|firefox|"
        r"微信|qq|钉钉|飞书|vscode|visual\s*studio|word|excel|powerpoint|ppt|photoshop|steam|"
        r"网易云|酷狗|potplayer|应用|软件|程序|\bapp\b)",
        re.IGNORECASE,
    ),
    "files": re.compile(
        r"读[一下]*文件|看[一下]*文件|打开.{0,6}(文件|目录|文件夹)|文件内容|文件里|"
        r"列[一下]*\s*(目录|文件)|目录里有|目录下有|里面有什么|找[一下]*文件|搜[一下]*文件|"
        r"搜索文件|有没有.{0,10}的?(文件|文档|pdf|docx|xlsx|txt)|\.md\b|\.py\b|readme|"
        r"路径下|这个文件夹",
        re.IGNORECASE,
    ),
    "desktop": re.compile(
        r"点击|单击|双击|点一下鼠标|移动鼠标|鼠标|滚动|滚轮|敲[一下]?键?|"
        r"按\s*(一)?[下个]?\s*(ctrl|control|alt|win|windows|esc|escape|enter|return|tab|"
        r"shift|space|空格|回车|退格|backspace|delete|insert|printscreen|f\d{1,2})|"
        r"组合键|快捷键|输入文字|打字|截屏|屏幕上|光标",
        re.IGNORECASE,
    ),
    # Bare 「镜像」 is deliberately absent: docker 镜像 and 投屏 get said in the same
    # breath on this kind of machine, and pulling three mirror tools into a 4K window
    # for a container question is the token bill this module exists to avoid.
    "mirror": re.compile(
        r"投屏|屏幕共享|屏幕镜像|画面镜像|miracast|scrcpy|"
        r"(手机|这台手机|我的手机).{0,10}(屏幕|画面|显示).{0,12}(电脑|显示器|大屏|投|共享)"
        r"|(手机|这台手机|我的手机).{0,8}(投到|投放|连到|镜像到).{0,8}(电脑|显示器|大屏)",
        re.IGNORECASE,
    ),
    # 「画一张…」是生图，不是命令行那一格的「截屏」，也不是把屏幕发给模型看的那两条：
    # 所以这里不收裸的「图」，也不收「截图」—— 那两个词今天已经有主人了。
    "image": re.compile(
        r"画一张|画张|画个|画幅|画一幅|生图|生成图片|生成一张|生成个|出张图|出图|"
        r"做个头像|做张头像|配张图|做壁纸|弄壁纸|换壁纸|文生图|stable diffusion|comfyui|"
        r"帮我画|给我画|"
        # r105：用户实拍里那两句一条都没命中 ——「嗯，随便生成有张图片给我」和
        # 「帮我做一张壁纸」。少了这一条，generate_image 根本没进请求，她只能回
        # 「当前我无法直接生成图片」。中文没有词边界，所以按"动词 + 最多八个字 + 图画类名词"收。
        r"(生成|做|来|整|要|给我|帮我).{0,8}(图|图片|照片|壁纸|头像|插画|海报|logo)",
        re.IGNORECASE,
    ),
    # r105：「帮我做一份周报」「导出成 Excel」这类话以前一条都不命中，
    # 于是她手里根本没有 write_document —— 只能拿 write_file 写一个 .md 交差。
    "documents": re.compile(
        r"excel|xlsx|csv 表|word|docx|文档|pptx?|幻灯片|演示|汇报|pdf|周报|日报|月报|"
        r"台账|明细表|清单表|说明书|方案书|建议书|导出成|导出为|做成.{0,4}(表|文档|报告)|"
        r"生成.{0,6}(文档|报告|表格|幻灯片)",
        re.IGNORECASE,
    ),
    "process": re.compile(
        r"结束进程|杀掉| kill|关闭进程|停止进程|强制结束|进程没了|卡死的进程",
        re.IGNORECASE,
    ),
    "settings": re.compile(
        r"设置里|这个设置|把.{0,8}(改成|设为|调成|换成)|开启.{0,6}开关|关闭.{0,6}开关|"
        r"改个音色|换个音色|用什么模型|切换模型|思考预算|上下文窗口|轮数",
        re.IGNORECASE,
    ),
    "reminders": re.compile(
        r"提醒|定时|到点|分钟后|小时后|明天\s*\d|后天\s*\d|周[一二三四五六日].*\d{1,2}[:点]",
        re.IGNORECASE,
    ),
    "memory": re.compile(
        r"我记得|你记得|上次|之前说|以前聊|跟你说过|还记不记得|我导入|资料库|知识库里|"
        r"手册里|文档里写|记住|记一下|记下来|帮我记|别忘了|下次记得|以后(都|就|按)|"
        r"(存|记|收|放|加)(进|到|成).{0,6}(资料|知识库|笔记|档案)",
        re.IGNORECASE,
    ),
    "voice": re.compile(r"念(给|出)|读给我|朗读|说出来|播报|用声音", re.IGNORECASE),
    "usage": re.compile(r"花了多少|token|用量|统计|字数|词频", re.IGNORECASE),
    "shell": re.compile(
        r"跑个命令|执行命令|运行命令|跑一条|powershell|命令行里|执行脚本|跑个脚本",
        re.IGNORECASE,
    ),
    "write": re.compile(
        r"写[入进]?到?文件|保存为|另存|新建(一个)?(文件|目录|文件夹)|改[一下]*文件|"
        r"删[掉除]?这?[个些]?文件|文件.{0,4}删|删掉那|清空这个文件|覆盖写",
        re.IGNORECASE,
    ),
}


def classify_tool_intent(text: str) -> tuple[str, ...]:
    """Which capability packs this sentence could plausibly need.

    Returns every match in :data:`PACKS` order, not just the first: 「打开记事本，
    顺便看下CPU」 needs both packs, and picking one at random would make the other
    half of the sentence unanswerable. Empty means :data:`GENERAL`.

    Pure and offline on purpose. A small model cannot be trusted to route itself,
    and a second round trip to decide which tools to send costs more than the
    tokens it saves.
    """
    if not text or not text.strip():
        return ()
    return tuple(name for name in PACKS if name != GENERAL and _PATTERNS[name].search(text))


def select_tool_names(intents: str | Iterable[str], available: Sequence[str]) -> tuple[str, ...]:
    """The registered tools this intent set can use, in stable pack order.

    ``available`` is what the registry actually holds right now, so a pack naming
    ``run_powershell`` on a build without a shell level contributes nothing rather
    than a tool that only ever answers 「被安全策略拒绝」.

    No intent matched means :data:`GENERAL`, not the general pack *added on top* of
    the matched ones. Padding every request with the retrieval tools would put back
    the ~470 tokens this function exists to save, and on a 4K local model those are
    the difference between carrying a tool table and carrying none.
    """
    wanted = (intents,) if isinstance(intents, str) else tuple(intents)
    names = wanted or (GENERAL,)
    known = set(available)
    chosen: list[str] = []
    seen: set[str] = set()
    for tool in (*CORE, *(t for name in names for t in PACKS.get(name, ()))):
        if tool in known and tool not in seen:
            seen.add(tool)
            chosen.append(tool)
    return tuple(chosen)


__all__ = ["CORE", "PACKS", "classify_tool_intent", "select_tool_names"]
