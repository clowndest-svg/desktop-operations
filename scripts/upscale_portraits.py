"""Build-time portrait upscale with the RealESRGAN x4 model already in the engine.

The animated frames must keep their exact composition: a uniform 4x pass is used
instead of face-aware retouching, so ``rigs.json`` coordinates stay valid for the
high-resolution copies.  Only the desktop bundle consumes ``public/portraits-hd``;
the mobile build disables the public directory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from torch import nn
from torch.nn import functional

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "frontend/src/assets/portraits"
DEFAULT_MODEL = Path("D:/AI/XiaoYe/engine/ComfyUI/models/upscale_models/RealESRGAN_x4plus.pth")
DEFAULT_OUT = ROOT / "frontend/public/portraits-hd"
TILE = 256
OVERLAP = 16


class ResidualDenseBlock(nn.Module):
    def __init__(self, num_feat: int = 64, num_grow_ch: int = 32) -> None:
        super().__init__()
        self.conv1 = nn.Conv2d(num_feat, num_grow_ch, 3, 1, 1)
        self.conv2 = nn.Conv2d(num_feat + num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv3 = nn.Conv2d(num_feat + 2 * num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv4 = nn.Conv2d(num_feat + 3 * num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv5 = nn.Conv2d(num_feat + 4 * num_grow_ch, num_feat, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(0.2, inplace=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x1 = self.lrelu(self.conv1(x))
        x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
        x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
        x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
        x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
        return x5 * 0.2 + x


class RRDB(nn.Module):
    def __init__(self, num_feat: int = 64, num_grow_ch: int = 32) -> None:
        super().__init__()
        self.rdb1 = ResidualDenseBlock(num_feat, num_grow_ch)
        self.rdb2 = ResidualDenseBlock(num_feat, num_grow_ch)
        self.rdb3 = ResidualDenseBlock(num_feat, num_grow_ch)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.rdb3(self.rdb2(self.rdb1(x)))
        return out * 0.2 + x


class RRDBNet(nn.Module):
    def __init__(
        self, num_in_ch: int = 3, num_out_ch: int = 3, num_feat: int = 64, num_block: int = 23
    ) -> None:
        super().__init__()
        self.conv_first = nn.Conv2d(num_in_ch, num_feat, 3, 1, 1)
        self.body = nn.Sequential(*[RRDB(num_feat) for _ in range(num_block)])
        self.conv_body = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_up1 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_up2 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_hr = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_last = nn.Conv2d(num_feat, num_out_ch, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(0.2, inplace=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        feat = self.conv_first(x)
        feat = feat + self.conv_body(self.body(feat))
        feat = self.lrelu(
            self.conv_up1(functional.interpolate(feat, scale_factor=2, mode="nearest"))
        )
        feat = self.lrelu(
            self.conv_up2(functional.interpolate(feat, scale_factor=2, mode="nearest"))
        )
        return self.conv_last(self.lrelu(self.conv_hr(feat)))


def load_model(path: Path) -> RRDBNet:
    state = torch.load(path, map_location="cpu", weights_only=True)
    if isinstance(state, dict) and "params_ema" in state:
        state = state["params_ema"]
    elif isinstance(state, dict) and "params" in state:
        state = state["params"]
    model = RRDBNet()
    model.load_state_dict(state, strict=True)
    return model.eval()


def upscale_array(model: RRDBNet, pixels: np.ndarray, device: torch.device) -> np.ndarray:
    height, width, _ = pixels.shape
    tensor = torch.from_numpy(pixels).to(device).permute(2, 0, 1).float().div_(255.0)[None]
    output = torch.empty((1, 3, height * 4, width * 4), dtype=tensor.dtype, device=device)
    step = TILE - OVERLAP * 2
    with torch.inference_mode():
        for top in range(0, height, step):
            for left in range(0, width, step):
                bottom = min(height, top + TILE)
                right = min(width, left + TILE)
                patch = tensor[:, :, top:bottom, left:right]
                rendered = model(patch)
                # Crop the context margin after the network, so seams are not
                # decoded from zero-padded borders.
                margin_top = OVERLAP if top else 0
                margin_left = OVERLAP if left else 0
                margin_bottom = 0 if bottom == height else OVERLAP
                margin_right = 0 if right == width else OVERLAP
                target_top = top + margin_top
                target_left = left + margin_left
                target_bottom = bottom - margin_bottom
                target_right = right - margin_right
                output[
                    :, :, target_top * 4 : target_bottom * 4, target_left * 4 : target_right * 4
                ] = rendered[
                    :,
                    :,
                    margin_top * 4 : (rendered.shape[2] - margin_bottom * 4),
                    margin_left * 4 : (rendered.shape[3] - margin_right * 4),
                ]
    return output[0].permute(1, 2, 0).clamp_(0, 1).mul_(255).round().to(torch.uint8).cpu().numpy()


def upscale_frame(
    model: RRDBNet, source: Path, target: Path, device: torch.device, patch_size: int
) -> dict[str, object]:
    with Image.open(source) as image:
        rgba = image.convert("RGBA")
    rgb = np.asarray(rgba.convert("RGB"), dtype=np.uint8)
    upscaled = upscale_array(model, rgb, device)
    alpha = rgba.getchannel("A").resize((rgba.width * 4, rgba.height * 4), Image.Resampling.LANCZOS)
    result = Image.fromarray(upscaled, "RGB").convert("RGBA")
    result.putalpha(alpha)
    if patch_size and result.width > patch_size:
        # Frames 1-5 are only composited as mouth/eye/face patches. Keeping them
        # at 2K avoids decoding six 4K RGBA bitmaps (about 400 MB) at runtime.
        result = result.resize((patch_size, patch_size), Image.Resampling.LANCZOS)
    target.parent.mkdir(parents=True, exist_ok=True)
    result.save(target, quality=92, method=6)
    return {
        "source": source.name,
        "target": target.name,
        "size": [result.width, result.height],
        "bytes": target.stat().st_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--characters", nargs="+", default=["p01"])
    parser.add_argument("--frames", type=int, default=6)
    parser.add_argument("--patch-size", type=int, default=2048)
    args = parser.parse_args()
    if not args.model.is_file():
        raise SystemExit(f"缺少 RealESRGAN 权重：{args.model}")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = load_model(args.model).to(device)
    entries = []
    for character in args.characters:
        for frame in range(args.frames):
            source = SOURCE / f"{character}-{frame}.webp"
            target = args.out / f"{character}-{frame}.webp"
            patch_size = 0 if frame == 0 else args.patch_size
            entry = upscale_frame(model, source, target, device, patch_size)
            entries.append(entry)
            print(
                f"{source.name} -> {target.name} {entry['size']} {entry['bytes']} bytes", flush=True
            )
    manifest = {"model": args.model.name, "scale": 4, "frames": entries}
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
