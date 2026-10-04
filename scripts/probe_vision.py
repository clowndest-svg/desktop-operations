"""P1-4's gate: two questions about seeing, answered with measurements, not hopes.

Run it from a shell that has an API key -- that is the only part an agent cannot do here,
and the reason no screen tool is registered:

    setx QWENAI_API_KEY "sk-..."      # then reopen the shell
    .venv\\Scripts\\python scripts\\probe_vision.py [provider]

It answers, separately:

1. **Does this provider take an image?** One request with a 64×64 red PNG and
   "图里是什么颜色". A provider that answers 红 supports vision; one that 400s does not.
2. **Is OCR installed on *this* install path?** ``rapidocr-onnxruntime`` is an optional
   extra; a screen-reading tool built on it would be the ``pyautogui`` mistake all over
   again -- registered, advertised, and guaranteed to fail for anybody who installed
   ``[desktop]`` rather than ``[vision]``.

The verdict line says which of the two routes is open. Registering ``screen_capture`` /
``read_screen`` is only correct after one of them reads 可以接.
"""

from __future__ import annotations

import base64
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# A 64x64 solid-red PNG, built here so the probe needs no asset files.
_QUESTION = "这张图整体是什么颜色？只回答颜色名。"


def _red_png_data_url() -> str:
    size = 64

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            len(payload).to_bytes(4, "big")
            + kind
            + payload
            + zlib.crc32(kind + payload).to_bytes(4, "big")
        )

    raw = b"".join(
        b"\x00" + b"\xff\x00\x00" * size for _ in range(size)
    )  # one filter byte + red pixels per row
    ihdr = size.to_bytes(4, "big") * 2 + b"\x08\x02\x00\x00\x00"
    png = b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", ihdr),
            chunk(b"IDAT", zlib.compress(raw)),
            chunk(b"IEND", b""),
        ]
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def probe_provider(name: str | None) -> bool:
    from dataclasses import replace

    from jarvis.config.service import ConfigService
    from jarvis.llm.service import LlmService
    from jarvis.llm.types import ChatMessage

    config = ConfigService()
    config.start()
    service = LlmService(lambda: config.config.llm)
    service.start()
    try:
        client = service.client_for(name) if name else service.client
    except Exception as exc:
        print(f"  · 拿不到 client：{exc}")
        return False

    print(f"1) provider={client.provider_name} model={client.model} 带图提问：")
    message = replace(ChatMessage.user(_QUESTION), images=(_red_png_data_url(),))
    from jarvis.llm.openai_compat import OpenAiCompatClient

    if not isinstance(client, OpenAiCompatClient):
        print("  · 不是 OpenAI 兼容客户端，探不了")
        config.stop()
        return False
    payload = client._payload([message], None, stream=False)
    print(f"  · 请求体里 messages[0] 的键：{sorted(payload['messages'][0])}")
    if not isinstance(payload["messages"][0].get("content"), list):
        print("  · 当前代码没有把图放进请求体，所以这一步必然测不出多模态。")
        print("    先把 _serialize_message 改成 content 数组再回来跑这个脚本。")
        config.stop()
        return False
    try:
        reply = client.complete([message])
    except Exception as exc:
        print(f"  · 被拒：{type(exc).__name__}: {str(exc)[:220]}")
        print("  结论：这个 provider 吃不了图，P1-4 整条搁置。")
        config.stop()
        return False
    answer = (reply.content or "").strip()
    print(f"  · 回答：{answer[:120]!r}")
    ok = "红" in answer
    print(f"  结论：{'可以接' if ok else '回答里没有「红」，按不能吃图处理'}")
    config.stop()
    return ok


def probe_ocr() -> bool:
    print("\n2) 这条安装路径上有没有 OCR：")
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception as exc:
        print(f"  · rapidocr_onnxruntime 不可用（{type(exc).__name__}）")
        print("  结论：屏幕取字工具不能注册——它会在每次调用时失败，正是 pyautogui 那次的错法。")
        return False
    print("  · rapidocr_onnxruntime 可导入")
    print("  结论：OCR 这条路可以接（还要确认打包 hiddenimports 里有它）。")
    return True


def main() -> int:
    provider = sys.argv[1] if len(sys.argv) > 1 else None
    vision = probe_provider(provider)
    ocr = probe_ocr()
    print("\n判定：")
    print(f"  多模态截图问答：{'可以接' if vision else '不接'}")
    print(f"  屏幕 OCR 取字：{'可以接' if ocr else '不接'}")
    if not vision and not ocr:
        print("  → P1-4 保持搁置，不给模型注册任何看屏幕的工具。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
