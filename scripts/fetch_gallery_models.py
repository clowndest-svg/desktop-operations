"""Rebuild the built-in pet models from their originals on the author's Drive.

The ``.glb`` files are not in git (same arrangement as ``avatar/小夜.vrm``: a binary
blob the repo has no reason to carry), so a fresh clone cannot build the pet page
without this script. It downloads the ``.gltf`` from the authors' public Drive folders,
re-packs it into ``.glb`` (the shipped files inline their binary as base64, which costs
~40% more bytes), and refuses to write a file whose checksum is not the one the app was
verified with -- a silent change in an asset that ships inside a paid deliverable is
exactly the kind of thing that should stop here rather than show up on somebody's
desktop.

Run it with no arguments; it prints what it fetched and where the license text lives.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESTINATION = ROOT / "frontend" / "public" / "models"
UA = {"User-Agent": "Mozilla/5.0"}

#: Quaternius packs. Terms: Quaternius Asset License v1.0 -- commercial use and selling a
#: product that incorporates these are allowed; redistributing the models as an asset pack
#: is not. Verbatim clauses in ``docs/内置模型与许可.md``.
PACKS = {
    "animals": "1uJ3N5HfB7jKTseJUNQr3N4YaN0UuEtHk",
    "monsters": "1zLLO_7ZoWgUsS4uooYnVSErQYRu1VdS0",
    "rpg": "1MIRQXLfTd21HMI5rwOb6Xy0rv0xv1m8b",
}

#: model -> (pack, expected md5 of the ``.glb`` this program ships and tests against)
WANTED = {
    "ShibaInu": ("animals", "e87adaaec07ba1d0462024342438cf56"),
    "Fox": ("animals", "e8f9b5a04efb9b17608e509677d7af3e"),
    "Alpaca": ("animals", "72ef460b0e5f79d80b28fb8b0d82278b"),
    "Panda": ("monsters", "a970ba4524712a363b172f91b60ae8cc"),
    "YellowDragon": ("monsters", "68ce14ff37233521eb7c38ba4bc2bb0c"),
    "Ghost": ("monsters", "7c05222743c8cfd3a239670b7d62374f"),
    "Mushroom": ("monsters", "899062b353d22a529d2e193aa2b501e0"),
    "Cthulhu": ("monsters", "c1938b4264082ac543c8cb1536e3e80c"),
    "Wizard": ("rpg", "7a7e42417880796e8dd92a5c74cb07a9"),
    "Cleric": ("rpg", "da615c371e18576a831e0c849d4040c0"),
    "Monk": ("rpg", "b9dcd5467ba4a11d0f633ece1087e128"),
}


def _get(url: str, timeout: int = 60) -> bytes:
    """Fetch, retrying the resets this network is known for.

    A multi-megabyte binary gets dropped mid-stream often enough here that "it worked
    twice already" is not a reason to fail the build on the third try. Three attempts,
    then a real error -- an infinite retry loop would just hide a dead URL.
    """
    last: Exception | None = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(request, timeout=timeout).read()
        except Exception as exc:  # urllib raises a dozen unrelated types here
            last = exc
            print(f"  第 {attempt + 1} 次没拿到（{str(exc)[:60]}），等一下再试")
            time.sleep(2 + 3 * attempt)
    raise SystemExit(f"拿不到 {url}：{last}")


def listing(folder_id: str) -> list[tuple[str, str, str]]:
    """``(kind, id, title)`` for a public Drive folder, via the embedded viewer.

    The normal folder URL answers 404 to a script; the embedded view is the same data
    and the entry id sits in the ``entry-<id>`` attribute.
    """
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#list"
    html = _get(url).decode("utf-8", "replace")
    rows: list[tuple[str, str, str]] = []
    for chunk in html.split('<div class="flip-entry"')[1:]:
        ident = re.search(r'id="entry-([A-Za-z0-9_-]+)"', chunk)
        title = re.search(r'flip-entry-title">([^<]+)', chunk)
        kind = "folder" if 'aria-label="Folder"' in chunk else "file"
        if ident and title:
            rows.append((kind, ident.group(1), title.group(1).strip()))
    return rows


def gltf_folder(pack: str) -> str:
    for kind, ident, title in listing(PACKS[pack]):
        if kind == "folder" and title == "glTF":
            return ident
    raise SystemExit(f"pack {pack} has no glTF folder any more; update this script")


def download(file_id: str) -> bytes:
    """Fetch one Drive file, walking past the interstitial big files get.

    ``uc?export=download`` answers a file it will not scan with an HTML page whose form
    posts back with ``confirm=t``. A ``.gltf`` starts with ``{``, so "is this JSON" is a
    complete test -- and the alternative is a mysterious parse error later.
    """
    body = _get(f"https://drive.google.com/uc?export=download&id={file_id}")
    if body[:1] == b"{":
        return body
    page = body.decode("utf-8", "replace")
    title = re.search(r"<title>([^<]*)</title>", page)
    form = re.search(r'action="([^"]+)"', page)
    if not form:
        # "Google Drive - Quota exceeded" is the one that bites after a bulk fetch: the
        # folder is public but the download counter is shared, and it resets by itself.
        raise SystemExit(
            f"download {file_id}: 取不到文件，页面标题是「{title.group(1) if title else '未知'}」"
        )
    target = urllib.parse.unescape(form.group(1)).replace("&amp;", "&")
    token = re.search(r"confirm=([A-Za-z0-9_-]+)", page)
    if token and "confirm=" not in target:
        target += "&confirm=" + token.group(1)
    return _get(target, timeout=300)


def to_glb(gltf: bytes, name: str) -> bytes:
    """Re-pack a ``.gltf`` whose single buffer is a base64 data URI into a ``.glb``."""
    if gltf[:1] != b"{":
        # Drive answers a request it does not like with an HTML page, and the failure
        # otherwise surfaces as a JSON error three functions away from the cause.
        raise SystemExit(f"{name}: 拿回来的不是 .gltf，前 40 字节 {gltf[:40]!r}")
    model = json.loads(gltf.decode("utf-8"))
    buffers = model.get("buffers", [])
    blob = b""
    if buffers:
        uri = str(buffers[0].get("uri", ""))
        if not uri.startswith("data:"):
            raise SystemExit(f"expected an inlined buffer, got {uri[:40]!r}")
        _, _, encoded = uri.partition(",")
        blob = base64.b64decode(encoded)
        buffers[0].pop("uri")
        buffers[0]["byteLength"] = len(blob)
    for view in model.get("bufferViews", []):
        if view["byteOffset"] + view["byteLength"] > len(blob):
            raise SystemExit("bufferView runs past the binary; refusing to write it")
    if any("uri" in image for image in model.get("images", [])):
        raise SystemExit("this file references an external texture; the page serves one URL")
    payload = json.dumps(model, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload += b" " * (-len(payload) % 4)
    blob += b"\x00" * (-len(blob) % 4)
    return (
        struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(payload) + 8 + len(blob))
        + struct.pack("<II", len(payload), 0x4E4F534A)
        + payload
        + struct.pack("<II", len(blob), 0x004E4942)
        + blob
    )


def main() -> int:
    DESTINATION.mkdir(parents=True, exist_ok=True)
    index: dict[str, dict[str, str]] = {}
    for pack in sorted({item[0] for item in WANTED.values()}):
        folder = gltf_folder(pack)
        index[pack] = {title: ident for kind, ident, title in listing(folder) if kind == "file"}

    for name, (pack, expected) in WANTED.items():
        target = DESTINATION / (name + ".glb")
        if target.is_file() and hashlib.md5(target.read_bytes()).hexdigest() == expected:
            print(f"  跳过 {target.name}（已经在了，md5 对得上）")
            continue
        entry = index[pack].get(name + ".gltf")
        if not entry:
            raise SystemExit(f"{name}.gltf is not in the {pack} pack any more")
        packed = to_glb(download(entry), name)
        digest = hashlib.md5(packed).hexdigest()
        if digest != expected:
            raise SystemExit(f"{name}: md5 {digest}, expected {expected}")
        target.write_bytes(packed)
        print(f"  取的 {target.name}  {len(packed)} 字节  md5 {digest}")
    print(f"共 {len(WANTED)} 个模型在 {DESTINATION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
