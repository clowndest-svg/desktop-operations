"""Copy an unstarted runtime into another edition without recompiling shared code.

Edition is an explicit external buildinfo field read before application startup.
Runtime files are copied; only immutable weights may share an inode.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path


def derive(source: Path, destination: Path, edition: str) -> None:
    source, destination = source.resolve(), destination.resolve()
    if destination.exists() or source == destination or source in destination.parents:
        raise ValueError("Destination must be a new, separate runtime directory")
    if any((source / name).exists() for name in ("数据", "用户数据", "preferences.json", ".env")):
        raise ValueError("Refusing to copy a runtime containing user data")
    metadata = source / "_internal/jarvis/buildinfo.json"
    info = json.loads(metadata.read_text(encoding="utf-8"))
    if not info.get("source_sha256") or not (source / "小夜.exe").is_file():
        raise ValueError("Source is not a stamped desktop runtime")

    def copy_file(src: str, dst: str) -> str:
        if Path(src).suffix.lower() in {".gguf", ".safetensors", ".pt"}:
            try:
                os.link(src, dst)
                return dst
            except OSError:
                pass
        return shutil.copy2(src, dst)

    shutil.copytree(source, destination, copy_function=copy_file)
    info["derived_from_edition"] = info.get("edition")
    info["edition"] = edition
    (destination / "_internal/jarvis/buildinfo.json").write_text(
        json.dumps(info, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Prepared {edition} runtime: {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--edition", choices=("author", "buyer"), required=True)
    args = parser.parse_args()
    derive(args.source, args.destination, args.edition)
