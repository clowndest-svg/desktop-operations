"""Run the QQ bot with ``python -m jarvis.qqbot``."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from jarvis.qqbot.client import QqBotClient
from jarvis.qqbot.settings import QqBotSettings, load_dotenv


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    try:
        settings = QqBotSettings.from_env()
    except (ValueError, OSError) as exc:
        logging.getLogger("jarvis.qqbot").error("QQ bot configuration error: %s", exc)
        return 2

    bot = QqBotClient(settings)
    try:
        bot.run(appid=settings.appid, secret=settings.secret)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
