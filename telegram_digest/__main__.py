"""Run the Telegram bot using environment variables."""

from contextlib import closing
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import sqlite3
from threading import Event

from .bot import Bot
from .config import Config
from .storage import Storage
from .telegram import Telegram, TelegramError


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger(__name__)
    log_dir = Path(os.environ.get("STATE_DIRECTORY", ".")) / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_dir / "bot.log", maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    except OSError as error:
        log.error("Не удалось открыть журнал: %s", error)
        return 1
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    try:
        config = Config.from_env(os.environ)
    except ValueError as error:
        log.error("Ошибка конфигурации: %s", error)
        return 2

    stopped = Event()

    def stop(signum, frame) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        with closing(Storage(config.database)) as storage:
            Bot(config, Telegram(config.token), storage, stopped).run()
    except (sqlite3.Error, TelegramError) as error:
        log.error("Бот остановлен: %s", error)
        return 1
    log.info("Бот остановлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
