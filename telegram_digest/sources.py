"""Readers for RSS, Atom and public JSON Feed endpoints."""

from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.client import HTTPException
import json
import logging
from time import sleep
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from .config import Config
from .selection import Article, article_key


LOG = logging.getLogger(__name__)
MAX_FEED_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Source:
    name: str
    url: str


class FeedTree(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ValueError("feed contains a DTD")


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "li", "pre"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if tag in {"p", "div", "li", "pre"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_html(text: str) -> str:
    parser = TextExtractor()
    parser.feed(text)
    parser.close()
    return " ".join("".join(parser.parts).split())


def text_of(element: ET.Element, *names: str) -> str:
    for name in names:
        child = element.find(f"{{*}}{name}")
        if child is not None and (value := "".join(child.itertext()).strip()):
            return value
    return ""


def is_russian(language: str) -> bool:
    return not language or language.lower().replace("_", "-").split("-")[0] == "ru"


def parse_article(source: Source, title: str, link: str, published: str, description: str) -> Article | None:
    if not title or not link or not published:
        return None
    try:
        url = urljoin(source.url, link)
        article_key(url)
        title.encode()
        description.encode()
        try:
            published_at = datetime.fromisoformat(published)
        except ValueError:
            published_at = parsedate_to_datetime(published)
    except (ValueError, OverflowError):
        return None
    if published_at.utcoffset() is None:
        return None
    return Article(title, url, source.name, published_at, description[:4000])


def xml_articles(data: bytes, source: Source) -> list[Article]:
    root = ET.fromstring(data, parser=ET.XMLParser(target=FeedTree()))
    kind = root.tag.rsplit("}", 1)[-1].lower()
    if kind not in {"rss", "rdf", "feed"}:
        raise ValueError("unsupported XML feed")
    channel = root.find("{*}channel")
    container = channel if channel is not None and kind == "rss" else root
    language = text_of(channel, "language") if channel is not None else root.get("{http://www.w3.org/XML/1998/namespace}lang", "")
    if not is_russian(language):
        return []
    articles = []
    for item in container.findall("{*}entry" if kind == "feed" else "{*}item"):
        if not is_russian(item.get("{http://www.w3.org/XML/1998/namespace}lang", "")):
            continue
        link = text_of(item, "link")
        if not link and kind == "feed":
            link = next(
                (node.get("href", "") for node in item.findall("{*}link") if node.get("rel", "alternate") == "alternate"),
                "",
            )
        article = parse_article(
            source,
            plain_html(text_of(item, "title")),
            link,
            text_of(item, "pubDate", "published", "date"),
            plain_html(text_of(item, "description", "summary", "encoded", "content")),
        )
        if article is not None:
            articles.append(article)
    return articles


def json_articles(data: bytes, source: Source) -> list[Article]:
    feed = json.loads(data)
    if not isinstance(feed, dict) or feed.get("version") not in (
        "https://jsonfeed.org/version/1",
        "https://jsonfeed.org/version/1.1",
    ) or not isinstance(feed.get("items"), list):
        raise ValueError("unsupported JSON feed")
    language = feed.get("language", "")
    if not isinstance(language, str) or not is_russian(language):
        return []
    articles = []
    for item in feed["items"]:
        if not isinstance(item, dict):
            continue
        language = item.get("language", "")
        title = item.get("title", "")
        link = item.get("url") or item.get("external_url", "")
        published = item.get("date_published", "")
        description = item.get("summary") or item.get("content_text", "")
        if not description and isinstance(item.get("content_html"), str):
            description = plain_html(item["content_html"])
        fields = (language, title, link, published, description)
        if not all(isinstance(value, str) for value in fields) or not is_russian(language):
            continue
        article = parse_article(source, title.strip(), link, published, description)
        if article is not None:
            articles.append(article)
    return articles


def configured_sources(config: Config) -> tuple[Source, ...]:
    sources = tuple(Source(urlsplit(url).hostname or url, url) for url in config.extra_sources)
    by_url: dict[str, Source] = {}
    for source in sources:
        key = article_key(source.url)
        by_url.setdefault(key, source)
    return tuple(by_url.values())


def read_source(source: Source) -> list[Article]:
    request = Request(source.url, headers={"User-Agent": "telegram-digest/0.1"})
    for attempt in range(2):
        try:
            with urlopen(request, timeout=8) as response:
                data = response.read(MAX_FEED_BYTES + 1)
                endpoint = Source(source.name, response.geturl())
            break
        except HTTPError as error:
            if attempt or error.code not in {500, 502, 503, 504}:
                raise
        except (OSError, HTTPException):
            if attempt:
                raise
        sleep(1)
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("feed exceeds size limit")
    return json_articles(data, endpoint) if data.lstrip().startswith(b"{") else xml_articles(data, endpoint)


def collect_articles(sources: tuple[Source, ...]) -> tuple[list[Article], int]:
    articles: list[Article] = []
    successes = 0
    for source in sources:
        try:
            articles.extend(read_source(source))
        except (OSError, HTTPException, ValueError, ET.ParseError, RecursionError) as error:
            LOG.warning("Источник %s: %s", source.name, type(error).__name__)
        else:
            successes += 1
    return articles, successes
