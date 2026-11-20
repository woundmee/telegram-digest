"""Owner commands, source checks and persistent digest delivery."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
from math import ceil
from threading import Event
from time import monotonic
from typing import Any

from .channels import collect_channel_posts
from .config import Config
from .selection import Article
from .sources import Source, collect_articles, configured_sources
from .storage import Storage
from .telegram import Telegram, TelegramError


LOG = logging.getLogger(__name__)
POLL_SECONDS = 15
FETCH_COOLDOWN_SECONDS = 60
CATEGORY_TITLES = {"web": "Сбор с сайтов", "tg": "Сбор с Telegram"}


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def digest_parts(category: str, entries: list[tuple[str, str]]) -> list[tuple[str, list[str]]]:
    """Split at entry boundaries, keeping full links and numbered section titles."""
    heading = CATEGORY_TITLES[category]
    parts: list[tuple[str, list[str]]] = []
    text = heading
    keys: list[str] = []
    for url, title in entries:
        title = " ".join(title.split())
        if len(title) > 180:
            title = title[:177].rstrip() + "…"
        entry = f"• {title or 'Публикация'}\n{url}"
        if utf16_length(text + "\n\n" + entry) > 4096:
            if not keys:
                raise ValueError("Ссылка публикации превышает лимит Telegram")
            parts.append((text, keys))
            text = f"{heading} · часть {len(parts) + 1}"
            keys = []
        if utf16_length(text + "\n\n" + entry) > 4096:
            raise ValueError("Ссылка публикации превышает лимит Telegram")
        text += "\n\n" + entry
        keys.append(url)
    if keys:
        parts.append((text, keys))
    elif not parts:
        parts.append((heading + "\n\nНовых публикаций за этот период нет.", []))
    return parts


def as_object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class Bot:
    def __init__(
        self,
        config: Config,
        telegram: Telegram,
        storage: Storage,
        stopped: Event,
        collector: Callable[[tuple[Source, ...]], tuple[list[Article], int]] = collect_articles,
    ) -> None:
        self.config = config
        self.telegram = telegram
        self.storage = storage
        self.stopped = stopped
        self.sources = configured_sources(config)
        self.collect = collector
        self.username = ""
        self.admin_id = storage.owner_id(config.admin_username)
        self.next_fetch_at = 0.0
        self.next_tg_fetch_at = 0.0

    def send(self, chat_id: int, text: str) -> int:
        result = as_object(self.telegram.call(
            "sendMessage", chat_id=chat_id, text=text, entities=[],
            link_preview_options={"is_disabled": True},
        ))
        if type(result.get("message_id")) is not int:
            raise TelegramError("sendMessage")
        return result["message_id"]

    @contextmanager
    def search_notice(self, chat_id: int) -> Iterator[None]:
        message_id = self.send(chat_id, "🔎 Поиск информации")
        try:
            yield
        finally:
            try:
                self.telegram.call("deleteMessage", chat_id=chat_id, message_id=message_id)
            except TelegramError as error:
                LOG.warning("Не удалось удалить сообщение о поиске: %s", error)
                try:
                    self.telegram.call("editMessageText", chat_id=chat_id, message_id=message_id,
                                       text="Поиск завершён")
                except TelegramError as edit_error:
                    LOG.warning("Не удалось обновить сообщение о поиске: %s", edit_error)

    def send_pending_digests(self, admin_id: int) -> None:
        for index, (batch_id, text) in enumerate(self.storage.pending_digests(self.config.admin_username)):
            if self.stopped.wait(1.1 if index else 0):
                break
            if not self.storage.set_digest_state(batch_id, "pending", "sending"):
                continue
            try:
                self.send(admin_id, text)
            except TelegramError as error:
                if error.rejected:
                    self.storage.set_digest_state(batch_id, "sending", "pending")
                LOG.warning("Дайджест %d: %s", batch_id, error)
                break
            else:
                self.storage.set_digest_state(batch_id, "sending", "sent")
                LOG.info("Отправлена часть дайджеста %d", batch_id)

    def manual_digest(self, category: str, articles: list[Article], admin_id: int) -> None:
        self.storage.enqueue_digest(self.config.admin_username, category, articles)
        entries = self.storage.queued_digest(self.config.admin_username, category)
        self.storage.prepare_digest(self.config.admin_username, category, digest_parts(category, entries))
        self.send_pending_digests(admin_id)

    def send_both_digests(self, admin_id: int, next_digest: datetime | None = None) -> None:
        owner = self.config.admin_username
        window = [
            (category, digest_parts(category, self.storage.queued_digest(owner, category)))
            for category in CATEGORY_TITLES
        ]
        self.storage.prepare_digest_window(owner, window, next_digest)
        self.send_pending_digests(admin_id)

    def fetch(self, admin_id: int) -> None:
        self.next_fetch_at = monotonic() + FETCH_COOLDOWN_SECONDS
        with self.search_notice(admin_id):
            articles, successes = self.collect(self.sources)
        now = datetime.now(timezone.utc)
        selected = [item for item in articles if item.published_at <= now]
        if self.sources and not successes:
            self.send(admin_id, "Не удалось прочитать сайты. Подробности в журнале.")
            return
        self.manual_digest("web", selected, admin_id)

    def fetch_tg(self, admin_id: int) -> None:
        if not self.config.tg_channels:
            self.send(admin_id, "Укажите публичные каналы в TG_CHANNELS и перезапустите бота.")
            return
        self.next_tg_fetch_at = monotonic() + FETCH_COOLDOWN_SECONDS
        with self.search_notice(admin_id):
            posts, failed = collect_channel_posts(self.config.tg_channels)
        if len(failed) == len(self.config.tg_channels):
            self.send(admin_id, "Не удалось прочитать Telegram-каналы: " + ", ".join(f"@{channel}" for channel in failed))
            return
        self.manual_digest("tg", posts, admin_id)

    def ensure_schedule(self, now: datetime) -> None:
        self.storage.ensure_digest_schedule(
            self.config.admin_username,
            now + timedelta(minutes=self.config.subscription_interval_minutes),
            now + timedelta(hours=self.config.digest_interval_hours),
            self.config.subscription_interval_minutes,
            self.config.digest_interval_hours,
        )

    def collect_subscription(self, started_at: datetime, now: datetime) -> None:
        report: list[dict[str, object]] = []
        total_added = 0

        def record_source(category: str, name: str, url: str, items: list[Article], ok: bool) -> None:
            nonlocal total_added
            read_at = datetime.now(timezone.utc)
            eligible = [item for item in items if started_at < item.published_at <= read_at]
            added = self.storage.enqueue_digest(self.config.admin_username, category, eligible)
            total_added += added
            latest = max((item.published_at for item in items), default=None)
            report.append({
                "source": name, "url": url, "ok": ok, "found": len(items), "new": added,
                "eligible": len(eligible), "latest": latest.isoformat() if latest else None,
            })

        for source in self.sources:
            if self.stopped.is_set():
                return
            articles, successes = self.collect((source,))
            record_source("web", source.name, source.url, articles, bool(successes))
        for channel in self.config.tg_channels:
            if self.stopped.is_set():
                return
            posts, failed = collect_channel_posts((channel,))
            record_source("tg", f"@{channel}", f"https://t.me/s/{channel}", posts, not failed)
        self.storage.record_check(
            self.config.admin_username, datetime.now(timezone.utc),
            now + timedelta(minutes=self.config.subscription_interval_minutes),
            json.dumps(report, ensure_ascii=False),
        )
        LOG.info("Проверено источников: %d/%d, новых записей: %d",
                 sum(bool(item["ok"]) for item in report), len(report), total_added)

    def check_subscription(self) -> datetime | None:
        owner = self.config.admin_username
        started_at = self.storage.subscription_started_at(owner)
        if self.admin_id is None or started_at is None:
            return None
        now = datetime.now(timezone.utc)
        self.ensure_schedule(now)
        schedule = self.storage.digest_schedule(owner)
        if schedule is None:
            return None
        next_check, next_digest, _, _ = schedule
        if now < next_check and now < next_digest:
            return min(next_check, next_digest)
        self.collect_subscription(started_at, now)
        if self.stopped.is_set():
            return None
        finished = datetime.now(timezone.utc)
        if finished >= next_digest:
            next_digest = finished + timedelta(hours=self.config.digest_interval_hours)
            self.send_both_digests(self.admin_id, next_digest)
        else:
            self.send_pending_digests(self.admin_id)
        return min(now + timedelta(minutes=self.config.subscription_interval_minutes), next_digest)

    def status(self, admin_id: int) -> None:
        owner = self.config.admin_username
        active = self.storage.subscription_started_at(owner) is not None
        if active:
            self.check_subscription()
        schedule = self.storage.digest_schedule(owner)

        def local(value: datetime | None) -> str:
            return value.astimezone(self.config.timezone).strftime("%d.%m %H:%M:%S") if value else "ещё не было"

        counts = self.storage.digest_counts(owner)
        lines = [
            f"Подписка: {'подписан' if active else 'не подписан'}",
            f"Проверка каждые {self.config.subscription_interval_minutes} мин.",
            f"Дайджест каждые {self.config.digest_interval_hours} ч.",
            f"Часовой пояс: {self.config.timezone.key}",
            f"Последняя проверка: {local(schedule[2] if schedule else None)}",
            f"Следующая проверка: {local(schedule[0]) if active and schedule else 'подписка отключена'}",
            f"Следующий дайджест: {local(schedule[1]) if active and schedule else 'подписка отключена'}",
            f"Накоплено: сайты — {counts.get('web', 0)}, Telegram — {counts.get('tg', 0)}",
            f"Отправлено сообщений дайджеста: {self.storage.sent_digest_count(owner)}",
        ]
        uncertain = self.storage.uncertain_digest_count(owner)
        if uncertain:
            lines.append(f"Отправок с неподтверждённым результатом: {uncertain}. Проверьте сообщения.")
        lines.append("\nИсточники")
        reports = {item["url"]: item for item in json.loads(schedule[3])} if schedule else {}
        configured = [(source.name, source.url) for source in self.sources]
        configured += [(f"@{channel}", f"https://t.me/s/{channel}") for channel in self.config.tg_channels]
        for name, url in configured:
            report = reports.get(url)
            state = "ещё не проверялся"
            if report:
                state = f"доступен, найдено {report['found']}, новых {report['new']}" if report["ok"] else "ошибка чтения"
                if report.get("latest"):
                    state += f"; последняя публикация {local(datetime.fromisoformat(report['latest']))}"
                if report.get("eligible", 0) > report["new"]:
                    state += f"; уже учтено {report['eligible'] - report['new']}"
            lines.append(f"• {name} — {state}")
        if not configured:
            lines.append("Список источников пуст.")
        text = ""
        for line in lines:
            if utf16_length(text + line + "\n") > 4096:
                self.send(admin_id, text.rstrip())
                text = ""
            text += line + "\n"
        self.send(admin_id, text.rstrip())

    def private_chat_id(self, message: dict[str, Any]) -> int | None:
        chat = as_object(message.get("chat"))
        chat_id = chat.get("id")
        if chat.get("type") == "private" and type(chat_id) is int and 0 < chat_id < 2**52:
            return chat_id
        return None

    def handle(self, update: dict[str, Any]) -> None:
        message = as_object(update.get("message"))
        chat_id = self.private_chat_id(message)
        sender = as_object(message.get("from"))
        if chat_id is None or sender.get("id") != chat_id:
            return
        text = message.get("text")
        if not isinstance(text, str) or not (parts := text.split()):
            return
        command = parts[0].casefold()
        if "@" in command:
            command, target = command.split("@", 1)
            if target != self.username:
                return
        sender_username = sender.get("username")
        if (self.admin_id is None and command == "/start" and isinstance(sender_username, str)
                and sender_username.lower() == self.config.admin_username):
            self.storage.bind_owner(self.config.admin_username, chat_id)
            self.admin_id = self.storage.owner_id(self.config.admin_username)
        if chat_id != self.admin_id:
            return
        if command == "/fetch":
            if monotonic() < self.next_fetch_at:
                self.send(chat_id, "Между запусками поиска должна пройти минута.")
            else:
                self.fetch(chat_id)
        elif command == "/fetchtg":
            if monotonic() < self.next_tg_fetch_at:
                self.send(chat_id, "Между запусками /fetchtg должна пройти минута.")
            else:
                self.fetch_tg(chat_id)
        elif command == "/digest" and len(parts) == 1:
            self.send_both_digests(chat_id)
        elif command in {"/subscribe", "/subscrube"}:
            if self.storage.subscription_started_at(self.config.admin_username) is None:
                now = datetime.now(timezone.utc)
                self.storage.subscribe(self.config.admin_username, now)
                self.storage.reset_digest_schedule(self.config.admin_username)
                self.ensure_schedule(now)
            self.send(chat_id, f"Подписка включена. Проверка каждые {self.config.subscription_interval_minutes} мин., "
                      f"два дайджеста каждые {self.config.digest_interval_hours} ч. Отмена — /unsubscribe.")
        elif command == "/unsubscribe":
            self.storage.unsubscribe(self.config.admin_username)
            self.send(chat_id, "Подписка отключена. Накопленные материалы сохранены.")
        elif command == "/status":
            self.status(chat_id)
        elif command == "/help":
            active = self.storage.subscription_started_at(self.config.admin_username) is not None
            self.send(chat_id, (
                "/start — привязать аккаунт владельца\n"
                "/fetch — дайджест с сайтов сейчас\n"
                "/fetchtg — дайджест из Telegram сейчас\n"
                "/digest — отправить оба накопленных дайджеста, сохранив расписание\n"
                "/subscribe — включить сбор и дайджесты\n"
                "/unsubscribe — отключить подписку\n"
                "/status — проверки, источники и накопленные материалы\n"
                "/help — основные команды\n\n"
                f"🔔 Статус подписки: {'подписан' if active else 'не подписан'}"
            ))
        elif command == "/start":
            self.send(chat_id, "Бот готов. /help — команды, /status — состояние, /subscribe — дайджесты.")

    def run(self) -> None:
        me = as_object(self.telegram.call("getMe"))
        if not isinstance(me.get("username"), str):
            raise TelegramError("getMe")
        self.username = me["username"].casefold()
        if self.telegram.call("deleteWebhook", drop_pending_updates=True) is not True:
            raise TelegramError("deleteWebhook")
        offset: int | None = None
        LOG.info("Бот запущен. Сайтов: %d, Telegram-каналов: %d", len(self.sources), len(self.config.tg_channels))
        while not self.stopped.is_set():
            next_action = self.check_subscription()
            if self.stopped.is_set():
                break
            poll_seconds = POLL_SECONDS
            if next_action is not None:
                remaining = (next_action - datetime.now(timezone.utc)).total_seconds()
                poll_seconds = min(POLL_SECONDS, max(1, ceil(remaining)))
            try:
                options = {} if offset is None else {"offset": offset}
                updates = self.telegram.call(
                    "getUpdates", http_timeout=poll_seconds + 10, timeout=poll_seconds,
                    allowed_updates=["message"], limit=20, **options,
                )
                if not isinstance(updates, list):
                    raise TelegramError("getUpdates")
            except TelegramError as error:
                LOG.warning("Long polling: %s", error)
                self.stopped.wait(5)
                continue
            for raw in updates:
                if self.stopped.is_set():
                    break
                update = as_object(raw)
                update_id = update.get("update_id")
                if type(update_id) is not int:
                    continue
                try:
                    self.handle(update)
                except TelegramError as error:
                    LOG.warning("Обновление %d: %s", update_id, error)
                offset = update_id + 1
