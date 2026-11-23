"""Слой работы с SQLite: WAL, схема, запись и выборка через aiosqlite."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS msgs (
    chat_id          INTEGER NOT NULL,
    chat_title       TEXT,
    msg_id           INTEGER NOT NULL,
    sender_id        INTEGER,
    sender_name      TEXT,
    sender_username  TEXT,
    date             TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT 'other',
    text             TEXT,
    caption          TEXT,
    reply_to_msg_id  INTEGER,
    edited_at        TEXT,
    deleted_at       TEXT,
    file_id          TEXT,
    file_name        TEXT,
    file_size        INTEGER,
    sticker_emoji    TEXT,
    media_path       TEXT,
    PRIMARY KEY (chat_id, msg_id)
);

CREATE TABLE IF NOT EXISTS edits (
    chat_id    INTEGER NOT NULL,
    msg_id     INTEGER NOT NULL,
    old_text   TEXT,
    new_text   TEXT,
    edited_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
    user_id       INTEGER NOT NULL,
    user_name     TEXT,
    user_chat_id  INTEGER,
    user_username TEXT,
    is_enabled    INTEGER NOT NULL,
    connected_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_msgs_chat_msg ON msgs (chat_id, msg_id);
CREATE INDEX IF NOT EXISTS idx_msgs_date ON msgs (date);
"""

# Поля, которые принимает save_message (остальные подставляются как NULL).
FIELDS = (
    "chat_id",
    "chat_title",
    "msg_id",
    "sender_id",
    "sender_name",
    "sender_username",
    "date",
    "kind",
    "text",
    "caption",
    "reply_to_msg_id",
    "file_id",
    "file_name",
    "file_size",
    "sticker_emoji",
    "media_path",
)


class Database:
    """Одно соединение на процесс; aiosqlite выполняет запросы вне event loop."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()
        logger.info("База открыта: %s", self._path)

    async def _migrate(self) -> None:
        """Добавляет новые колонки в базу, созданную старой версией."""
        async with self.conn.execute("PRAGMA table_info(connections)") as cursor:
            existing = {row["name"] for row in await cursor.fetchall()}
        for column, sql_type in (("user_chat_id", "INTEGER"), ("user_username", "TEXT")):
            if column not in existing:
                await self.conn.execute(f"ALTER TABLE connections ADD COLUMN {column} {sql_type}")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("База закрыта")

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() не вызван")
        return self._conn

    async def save_message(self, data: dict[str, Any]) -> bool:
        """Сохраняет новое сообщение; если такое уже есть — ничего не меняет."""
        row = {key: None for key in FIELDS}
        row.update(data)
        cursor = await self.conn.execute(
            """
            INSERT INTO msgs (chat_id, chat_title, msg_id, sender_id, sender_name, sender_username,
                              date, kind, text, caption, reply_to_msg_id, file_id, file_name,
                              file_size, sticker_emoji, media_path)
            VALUES (:chat_id, :chat_title, :msg_id, :sender_id, :sender_name, :sender_username,
                    :date, :kind, :text, :caption, :reply_to_msg_id, :file_id, :file_name,
                    :file_size, :sticker_emoji, :media_path)
            ON CONFLICT (chat_id, msg_id) DO NOTHING
            """,
            row,
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def get_message(self, chat_id: int, msg_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM msgs WHERE chat_id = ? AND msg_id = ?", (chat_id, msg_id)
        ) as cursor:
            return await cursor.fetchone()

    async def update_message(
        self,
        chat_id: int,
        msg_id: int,
        *,
        text: str | None,
        caption: str | None,
        kind: str,
        edited_at: str,
    ) -> None:
        await self.conn.execute(
            """
            UPDATE msgs
               SET text = ?, caption = ?, kind = ?, edited_at = ?
             WHERE chat_id = ? AND msg_id = ?
            """,
            (text, caption, kind, edited_at, chat_id, msg_id),
        )
        await self.conn.commit()

    async def add_edit(
        self, chat_id: int, msg_id: int, old_text: str | None, new_text: str | None, edited_at: str
    ) -> None:
        await self.conn.execute(
            "INSERT INTO edits (chat_id, msg_id, old_text, new_text, edited_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, msg_id, old_text, new_text, edited_at),
        )
        await self.conn.commit()

    async def mark_deleted(self, chat_id: int, msg_id: int, deleted_at: str) -> aiosqlite.Row | None:
        """Помечает сообщение удалённым.

        Возвращает сохранённую копию для уведомления или None, если записи нет
        либо она уже была помечена (повторное событие не шлём).
        """
        existing = await self.get_message(chat_id, msg_id)
        if existing is None or existing["deleted_at"]:
            return None
        await self.conn.execute(
            "UPDATE msgs SET deleted_at = ? WHERE chat_id = ? AND msg_id = ? AND deleted_at IS NULL",
            (deleted_at, chat_id, msg_id),
        )
        await self.conn.commit()
        return existing

    async def record_connection(
        self,
        user_id: int,
        user_name: str | None,
        is_enabled: bool,
        connected_at: str,
        *,
        user_chat_id: int | None = None,
        user_username: str | None = None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO connections (user_id, user_name, user_chat_id, user_username, is_enabled, connected_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, user_name, user_chat_id, user_username, 1 if is_enabled else 0, connected_at),
        )
        await self.conn.commit()

    async def latest_connection(self) -> aiosqlite.Row | None:
        """Последнее включённое подключение — оттуда берём владельца и чат уведомлений."""
        async with self.conn.execute(
            """
            SELECT * FROM connections
             WHERE is_enabled = 1
             ORDER BY connected_at DESC, rowid DESC
             LIMIT 1
            """
        ) as cursor:
            return await cursor.fetchone()

    async def cleanup(self, days: int) -> int:
        """Удаляет записи старше N дней вместе с историей правок."""
        if days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        await self.conn.execute(
            """
            DELETE FROM edits
             WHERE EXISTS (
                   SELECT 1 FROM msgs m
                    WHERE m.chat_id = edits.chat_id
                      AND m.msg_id = edits.msg_id
                      AND m.date < ?
             )
            """,
            (cutoff,),
        )
        cursor = await self.conn.execute("DELETE FROM msgs WHERE date < ?", (cutoff,))
        await self.conn.commit()
        return cursor.rowcount
