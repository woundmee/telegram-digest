"""Configuration from the process environment."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .selection import article_key


def split_list(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


def read_username(env: Mapping[str, str], key: str) -> str:
    username = env.get(key, "").strip().removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", username) or username.isdecimal():
        raise ValueError(f"укажите {key} в формате @username")
    return username.lower()


@dataclass(frozen=True, slots=True)
class Config:
    token: str
    admin_username: str
    timezone: ZoneInfo
    extra_sources: tuple[str, ...]
    database: Path
    tg_channels: tuple[str, ...] = ()
    subscription_interval_minutes: int = 60
    digest_interval_hours: int = 10

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        token = env.get("TELEGRAM_BOT_TOKEN", "")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("укажите TELEGRAM_BOT_TOKEN от BotFather")
        admin_username = read_username(env, "ADMIN_USERNAME")
        try:
            timezone = ZoneInfo(env.get("TIMEZONE", "Europe/Moscow"))
        except ZoneInfoNotFoundError:
            raise ValueError("TIMEZONE должен содержать имя часового пояса IANA") from None
        extra_sources = split_list(env.get("SOURCE_EXTRA_URLS", ""))
        for url in extra_sources:
            article_key(url)
        if not (database := env.get("DB_PATH", "rblog.sqlite3").strip()):
            raise ValueError("DB_PATH не может быть пустым")
        tg_channels = tuple(dict.fromkeys(
            read_username({"TG_CHANNELS": channel}, "TG_CHANNELS")
            for channel in split_list(env.get("TG_CHANNELS", ""))
        ))
        interval = int(env.get("SUBSCRIPTION_INTERVAL_MINUTES", "60"))
        if not 1 <= interval <= 1440:
            raise ValueError("SUBSCRIPTION_INTERVAL_MINUTES должен быть от 1 до 1440")
        digest_hours = int(env.get("DIGEST_INTERVAL_HOURS", "10"))
        if not 1 <= digest_hours <= 168:
            raise ValueError("DIGEST_INTERVAL_HOURS должен быть от 1 до 168")
        return cls(token, admin_username, timezone, extra_sources, Path(database),
                   tg_channels, interval, digest_hours)
