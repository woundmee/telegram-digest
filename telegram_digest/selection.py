"""Rules for selecting articles for the moderation queue."""

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True, slots=True)
class Article:
    title: str
    url: str
    source: str
    published_at: datetime
    description: str = ""


def article_key(url: str) -> str:
    """Identify the same article across feeds without changing its query."""
    if any(character.isspace() or ord(character) < 32 for character in url) or "\\" in url:
        raise ValueError("article URL contains invalid characters")
    url.encode()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
        raise ValueError("article URL must be an absolute HTTP(S) URL without credentials")
    if parts.port == 0:
        raise ValueError("article URL contains an invalid port")
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


def select_articles(
    articles: Iterable[Article],
    *,
    topics: Iterable[str],
    seen_keys: Collection[str],
    now: datetime,
    max_age: timedelta,
    limit: int,
) -> list[Article]:
    """Return relevant, recent articles in moderation order."""
    if now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    if limit < 1 or max_age <= timedelta(0):
        raise ValueError("limit and max_age must be positive")

    terms = {term.strip().casefold() for term in topics if term.strip()}
    patterns = [re.compile(rf"(?<!\w){re.escape(term)}(?!\w)") for term in terms]
    now = now.astimezone(timezone.utc)
    cutoff = now - max_age
    ranked: list[tuple[int, datetime, str, Article]] = []

    for article in articles:
        published_at = article.published_at
        if published_at.utcoffset() is None:
            continue
        published_at = published_at.astimezone(timezone.utc)
        if not cutoff <= published_at <= now:
            continue
        try:
            key = article_key(article.url)
        except ValueError:
            continue
        if key in seen_keys:
            continue

        searchable = f"{article.title}\n{article.description}".casefold()
        score = sum(pattern.search(searchable) is not None for pattern in patterns)
        if score or not patterns:
            ranked.append((score, published_at, key, article))

    ranked.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    selected: list[Article] = []
    used_keys: set[str] = set()
    for _, _, key, article in ranked:
        if key in used_keys:
            continue
        selected.append(article)
        used_keys.add(key)
        if len(selected) == limit:
            break

    return selected
