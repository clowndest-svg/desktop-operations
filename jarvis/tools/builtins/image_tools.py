"""生图那两条工具：给她一句话就能出一张图，以及"现在到底能不能出"。

放在这里而不是只在面板里，理由和投屏那三扇一样：「给我画一只猫」是一句话。一个只有
``open_app`` 没有生图工具的模型，会去给用户打开一个第三方画图网站，然后说"画好了"。

面板和工具共用同一个 :class:`ImageEngineSurface` —— 也就是 ``jarvis.app.image_gen.ImageService``
那一个实例。两处各自实现一遍"怎么问引擎"，迟早只有一边是对的。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Protocol

from jarvis.tools.types import (
    PERMISSION_NETWORK,
    RiskLevel,
    ToolHandler,
    ToolSpec,
    object_schema,
    string_property,
)

_GENERATE: Final = "generate_image"
_STATUS: Final = "image_status"


class ImageEngineSurface(Protocol):
    """``ImageService``：这三下就是工具需要的全部。

    声明成 Protocol 是因为 ``tools`` 不许 import ``app`` —— 和 ``ShellGate``、
    ``MirrorSurface`` 在同一层的原因一样。
    """

    def available(self) -> bool: ...

    def capabilities(self) -> str: ...

    def generate(
        self, prompt: str, *, zh: object = None, size: object = None, steps: object = None
    ) -> Mapping[str, object]: ...


def _words(answer: Mapping[str, object]) -> str:
    """把一次生成的结果写成回给模型的那句话。

    成功那半必须带上**路径、尺寸、用了哪条引擎、多少秒**：这四样是用户接着要问的东西
    （"存哪了"、"怎么这么慢"、"换个大一点的"），少了哪一样她都得再猜一次。
    失败那半照实把引擎给的那句人话转出去，不许改写成"画不出来"。
    """
    if answer.get("ok"):
        path = str(answer.get("path") or "")
        engine = "本地 ComfyUI" if answer.get("engine") == "local" else "云端 API"
        return (
            f"画好了，存在：{path}"
            f"（{answer.get('size')}，{answer.get('steps')} 步，{engine}，"
            f"用了 {answer.get('seconds')} 秒，种子 {answer.get('seed')}）。"
            "要改就再说一句，或让我换个尺寸/步数。"
        )
    return f"这一张没画成：{answer.get('error') or '引擎没给原因'}"


def _generate(engine: ImageEngineSurface) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        prompt = str(arguments.get("prompt") or "").strip()
        # 用户原话原封不动往下递。模型翻错过主体（r112 实拍「猪头人」→ `pink pig`），
        # 而词典里那张表是对着这台引擎的权重调过的 —— 服务层要有原话才有机会用上它。
        zh = str(arguments.get("zh") or "").strip()
        size = arguments.get("size")
        if not prompt:
            return "要一句画什么的描述，不能空着。"
        return _words(
            engine.generate(prompt, zh=zh or None, size=size if isinstance(size, str) else None)
        )

    return handler


def _status(engine: ImageEngineSurface) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        del arguments
        return engine.capabilities()

    return handler


_SPECS: Final[tuple[tuple[ToolSpec, str], ...]] = (
    (
        ToolSpec(
            name=_GENERATE,
            description=(
                "按一句话生成一张图片，存到本机「生图」目录并回一个路径。用户说「画一张…」"
                "「生成一张…的图」「给我做个头像/壁纸」，或者任何让你画点什么的说法，都用这一条。"
                "**两个参数都要给**：`zh` 是用户原话（一个字不改，连标点都照抄），"
                "`prompt` 是你把那句话扩写成的英文标签串。软件会拿 `zh` 过一遍本机那张"
                "中文→标签表（它对这台引擎的权重调过），认得主体就用表里的，认不出来才用你写的。"
                "`zh` 漏了不会报错，但那张表就用不上了，所以**每次都带上**。"
                "`prompt` 里**不许留汉字**：引擎的文本编码器只认英文，漏进去的汉字会被逐字拆开"
                "变成噪声（实测一句话漏了 21 个字，画出来是一团粉色色块）。整句中文翻不动的词，"
                "宁可不写，也不要原样抄进 `prompt`。"
                "英文标签的顺序是 主体 → 主体的关键属性（颜色这类写在名词前面，必要时重复一次）→ "
                "场景 → 光照 → 风格/媒介；逗号分隔的短语，不要写完整句子，也不要加用户没提的"
                "人物或性别。用户说得很随便时，由你补成一句具体能画的描述。"
                "size 可留空，留空用软件记着的那一档。"
            ),
            parameters=object_schema(
                {
                    "prompt": string_property(
                        "扩写后的英文标签串，例如 green goblin, green skin, muscular, "
                        "pointed ears, standing, dark forest, moonlight, fantasy illustration"
                        "（只能有英文，不要夹汉字）"
                    ),
                    "zh": string_property(
                        "用户原话，一个字不改，例如 生成一张在月光下翩翩起舞的猪头人"
                    ),
                    "size": string_property("宽x高，例如 512x512 / 768x768 / 1024x1024，可留空"),
                },
                required=("prompt",),
            ),
            risk=RiskLevel.CAUTION,
            permissions=frozenset({PERMISSION_NETWORK}),
        ),
        _GENERATE,
    ),
    (
        ToolSpec(
            name=_STATUS,
            description=(
                "问生图现在能不能用：用的哪一档引擎、本机有没有 ComfyUI 在跑、"
                "云端配了没有。用户问「你能画图吗」「为什么画不出来」先问这个，别猜。"
            ),
            parameters={"type": "object", "properties": {}},
            risk=RiskLevel.SAFE,
        ),
        _STATUS,
    ),
)


def build(engine: ImageEngineSurface | None) -> list[tuple[ToolSpec, ToolHandler]]:
    """生图工具，或者什么都没有。

    和投屏那一道门同一条规矩：引擎一条都走不通的时候不广告名字 —— 模型答应得极自然，
    然后每次都说"我画好了"，而盘上一个文件都没有。
    """
    if engine is None or not engine.available():
        return []
    handlers: dict[str, ToolHandler] = {_GENERATE: _generate(engine), _STATUS: _status(engine)}
    return [(spec, handlers[key]) for spec, key in _SPECS]


__all__ = ["ImageEngineSurface", "build"]
