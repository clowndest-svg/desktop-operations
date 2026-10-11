"""The built-in pet models that ship inside the bundle.

Why the list lives here
-----------------------
``frontend/src/avatar/gallery.ts`` could have kept its own table, but then the picker
could offer a model whose file is not in the package -- a button that is guaranteed to
fail, which is worse than not offering it. So this module owns the names and reads the
numbers (bytes, animation clips) off the ``.glb`` files it actually finds on disk. An
entry whose file is missing is simply not offered, and says so in the log.

Where the files live
--------------------
Next to the HUD bundle (``jarvis/ui/web/models``), which is where the page fetches them
from, and which is *not* user data: nothing here is written at runtime. The binaries are
not in git either -- same arrangement as ``avatar/小夜.vrm`` -- so
``scripts/fetch_gallery_models.py`` rebuilds them from the originals. Their terms are the
Quaternius Asset License v1.0 (commercial use and selling a product that incorporates
them: yes; handing the models out as an asset pack: no), recorded in
``docs/内置模型与许可.md``.
"""

from __future__ import annotations

import json
import logging
import struct
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).resolve().parents[1] / "ui" / "web" / "models"
BUILTIN_PREFIX = "builtin:"

#: id, Chinese label, category shown in the picker, and the one-line description the
#: operator reads before choosing. Categories are the three the operator asked for.
KNOWN: tuple[tuple[str, str, str, str], ...] = (
    ("ShibaInu", "柴犬", "动物", "坐着的那只"),
    ("Fox", "狐狸", "动物", "会回头张望"),
    ("Alpaca", "羊驼", "动物", "脖子最长"),
    ("Panda", "熊猫", "动物", "圆的那只"),
    ("YellowDragon", "小龙", "幻想", "会扑翅膀"),
    ("Ghost", "幽灵", "幻想", "飘着不落地"),
    ("Mushroom", "蘑菇精", "幻想", "一蹦一蹦"),
    ("Cthulhu", "小克苏鲁", "幻想", "触手会动"),
    ("Wizard", "小法师", "人物", "带法杖"),
    ("Cleric", "牧师", "人物", "会鞠躬"),
    ("Monk", "武僧", "人物", "打坐那款"),
)


@dataclass(frozen=True)
class GalleryModel:
    model_id: str
    label: str
    kind: str
    note: str
    bytes: int
    clips: int

    @property
    def preference(self) -> str:
        """What goes into ``pet.figure`` -- prefixed so a built-in can never be
        mistaken for a generated ``pet:<hex>`` id."""
        return BUILTIN_PREFIX + self.model_id

    def to_public(self) -> dict[str, object]:
        return {
            "id": self.preference,
            "key": self.model_id,
            "name": self.label,
            "kind": self.kind,
            "note": self.note,
            "bytes": self.bytes,
            "clips": self.clips,
            "url": "models/" + self.model_id + ".glb",
        }


def count_clips(path: Path) -> int:
    """How many animation clips the ``.glb`` declares, read out of its JSON chunk.

    Deliberately not a constant: the number is shown on screen, and a hand-copied count
    goes stale the moment somebody swaps the file. A file that cannot be read is left in
    the list with zero clips -- one broken card beats an empty picker -- but it says so
    in the log, because "the number is 0" on screen is not something anybody can debug.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
            if len(header) < 12:
                raise ValueError("文件比 GLB 的头还短")
            _magic, _version, _total = struct.unpack("<III", header)
            chunk = handle.read(8)
            if len(chunk) < 8:
                raise ValueError("没有第一个 chunk 的头")
            length, kind = struct.unpack("<II", chunk)
            if kind != 0x4E4F534A:  # 'JSON'
                raise ValueError("第一个 chunk 不是 JSON")
            body = json.loads(handle.read(length).decode("utf-8", "replace"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        logger.warning("内置模型 %s 读不出动画数：%s", path.name, exc)
        return 0
    return len(body.get("animations", []))


def models() -> list[GalleryModel]:
    """Every known model whose file is actually on disk, in the order to show them."""
    found: list[GalleryModel] = []
    for model_id, label, kind, note in KNOWN:
        path = MODELS_DIR / (model_id + ".glb")
        if not path.is_file():
            logger.warning("内置模型 %s 没有随包（%s），选择器里不列它", model_id, path)
            continue
        found.append(
            GalleryModel(
                model_id=model_id,
                label=label,
                kind=kind,
                note=note,
                bytes=path.stat().st_size,
                clips=count_clips(path),
            )
        )
    return found


def resolve(value: object) -> GalleryModel | None:
    """Turn a stored ``pet.figure`` value into a built-in model, or ``None``.

    Anything that is not ``builtin:<known id>`` is not ours -- including a built-in that
    has since been dropped from the package, which is why the lookup goes through
    :func:`models` and not through :data:`KNOWN`.
    """
    text = str(value or "")
    if not text.startswith(BUILTIN_PREFIX):
        return None
    wanted = text[len(BUILTIN_PREFIX) :]
    return next((item for item in models() if item.model_id == wanted), None)
