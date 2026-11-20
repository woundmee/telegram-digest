"""Read recent posts from public Telegram channel web previews."""

from datetime import datetime, timezone
from html.parser import HTMLParser
from http.client import HTTPException
import logging
import re
from time import sleep
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .selection import Article


LOG = logging.getLogger(__name__)
MAX_PAGE_BYTES = 4 * 1024 * 1024
VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})


class ChannelParser(HTMLParser):
    def __init__(self, channel: str) -> None:
        super().__init__()
        self.channel = channel
        self.stack: list[str] = []
        self.message_depth = 0
        self.text_depth = 0
        self.post_id = ""
        self.published = ""
        self.parts: list[str] = []
        self.posts: dict[int, Article] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag not in VOID_TAGS:
            self.stack.append(tag)
        if "tgme_widget_message" in classes and not self.message_depth:
            post = attributes.get("data-post") or ""
            match = re.fullmatch(r"([A-Za-z0-9_]+)/([1-9][0-9]*)", post)
            if match and match[1].lower() == self.channel:
                self.message_depth = len(self.stack)
                self.post_id = match[2]
                self.published = ""
                self.parts = []
        if not self.message_depth:
            return
        if "tgme_widget_message_text" in classes:
            self.text_depth = len(self.stack)
        if tag == "time" and attributes.get("datetime"):
            self.published = attributes["datetime"] or ""
        if self.text_depth:
            if tag in {"br", "p", "div", "blockquote", "pre"}:
                self.parts.append("\n")
            if tag == "img":
                self.parts.append(attributes.get("alt") or "")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in self.stack:
            return
        depth = len(self.stack) - self.stack[::-1].index(tag)
        if self.text_depth and depth <= self.text_depth:
            self.text_depth = 0
        if self.message_depth and depth <= self.message_depth:
            self.finish_post()
            self.message_depth = 0
        del self.stack[depth - 1:]

    def handle_data(self, data: str) -> None:
        if self.text_depth:
            self.parts.append(data)

    def finish_post(self) -> None:
        try:
            published_at = datetime.fromisoformat(self.published)
        except ValueError:
            return
        if published_at.utcoffset() is None:
            return
        text = "\n".join(line.strip() for line in "".join(self.parts).splitlines()).strip()
        first_line, _, remaining = text.partition("\n")
        title = first_line if first_line and len(first_line) <= 250 else "Новая публикация"
        description = remaining.strip() if title == first_line else text
        self.posts[int(self.post_id)] = Article(
            title=title,
            url=f"https://t.me/{self.channel}/{self.post_id}",
            source=f"Telegram · @{self.channel}",
            published_at=published_at.astimezone(timezone.utc),
            description=description if text else "Медиапубликация. Откройте оригинал по ссылке.",
        )


def parse_channel(data: str, channel: str) -> list[Article]:
    parser = ChannelParser(channel)
    parser.feed(data)
    parser.close()
    # Message IDs keep pinned and edited older posts out of the newest positions.
    return [parser.posts[post_id] for post_id in sorted(parser.posts, reverse=True)]


def read_channel(channel: str) -> list[Article]:
    if not re.fullmatch(r"[a-z0-9_]{1,32}", channel) or channel.isdecimal():
        raise ValueError("invalid channel username")
    request = Request(f"https://t.me/s/{channel}", headers={"User-Agent": "telegram-digest/0.1"})
    for attempt in range(2):
        try:
            with urlopen(request, timeout=10) as response:
                data = response.read(MAX_PAGE_BYTES + 1)
            break
        except HTTPError as error:
            if attempt or error.code not in {500, 502, 503, 504}:
                raise
        except (OSError, HTTPException):
            if attempt:
                raise
        sleep(1)
    if len(data) > MAX_PAGE_BYTES:
        raise ValueError("channel page exceeds size limit")
    posts = parse_channel(data.decode("utf-8"), channel)
    if not posts:
        raise ValueError("public channel preview has no readable posts")
    return posts


def collect_channel_posts(channels: tuple[str, ...]) -> tuple[list[Article], tuple[str, ...]]:
    posts: list[Article] = []
    failed: list[str] = []
    for channel in channels:
        try:
            posts.extend(read_channel(channel))
        except (OSError, HTTPException, ValueError, RecursionError) as error:
            LOG.warning("Telegram-канал @%s: %s", channel, type(error).__name__)
            failed.append(channel)
    return posts, tuple(failed)
