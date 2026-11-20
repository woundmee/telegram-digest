"""Persistent owner settings, source diagnostics and digest delivery."""

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
import sqlite3
from .selection import Article, article_key


class Storage:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS owners (
                username TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                username TEXT PRIMARY KEY,
                started_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS digest_schedule (
                username TEXT PRIMARY KEY,
                next_check TEXT NOT NULL,
                next_digest TEXT NOT NULL,
                last_check TEXT,
                report TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS digest_outbox (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                category TEXT NOT NULL,
                text TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending', 'sending', 'sent'))
            );
            CREATE TABLE IF NOT EXISTS digest_queue (
                username TEXT NOT NULL,
                url_key TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                published_at TEXT NOT NULL,
                batch_id INTEGER REFERENCES digest_outbox(id),
                PRIMARY KEY(username, url_key)
            );
            CREATE INDEX IF NOT EXISTS idx_queue_batch ON digest_queue(batch_id);
            CREATE INDEX IF NOT EXISTS idx_queue_owner_category
                ON digest_queue(username, category, batch_id, published_at, url_key);
            CREATE INDEX IF NOT EXISTS idx_outbox_owner_state ON digest_outbox(username, state, id);
        """)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(digest_schedule)")}
            for column in ("check_minutes", "digest_hours"):
                if column not in columns:
                    self.db.execute(f"ALTER TABLE digest_schedule ADD COLUMN {column} INTEGER")
            if self.db.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'seen_urls'"
            ).fetchone() is None:
                self.db.execute("CREATE TABLE seen_urls (url_key TEXT PRIMARY KEY NOT NULL) WITHOUT ROWID")
                if self.db.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'articles'"
                ).fetchone():
                    self.db.execute("INSERT INTO seen_urls (url_key) SELECT url_key FROM articles")

    def close(self) -> None:
        self.db.close()

    def owner_id(self, username: str) -> int | None:
        row = self.db.execute("SELECT user_id FROM owners WHERE username = ?", (username,)).fetchone()
        return row[0] if row else None

    def bind_owner(self, username: str, user_id: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO owners VALUES (?, ?) ON CONFLICT(username) DO NOTHING",
                (username, user_id),
            )

    def subscription_started_at(self, username: str) -> datetime | None:
        row = self.db.execute("SELECT started_at FROM subscriptions WHERE username = ?", (username,)).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def subscribe(self, username: str, started_at: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO subscriptions VALUES (?, ?) ON CONFLICT DO NOTHING",
                (username, started_at.isoformat()),
            )

    def unsubscribe(self, username: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM subscriptions WHERE username = ?", (username,))

    def ensure_digest_schedule(
        self, username: str, check_at: datetime, digest_at: datetime, check_minutes: int, digest_hours: int,
    ) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO digest_schedule
                       (username, next_check, next_digest, report, check_minutes, digest_hours)
                   VALUES (?, ?, ?, '[]', ?, ?)
                   ON CONFLICT(username) DO UPDATE SET
                       next_check = CASE WHEN check_minutes IS NOT excluded.check_minutes
                                         THEN excluded.next_check ELSE next_check END,
                       next_digest = CASE WHEN digest_hours IS NOT excluded.digest_hours
                                          THEN excluded.next_digest ELSE next_digest END,
                       check_minutes = excluded.check_minutes,
                       digest_hours = excluded.digest_hours
                   WHERE check_minutes IS NOT excluded.check_minutes OR digest_hours IS NOT excluded.digest_hours""",
                (username, check_at.isoformat(), digest_at.isoformat(), check_minutes, digest_hours),
            )

    def digest_schedule(self, username: str) -> tuple[datetime, datetime, datetime | None, str] | None:
        row = self.db.execute(
            "SELECT next_check, next_digest, last_check, report FROM digest_schedule WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None:
            return None
        return (datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1]),
                datetime.fromisoformat(row[2]) if row[2] else None, row[3])

    def record_check(self, username: str, now: datetime, next_check: datetime, report: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE digest_schedule SET last_check = ?, next_check = ?, report = ? WHERE username = ?",
                (now.isoformat(), next_check.isoformat(), report, username),
            )

    def reset_digest_schedule(self, username: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM digest_schedule WHERE username = ?", (username,))

    def enqueue_digest(self, username: str, category: str, articles: Iterable[Article]) -> int:
        added = 0
        with self.db:
            for article in articles:
                key = article_key(article.url)
                title = article.title
                if category == "tg" and (title == "Новая публикация" or title.startswith("Публикация @")):
                    title = article.description[:200] or title
                cursor = self.db.execute(
                    """INSERT INTO digest_queue (username, url_key, category, title, published_at)
                       SELECT ?, ?, ?, ?, ?
                       WHERE NOT EXISTS (SELECT 1 FROM seen_urls WHERE url_key = ?)
                       ON CONFLICT(username, url_key) DO NOTHING""",
                    (username, key, category, title, article.published_at.isoformat(), key),
                )
                added += cursor.rowcount
        return added

    def queued_digest(self, username: str, category: str) -> list[tuple[str, str]]:
        return list(self.db.execute(
            """SELECT url_key, title FROM digest_queue
               WHERE username = ? AND category = ? AND batch_id IS NULL ORDER BY published_at, url_key""",
            (username, category),
        ))

    def prepare_digest(self, username: str, category: str, parts: list[tuple[str, list[str]]]) -> None:
        self.prepare_digest_window(username, [(category, parts)])

    def prepare_digest_window(
        self, username: str, window: list[tuple[str, list[tuple[str, list[str]]]]],
        next_digest: datetime | None = None,
    ) -> None:
        with self.db:
            for category, parts in window:
                for text, keys in parts:
                    cursor = self.db.execute(
                        "INSERT INTO digest_outbox (username, category, text, state) VALUES (?, ?, ?, 'pending')",
                        (username, category, text),
                    )
                    for key in keys:
                        self.db.execute(
                            "UPDATE digest_queue SET batch_id = ? WHERE username = ? AND url_key = ? AND batch_id IS NULL",
                            (cursor.lastrowid, username, key),
                        )
            if next_digest is not None:
                self.db.execute(
                    "UPDATE digest_schedule SET next_digest = ? WHERE username = ?", (next_digest.isoformat(), username)
                )

    def pending_digests(self, username: str) -> list[tuple[int, str]]:
        return list(self.db.execute(
            "SELECT id, text FROM digest_outbox WHERE username = ? AND state = 'pending' ORDER BY id",
            (username,),
        ))

    def set_digest_state(self, batch_id: int, previous: str, current: str) -> bool:
        with self.db:
            cursor = self.db.execute(
                "UPDATE digest_outbox SET state = ? WHERE id = ? AND state = ?", (current, batch_id, previous)
            )
            if cursor.rowcount and current == "sent":
                self.db.execute(
                    """INSERT INTO seen_urls (url_key)
                       SELECT url_key FROM digest_queue WHERE batch_id = ?
                       ON CONFLICT(url_key) DO NOTHING""", (batch_id,)
                )
                self.db.execute("DELETE FROM digest_queue WHERE batch_id = ?", (batch_id,))
        return bool(cursor.rowcount)

    def digest_counts(self, username: str) -> dict[str, int]:
        return dict(self.db.execute(
            "SELECT category, count(*) FROM digest_queue WHERE username = ? GROUP BY category", (username,)
        ))

    def uncertain_digest_count(self, username: str) -> int:
        return self.db.execute(
            "SELECT count(*) FROM digest_outbox WHERE username = ? AND state = 'sending'", (username,)
        ).fetchone()[0]

    def sent_digest_count(self, username: str) -> int:
        return self.db.execute(
            "SELECT count(*) FROM digest_outbox WHERE username = ? AND state = 'sent'", (username,)
        ).fetchone()[0]
