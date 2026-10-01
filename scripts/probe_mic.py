"""What the microphone actually delivers, measured rather than assumed.

The complaint was "speech recognition is garbage". The clean-speech benchmark says the
model is at 1% character error and the VAD path loses nothing, so the remaining
suspects are all in front of the recogniser: input gain, the selected device, and
background level. None of those are visible anywhere in the product today -- the HUD
has an output meter and no input meter -- which means a user with a quiet mic and a
developer with a loud one see the same flat transcript and blame different things.

Records a few seconds from the default input device, reports peak / RMS / the share of
frames above a speech-like floor, then runs the same audio through SenseVoice so the
text and the level can be read side by side.

    .venv/Scripts/python scripts/probe_mic.py --seconds 6
"""

from __future__ import annotations

import argparse
import sys
import time

RATE = 16_000
"""The pipeline's capture rate; everything downstream assumes it."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="量一次真实麦克风，再跑一遍识别")
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--device", type=int, default=None, help="sounddevice 设备序号")
    parser.add_argument("--list", action="store_true", dest="list_devices")
    args = parser.parse_args(argv)

    import numpy as np
    import sounddevice as sd

    if args.list_devices:
        for index, device in enumerate(sd.query_devices()):
            flag = "输入" if device["max_input_channels"] > 0 else "    "
            print(
                f"  {index:>3}  {flag}  {device['name'][:40]:<40} "
                f"{device['default_samplerate']:.0f}Hz"
            )
        return 0

    print("录音中…（对着麦克风正常说话即可）")
    frames: list[np.ndarray] = []

    def callback(indata, frame_count, time_info, status) -> None:
        frames.append(indata.copy())

    with sd.InputStream(
        samplerate=RATE,
        channels=1,
        dtype="int16",
        device=args.device,
        callback=callback,
    ):
        time.sleep(args.seconds)

    if not frames:
        print("一个样本都没拿到：设备被独占、选错，或者系统禁止录音。")
        return 1

    pcm = np.concatenate([chunk[:, 0] for chunk in frames])
    magnitude = np.abs(pcm.astype(np.float64))
    peak = float(magnitude.max()) if magnitude.size else 0.0
    rms = float(np.sqrt((magnitude**2).mean())) if magnitude.size else 0.0
    # A frame "carrying sound" is one above ~-45 dBFS: below that, SenseVoice is
    # transcribing the noise floor and any text it produces is a hallucination.
    frame = 512
    energies = [
        float(np.sqrt((pcm[i : i + frame].astype(np.float64) ** 2).mean()))
        for i in range(0, len(pcm) - frame, frame)
    ]
    voiced = sum(1 for value in energies if value > 18.0)
    print(f"时长            : {len(pcm) / RATE:.1f}s")
    dbfs = 20 * np.log10(peak / 32767) if peak else -99.0
    print(f"峰值            : {peak:.0f} / 32767  ({dbfs:.1f} dBFS)")
    print(f"RMS             : {rms:.1f}")
    share = 100 * voiced / max(1, len(energies))
    print(f"有声音的帧      : {voiced}/{len(energies)}  ({share:.0f}%)")
    verdict = []
    if peak < 1500:
        verdict.append("峰值过低——麦克风基本没收到声音（检查输入设备、系统录音音量、应用权限）")
    elif peak > 30000:
        verdict.append("峰值削顶——太响或在爆音")
    if voiced / max(1, len(energies)) < 0.05:
        verdict.append("几乎没有有声帧——要么没人说话，要么增益太低")
    print("判读            : " + ("；".join(verdict) if verdict else "电平正常"))

    print("\n跑一遍 SenseVoice…")
    from jarvis.asr.engines import SenseVoiceAsrEngine
    from jarvis.config.schema import AsrSection

    section = AsrSection(
        enabled=True,
        engine="sensevoice",
        model="iic/SenseVoiceSmall",
        language="zh",
        temperature=0.0,
        beam_size=5,
        device="cpu",
        punctuation=False,
    )
    engine = SenseVoiceAsrEngine(section)
    result = engine.recognize(pcm.astype("<i2").tobytes())
    print(f"识别结果        : {result.text!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
