"""Build platform branding from the supplied logo; never download art at build time."""

from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets-src/brand/xiaoye-logo.jpg"
WEB = ROOT / "frontend/src/assets/brand"


def main() -> None:
    WEB.mkdir(parents=True, exist_ok=True)
    for name in ("portrait", "speaking", "blink"):
        with Image.open(SOURCE.parent / f"{name}.png") as image:
            image.convert("RGBA").save(WEB / f"{name}.webp", quality=90, method=6)
    with Image.open(SOURCE) as original:
        logo = original.convert("RGBA")
        logo.resize((640, 640), Image.Resampling.LANCZOS).save(WEB / "logo.webp")
        logo.resize((64, 64), Image.Resampling.LANCZOS).save(ROOT / "frontend/public/favicon.png")
        res = ROOT / "frontend/android/app/src/main/res"
        for density, size in {
            "mdpi": 48,
            "hdpi": 72,
            "xhdpi": 96,
            "xxhdpi": 144,
            "xxxhdpi": 192,
        }.items():
            target = res / f"mipmap-{density}"
            target.mkdir(parents=True, exist_ok=True)
            for name in ("ic_launcher", "ic_launcher_round"):
                logo.resize((size, size), Image.Resampling.LANCZOS).save(target / f"{name}.png")
            # Adaptive foreground has a 66% safe zone; keep the complete supplied mark.
            canvas = Image.new("RGBA", (size * 9 // 4, size * 9 // 4))
            mark = logo.resize((size * 3 // 2, size * 3 // 2), Image.Resampling.LANCZOS)
            at = (canvas.width - mark.width) // 2
            canvas.alpha_composite(mark, (at, at))
            canvas.save(target / "ic_launcher_foreground.png")
        for path in res.glob("drawable*/splash.png"):
            with Image.open(path) as old:
                splash = Image.new("RGB", old.size, "#100b20")
            size = min(splash.size) // 2
            mark = logo.resize((size, size), Image.Resampling.LANCZOS)
            splash.paste(mark, ((splash.width - size) // 2, (splash.height - size) // 2), mark)
            splash.save(path)
    shutil.copyfile(ROOT / "frontend/public/favicon.png", ROOT / "frontend/mobile/favicon.png")
    print("Brand web, Android launcher and splash assets updated.")


if __name__ == "__main__":
    main()
