"""Real Edge-TTS audio through the page's transport, written out for a browser.

Run with the project venv. It produces the *exact* payloads ``jarvis.ui.audio_bridge``
would push into the desktop window, so a browser can replay them and prove the seam
(base64, slicing, ordering, the final flag) against audio that is actually someone
speaking rather than a sine the test invented.

Output goes to ``build/``, never to ``jarvis/ui/web/``: the packaging spec copies
that directory whole, so a probe file left in it would ship inside the exe. To
replay in a browser, serve the repository root and open the bundle by path::

    python -m http.server 8734        # from the repo root
    # then http://127.0.0.1:8734/jarvis/ui/web/index.html
    # and in devtools:  fetch('/build/probe-audio.json').then(r => r.json())
"""

from __future__ import annotations

import base64
import json
import wave
from pathlib import Path

from jarvis.config.schema import TtsSection
from jarvis.orchestration.player import BridgePlayer
from jarvis.tts.engines import EdgeTtsEngine
from jarvis.ui.audio_bridge import AudioPusher

ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = ROOT / "build" / "probe-audio.json"
OUT_WAV = ROOT / "build" / "probe-audio.wav"

TEXT = "你好，我是小夜。现在我的声音是从界面里播出来的，所以画面上的每一根柱子都是量出来的。"


class RecordingWindow:
    """Stands in for ``webview.Window`` and keeps every script it was handed."""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def evaluate_js(self, script: str) -> None:
        self.scripts.append(script)


def payloads(scripts: list[str]) -> list[dict[str, object]]:
    out = []
    for script in scripts:
        start = script.index("({") + 1
        end = script.rindex("})") + 1
        out.append(json.loads(script[start:end]))
    return out


def main() -> int:
    section = TtsSection(
        enabled=True,
        engine="edge_tts",
        voice="zh-CN-XiaoxiaoNeural",
        speed=1.0,
        volume=1.0,
        device="cpu",
        model="",
    )
    engine = EdgeTtsEngine(section)
    pusher = AudioPusher()
    window = RecordingWindow()
    pusher.attach_window(window)
    pusher.mark_ready(True)
    player = BridgePlayer(pusher.emit)

    pcm_parts = []
    for chunk in engine.synthesize(TEXT):
        player.play(chunk)
        pcm_parts.append(chunk.audio)
        rate = chunk.sample_rate
    pcm = b"".join(pcm_parts)
    sent = b"".join(base64.b64decode(str(item["pcm"])) for item in payloads(window.scripts))

    print(f"synthesized bytes : {len(pcm)}")
    print(f"pushed slices     : {len(window.scripts)}")
    print(f"decoded bytes     : {len(sent)}")
    print(f"lossless          : {sent == pcm}")
    print(f"sent_bytes counter: {pusher.sent_bytes}")
    print(f"output device     : {'browser' if pusher.ready else 'speaker'}")

    OUT_WAV.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT_WAV), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    OUT_JSON.write_text(
        json.dumps({"sample_rate": rate, "messages": payloads(window.scripts)}),
        encoding="utf-8",
    )
    print(f"wrote             : {OUT_JSON.name}, {OUT_WAV.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
