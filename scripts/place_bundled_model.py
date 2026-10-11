"""Put the bundled weights next to a freshly built 小夜 so the buyer's first run has them.

PyInstaller does not carry the 4.36 GB ``.gguf``: copying it through the spec would make
every build re-copy four gigabytes into ``_internal`` for no benefit, and a buyer who
wants to drop the model out should be able to see it. So the runtime ships inside the
package (``jarvis/vendor/llama``, see ``packaging/jarvis.spec``) and the weights land in
``<安装目录>/模型/`` afterwards, which is exactly where ``local_server.model_file()`` looks.

Hard-linked when the destination is on the same volume -- the file is four gigabytes and
the repo already holds a copy, so a copy would just be a second copy.

Usage::

    .venv\\\\Scripts\\\\python.exe scripts/place_bundled_model.py --round r72
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

BUILD_ROOT = Path("E:/BianChengGongJu/JarvisBuild")
APP_DIRNAME = "".join([chr(0x5C0F), chr(0x591C)])  # 小夜, without depending on the console codepage
SOURCE_DIR = Path(__file__).resolve().parent.parent / "模型"
#: Measured with ``--record``; the check that stops a swapped file shipping silently.
#: 每一份随包权重的指纹（``--record`` 打出来抄进来的）。发出去的包靠它对"下载断了重下哪一个"，
#: 不在这张表里的文件只摆进去、不核对，并且会喊一声。
MODEL_MD5 = {
    "Qwen2.5-7B-Instruct.Q4_K_M.gguf": "86de4a7a1f21f1ae6baa0abaa0cdd37b",
    "Qwen3-0.6B-Q8_0.gguf": "fcd6e70c809c010b283b3f6b4ecc91fe",
    "Qwen3-1.7B-Q8_0.gguf": "c612e1abbc49f966570fb7fb2d62c878",
    "Qwen3-4B.Q4_K_M.gguf": "838803c0c88871f001067f1f5c364d54",
}
CHUNK = 1024 * 1024 * 8


def digest(path: Path) -> str:
    hash_ = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            hash_.update(block)
    return hash_.hexdigest()


def latest_round() -> str:
    rounds = [
        p
        for p in BUILD_ROOT.iterdir()
        if p.is_dir() and (p / "dist" / APP_DIRNAME / f"{APP_DIRNAME}.exe").is_file()
    ]
    if not rounds:
        raise SystemExit("JarvisBuild 下没有一个真有 dist/小夜/小夜.exe 的轮次")
    return max(rounds, key=lambda p: p.stat().st_mtime).name


def place(target: Path, source: Path, *, expect: str | None) -> str:
    target.mkdir(parents=True, exist_ok=True)
    landing = target / source.name
    if landing.exists():
        same = landing.stat().st_size == source.stat().st_size
        if not same:
            raise SystemExit(f"{landing} 已经在了但大小不同，先手工确认再删")
        how = "已在那儿"
    else:
        try:
            os.link(source, landing)
            how = "硬链接"
        except OSError:
            shutil.copy2(source, landing)
            how = "拷过去"
    if expect is not None:
        actual = digest(landing)
        if actual != expect:
            raise SystemExit(
                f"{landing.name} md5 {actual}，和记下来的 {expect} 不一样 —— 别发这一版"
            )
    else:
        actual = digest(landing)
    return f"{how} {landing}  {landing.stat().st_size:,} 字节  md5 {actual}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", default="auto", help="rNN，或 auto 取最新一版")
    parser.add_argument("--src", default="", help="权重所在目录，默认仓库根的 模型/")
    parser.add_argument("--record", action="store_true", help="只打印 md5，不落位")
    args = parser.parse_args(argv)

    source_dir = Path(args.src) if args.src else SOURCE_DIR
    weights = sorted(source_dir.glob("*.gguf"), key=lambda path: -path.stat().st_size)
    if not weights:
        raise SystemExit(f"{source_dir} 里没有 .gguf")

    if args.record:
        for source in weights:
            print(f'    "{source.name}": "{digest(source)}",')
        return 0

    # 每一份都摆进去：界面上那个"选哪个离线模型"列的就是这一堆，
    # 只放最大的会让买家看到四个选项、点哪个都是"文件不在包里"。
    round_name = latest_round() if args.round == "auto" else args.round
    target = BUILD_ROOT / round_name / "dist" / APP_DIRNAME / SOURCE_DIR.name
    for source in weights:
        expect = MODEL_MD5.get(source.name)
        if expect is None:
            print(f"注意：{source.name} 没在 MODEL_MD5 里，只核对存在、不核对内容")
        print(place(target, source, expect=expect))
    return 0


if __name__ == "__main__":
    sys.exit(main())
