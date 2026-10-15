"""Golden media descriptions against an explicitly supplied, key-free local endpoint."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jarvis.app.media_prompt import PromptTranslator
from jarvis.llm.openai_compat import OpenAiCompatClient, OpenAiCompatSettings

CASES = {
    "zh": "三只红猫坐在蓝色长椅上，没有帽子。",
    "en": "Three red cats sit on a blue bench without hats.",
    "fr": "Trois chats rouges sont assis sur un banc bleu, sans chapeaux.",
    "de": "Drei rote Katzen sitzen auf einer blauen Bank, ohne Hüte.",
    "es": "Tres gatos rojos están sentados en un banco azul, sin sombreros.",
    "pt": "Três gatos vermelhos estão sentados em um banco azul, sem chapéus.",
    "ja": "三匹の赤い猫が青いベンチに座っていて、帽子はありません。",
    "ko": "빨간 고양이 세 마리가 파란 벤치에 앉아 있고 모자는 없습니다.",
    "ru": "Три красных кота сидят на синей скамейке без шляп.",
    "ar": "ثلاث قطط حمراء تجلس على مقعد أزرق بدون قبعات.",
    "hi": "तीन लाल बिल्लियाँ नीली बेंच पर बैठी हैं, कोई टोपी नहीं है।",
    "tr": "Üç kırmızı kedi mavi bir bankta oturuyor, şapka yok.",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--out", type=Path, default=Path("D:/AI/XiaoYe/qa/translation-qwen-r119.json")
    )
    args = parser.parse_args()
    if not args.base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError("This verification accepts only a local loopback endpoint")
    client = OpenAiCompatClient(
        OpenAiCompatSettings(
            provider_name="local-translation-qa",
            base_url=args.base_url,
            model=args.model,
            api_key_env="",
            timeout_seconds=180,
            max_retries=0,
            retry_backoff_seconds=0,
            key_optional=True,
        ),
        environ={},
    )
    translator = PromptTranslator(environ={}, client_provider=lambda: client)
    rows = []
    for language, source in CASES.items():
        started = time.perf_counter()
        try:
            result = translator.translate(source)
            text = result.english.lower()
            checks = {
                "count": bool(re.search(r"\b(three|3)\b", text)),
                "cats": bool(re.search(r"\bcats?\b", text)),
                "colors": "red" in text and "blue" in text,
                "seating": any(word in text for word in ("bench", "seat", "long chair")),
                "negative_hats": bool(re.search(r"\b(no|without|not|don't|doesn't)\b", text))
                and bool(re.search(r"\b(hats?|caps?|headwear)\b", text)),
            }
            row = {
                "language": language,
                "source": source,
                "english": result.english,
                "checks": checks,
                "ok": all(checks.values()),
                "seconds": round(time.perf_counter() - started, 3),
            }
        except Exception as exc:
            row = {"language": language, "ok": False, "error": str(exc)}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    passed = all(row["ok"] for row in rows)
    args.out.write_text(
        json.dumps(
            {"ok": passed, "model": args.model, "cases": rows}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
