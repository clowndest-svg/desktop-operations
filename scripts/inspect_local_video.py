"""Decode a clip and make a contact sheet for visual release verification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    capture = cv2.VideoCapture(str(args.video))
    frames = []
    ok, frame = capture.read()
    while ok:
        frames.append(frame)
        ok, frame = capture.read()
    if not frames:
        raise ValueError("MP4 无法解码")
    indices = np.linspace(0, len(frames) - 1, 4).astype(int)
    cv2.imwrite(str(args.output), np.hstack([frames[index] for index in indices]))
    print(
        json.dumps(
            {
                "frames": len(frames),
                "fps": capture.get(cv2.CAP_PROP_FPS),
                "width": capture.get(cv2.CAP_PROP_FRAME_WIDTH),
                "height": capture.get(cv2.CAP_PROP_FRAME_HEIGHT),
            }
        )
    )


if __name__ == "__main__":
    main()
