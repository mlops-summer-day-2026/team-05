"""Инфраструктура хранения: SQLite через aiosqlite (подписки, настройки, кеш постов)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from models import Channel, Post

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id      INTEGER PRIMARY KEY,
    hours_n    INTEGER NOT NULL DEFAULT 24,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id        INTEGER NOT NULL,
    username     TEXT NOT NULL,
    mode         TEXT NOT NULL DEFAULT 'public',
    added_at     TEXT NOT NULL,
    last_fetched TEXT,
    UNIQUE(tg_id, username)
);
CREATE TABLE IF NOT EXISTS posts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    post_id    TEXT NOT NULL,
    text       TEXT NOT NULL,
    published  TEXT NOT NULL,
    views      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(channel_id, post_id)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    """Асинхронная обёртка над SQLite-файлом бота."""

    def __init__(self, path: Path | str) -> None:
        """Создаёт обёртку.

        :param path: путь к файлу БД.
        """
        self.path = Path(path)

    async def init(self) -> None:
        """Создаёт схему, если БД ещё нет."""
        async with self._connect() as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    def _connect(self) -> aiosqlite.Connection:
        """Открывает соединение с БД."""
        return aiosqlite.connect(self.path)

    # --- users ---

    async def ensure_user(self, tg_id: int) -> None:
        """Создаёт запись пользователя, если её нет.

        :param tg_id: telegram id пользователя.
        """
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (tg_id, created_at) VALUES (?, ?)",
                (tg_id, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()

    async def get_hours(self, tg_id: int, default_hours: int) -> int:
        """Возвращает сохранённый период пользователя.

        :param tg_id: telegram id пользователя.
        :param default_hours: значение по умолчанию из конфига.
        :return: число часов.
        """
        async with self._connect() as db:
            cursor = await db.execute("SELECT hours_n FROM users WHERE tg_id = ?", (tg_id,))
            row = await cursor.fetchone()
        return row[0] if row else default_hours

    async def set_hours(self, tg_id: int, hours: int) -> None:
        """Сохраняет выбранный период.

        :param tg_id: telegram id пользователя.
        :param hours: число часов.
        """
        await self.ensure_user(tg_id)
        async with self._connect() as db:
            await db.execute("UPDATE users SET hours_n = ? WHERE tg_id = ?", (hours, tg_id))
            await db.commit()

    # --- channels ---

    async def add_channel(self, tg_id: int, username: str, mode: str = "public") -> Channel:
        """Добавляет подписку на канал.

        :param tg_id: telegram id пользователя.
        :param username: юзернейм канала без @.
        :param mode: способ получения постов.
        :return: сохранённый канал.
        """
        await self.ensure_user(tg_id)
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO channels (tg_id, username, mode, added_at) "
                "VALUES (?, ?, ?, ?)",
                (tg_id, username, mode, datetime.now(timezone.utc).isoformat()),
            )
            cursor = await db.execute(
                "SELECT id FROM channels WHERE tg_id = ? AND username = ?",
                (tg_id, username),
            )
            row = await cursor.fetchone()
            await db.commit()
        return Channel(id=row[0], username=username, mode=mode)

    async def get_channel(self, tg_id: int, username: str) -> Channel | None:
        """Ищет подписку пользователя на канал.

        :param tg_id: telegram id пользователя.
        :param username: юзернейм канала без @.
        :return: канал или None.
        """
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id, username, mode FROM channels WHERE tg_id = ? AND username = ?",
                (tg_id, username),
            )
            row = await cursor.fetchone()
        return Channel(id=row[0], username=row[1], mode=row[2]) if row else None

    async def list_channels(self, tg_id: int) -> list[Channel]:
        """Возвращает все подписки пользователя.

        :param tg_id: telegram id пользователя.
        :return: список каналов.
        """
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id, username, mode FROM channels WHERE tg_id = ? ORDER BY id",
                (tg_id,),
            )
            rows = await cursor.fetchall()
        return [Channel(id=r[0], username=r[1], mode=r[2]) for r in rows]

    async def delete_channel(self, channel_id: int, tg_id: int) -> str | None:
        """Удаляет подписку и её посты из кеша.

        :param channel_id: id записи канала.
        :param tg_id: telegram id владельца.
        :return: юзернейм удалённого канала или None.
        """
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT username FROM channels WHERE id = ? AND tg_id = ?", (channel_id, tg_id)
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            username = row[0]
            await db.execute("DELETE FROM posts WHERE channel_id = ?", (channel_id,))
            await db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
            await db.commit()
        return username

    async def get_channels_by_username(self, username: str) -> list[Channel]:
        """Ищет подписки всех пользователей на канал (для polling-коллектора).

        :param username: юзернейм канала без @.
        :return: список каналов с этим юзернеймом.
        """
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id, username, mode FROM channels WHERE username = ?", (username,)
            )
            rows = await cursor.fetchall()
        return [Channel(id=r[0], username=r[1], mode=r[2]) for r in rows]

    async def set_last_fetched(self, channel_id: int, timestamp: str) -> None:
        """Фиксирует время последнего парсинга канала.

        :param channel_id: id записи канала.
        :param timestamp: ISO-время.
        """
        async with self._connect() as db:
            await db.execute(
                "UPDATE channels SET last_fetched = ? WHERE id = ?", (timestamp, channel_id)
            )
            await db.commit()

    async def get_last_fetched(self, channel_id: int) -> str | None:
        """Возвращает время последнего парсинга канала.

        :param channel_id: id записи канала.
        :return: ISO-время или None.
        """
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT last_fetched FROM channels WHERE id = ?", (channel_id,)
            )
            row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    # --- posts cache ---

    async def save_posts(self, channel_id: int, posts: list[Post]) -> int:
        """Сохраняет посты в кеш, дубликаты игнорируются.

        :param channel_id: id записи канала.
        :param posts: список постов.
        :return: количество новых записей.
        """
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM posts WHERE channel_id = ?", (channel_id,)
            )
            before = (await cursor.fetchone())[0]
            await db.executemany(
                "INSERT OR IGNORE INTO posts (channel_id, post_id, text, published, views) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (channel_id, p.post_id, p.text, p.published, p.views)
                    for p in posts
                ],
            )
            cursor = await db.execute(
                "SELECT COUNT(*) FROM posts WHERE channel_id = ?", (channel_id,)
            )
            after = (await cursor.fetchone())[0]
            await db.commit()
            return after - before

    async def get_posts_since(self, channel_id: int, since: str) -> list[Post]:
        """Возвращает посты канала из кеша не старше since.

        :param channel_id: id записи канала.
        :param since: ISO-время нижней границы.
        :return: список постов.
        """
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT post_id, text, published, views FROM posts "
                "WHERE channel_id = ? AND published >= ? ORDER BY published DESC",
                (channel_id, since),
            )
            rows = await cursor.fetchall()
        return [
            Post(
                channel_username="",
                post_id=r[0],
                text=r[1],
                published=r[2],
                views=r[3],
            )
            for r in rows
        ]

    # --- meta ---

    async def get_meta(self, key: str) -> str | None:
        """Читает произвольное значение из meta-таблицы.

        :param key: ключ.
        :return: строковое значение или None.
        """
        async with self._connect() as db:
            cursor = await db.execute("SELECT value FROM meta WHERE key = ?", (key,))
            row = await cursor.fetchone()
        return row[0] if row else None

    async def set_meta(self, key: str, value: Any) -> None:
        """Сохраняет произвольное значение в meta-таблицу.

        :param key: ключ.
        :param value: значение (приводится к строке).
        """
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            await db.commit()
