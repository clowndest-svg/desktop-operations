"""闲鱼上架前要回答的一件事：这套东西依赖的许可允许你卖吗。

只看项目自己声明的直接依赖（pyproject 的 dependencies + 可选 extras），
把每个包的 License / License-Expression 抄出来。不猜，不凭记忆。
"""

from __future__ import annotations

import tomllib
from importlib.metadata import distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

wanted: set[str] = set()
for spec in data["project"].get("dependencies", []):
    wanted.add(
        spec.split("[")[0].split("=")[0].split(">")[0].split("<")[0].split("!")[0].strip().lower()
    )
for group in data["project"].get("optional-dependencies", {}).values():
    for spec in group:
        wanted.add(
            spec.split("[")[0]
            .split("=")[0]
            .split(">")[0]
            .split("<")[0]
            .split("!")[0]
            .strip()
            .lower()
        )

found: dict[str, str] = {}
for dist in distributions():
    name = (dist.metadata["Name"] or "").lower()
    if name not in wanted:
        continue
    license_text = (dist.metadata.get("License") or "").strip()
    expression = (dist.metadata.get("License-Expression") or "").strip()
    classifiers = [
        value.split("::")[-1].strip()
        for key, value in dist.metadata.items()
        if key == "Classifier" and value.startswith("License ::")
    ]
    shown = expression or license_text or "; ".join(classifiers) or "?"
    found[name] = " ".join(shown.split())[:70]

print(f"直接依赖 {len(wanted)} 个，本机装到的 {len(found)} 个：\n")
for name in sorted(found):
    print(f"| {name} | {found[name]} |")
missing = sorted(wanted - set(found))
if missing:
    print("\n未安装（按 PyPI 上的许可自己核）：" + "、".join(missing))
