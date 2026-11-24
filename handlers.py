"""Обработчики business_connection, business_message, правок и удалений."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot, Router
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

from config import Config
from db import Database
from notifications import NotifyTarget, notify_captured_media, notify_connected, notify_deleted, notify_edited

logger = logging.getLogger(__name__)

router = Router(name="business")

_cfg: Config | None = None
_db: Database | None = None
# Куда слать уведомления; заполняется из .env и последнего business_connection.
_target = NotifyTarget()


async def configure(cfg: Config, db: Database) -> None:
    """Передаёт настройки и базу; подхватывает владельца из прошлых подключений."""
    global _cfg, _db, _target
    _cfg = cfg
    _db = db
    _target = NotifyTarget(chat_id=cfg.owner_id, user_id=cfg.owner_id, username=cfg.owner_username)

    row = await db.latest_connection()
    if row is None:
        logger.info("Владелец ещё не известен — ждём business_connection")
        return
    _target.chat_id = row["user_chat_id"] or row["user_id"] or _target.chat_id
    _target.user_id = row["user_id"] or _target.user_id
    _target.name = row["user_name"] or _target.name
    _target.username = row["user_username"] or _target.username
    logger.info("Владелец из базы: %s (%s)", _target.username or "без username", _target.user_id)


def _iso(value: datetime | int | None = None) -> str:
    """ISO-время в UTC; int — Unix-timestamp (так Telegram отдаёт edit_date)."""
    if value is None:
        value = datetime.now(timezone.utc)
    elif isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _chat_title(message: Message) -> str:
    chat = message.chat
    return chat.title or chat.full_name or (f"@{chat.username}" if chat.username else str(chat.id))


def _suffix(file_name: str | None, default: str) -> str:
    return Path(file_name).suffix if file_name else default


def _meta(
    kind: str,
    file: Any = None,
    *,
    name: str | None = None,
    emoji: str | None = None,
    ext: str = "",
) -> dict[str, Any]:
    """Метаданные медиа: без file_id/размера бот не сможет скачать файл позже."""
    return {
        "kind": kind,
        "file_id": getattr(file, "file_id", None),
        "file_size": getattr(file, "file_size", None),
        "file_name": name,
        "sticker_emoji": emoji,
        "ext": ext,
    }


def _media_info(message: Message) -> dict[str, Any]:
    """Тип сообщения и метаданные файла (без скачивания)."""
    if message.sticker is not None:
        return _meta("sticker", message.sticker, emoji=message.sticker.emoji, ext=".webp")
    if message.photo:
        return _meta("photo", message.photo[-1], ext=".jpg")
    if message.video is not None:
        video = message.video
        return _meta("video", video, name=video.file_name, ext=_suffix(video.file_name, ".mp4"))
    if message.video_note is not None:
        return _meta("video_note", message.video_note, ext=".mp4")
    if message.voice is not None:
        return _meta("voice", message.voice, ext=".ogg")
    if message.audio is not None:
        audio = message.audio
        return _meta("audio", audio, name=audio.file_name, ext=_suffix(audio.file_name, ".mp3"))
    if message.document is not None:
        document = message.document
        return _meta("document", document, name=document.file_name, ext=_suffix(document.file_name, ""))
    if message.location is not None:
        return {"kind": "location"}
    if message.contact is not None:
        return {"kind": "contact"}
    return {"kind": "text" if message.text else "other"}


async def _download_media(bot: Bot, message: Message, info: dict[str, Any]) -> str | None:
    """Скачивает файл в MEDIA_DIR (только при DOWNLOAD_MEDIA=1)."""
    if _cfg is None or not info.get("file_id"):
        return None
    try:
        file = await bot.get_file(info["file_id"])
        ext = info.get("ext") or (Path(file.file_path).suffix if file.file_path else "")
        destination = _cfg.media_dir / f"{message.chat.id}_{message.message_id}{ext}"
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        await bot.download_file(file.file_path, destination=destination)
        return str(destination)
    except Exception:
        logger.exception("Не удалось скачать медиа %s/%s", message.chat.id, message.message_id)
        return None


async def _save_message(message: Message, bot: Bot, *, msg_id: int | None = None) -> bool:
    """Сохраняет копию сообщения в архив."""
    assert _cfg is not None and _db is not None
    info = _media_info(message)
    media_path = None
    if _cfg.download_media and info.get("file_id"):
        media_path = await _download_media(bot, message, info)

    sender = message.from_user
    inserted = await _db.save_message(
        {
            "chat_id": message.chat.id,
            "chat_title": _chat_title(message),
            "msg_id": msg_id if msg_id is not None else message.message_id,
            "sender_id": sender.id if sender else None,
            "sender_name": sender.full_name if sender else None,
            "sender_username": sender.username if sender else None,
            "date": _iso(message.date),
            "kind": info["kind"],
            "text": message.text,
            "caption": message.caption,
            "reply_to_msg_id": message.reply_to_message.message_id if message.reply_to_message else None,
            "file_id": info.get("file_id"),
            "file_name": info.get("file_name"),
            "file_size": info.get("file_size"),
            "sticker_emoji": info.get("sticker_emoji"),
            "media_path": media_path,
        }
    )
    if inserted:
        logger.info(
            "Сохранено %s/%s (%s) из чата %s",
            message.chat.id,
            message.message_id,
            info["kind"],
            _chat_title(message),
        )
    return inserted


@router.business_connection()
async def on_business_connection(connection: BusinessConnection, bot: Bot) -> None:
    """Бот подключили или отключили от аккаунта."""
    if _cfg is None or _db is None:
        return
    user = connection.user
    try:
        await _db.record_connection(
            user.id,
            user.full_name,
            connection.is_enabled,
            _iso(datetime.now(timezone.utc)),
            user_chat_id=connection.user_chat_id,
            user_username=user.username,
        )
        if connection.is_enabled:
            # Запоминаем владельца и чат с ботом для уведомлений.
            _target.chat_id = connection.user_chat_id or user.id
            _target.user_id = user.id
            _target.name = user.full_name
            _target.username = user.username or _target.username
            logger.info("Business-бот подключён к %s (%s)", user.full_name, user.id)
            await notify_connected(bot, _cfg, _target)
        else:
            logger.info("Business-бот отключён от %s (%s)", user.full_name, user.id)
    except Exception:
        logger.exception("Ошибка обработки business_connection")


@router.business_message()
async def on_business_message(message: Message, bot: Bot) -> None:
    """Новое сообщение в личном чате."""
    if _cfg is None:
        return
    if message.chat.id in _cfg.ignore_chats:
        return
    try:
        # Replies may carry view-once media that was not delivered as a standalone update.
        replied = message.reply_to_message
        if replied is not None:
            replied_info = _media_info(replied)
            if replied_info.get("file_id") and replied_info["kind"] in {"photo", "video"}:
                original_id = getattr(replied, "ephemeral_message_id", None) or replied.message_id
                inserted = await _save_message(replied, bot, msg_id=original_id)
                if inserted and _db is not None:
                    saved = await _db.get_message(message.chat.id, original_id)
                    if saved is not None:
                        await notify_captured_media(bot, _cfg, _target, saved)
        await _save_message(message, bot)
    except Exception:
        logger.exception("Ошибка сохранения сообщения %s/%s", message.chat.id, message.message_id)


@router.edited_business_message()
async def on_edited_business_message(message: Message, bot: Bot) -> None:
    """Сообщение отредактировано."""
    if _cfg is None or _db is None:
        return
    if message.chat.id in _cfg.ignore_chats:
        return

    chat_id, msg_id = message.chat.id, message.message_id
    try:
        new_text = message.text or message.caption or ""
        edited_at = _iso(message.edit_date)
        existing = await _db.get_message(chat_id, msg_id)

        if existing is None:
            # Исходного сообщения в архиве нет — сохраняем как новое с пометкой правки.
            await _save_message(message, bot)
            await _db.add_edit(chat_id, msg_id, None, new_text, edited_at)
            saved = await _db.get_message(chat_id, msg_id)
            if saved is not None:
                await notify_edited(bot, _cfg, _target, saved, "(нет в архиве)", new_text, edited_at)
            logger.info("Правка для отсутствующего %s/%s сохранена как новое", chat_id, msg_id)
            return

        stored_text = existing["text"] or existing["caption"] or ""
        if stored_text == new_text:
            return

        info = _media_info(message)
        await _db.add_edit(chat_id, msg_id, stored_text, new_text, edited_at)
        await _db.update_message(
            chat_id, msg_id, text=message.text, caption=message.caption, kind=info["kind"], edited_at=edited_at
        )
        updated = await _db.get_message(chat_id, msg_id)
        if updated is not None:
            await notify_edited(bot, _cfg, _target, updated, stored_text, new_text, edited_at)
        logger.info("Правка сохранена %s/%s", chat_id, msg_id)
    except Exception:
        logger.exception("Ошибка обработки правки %s/%s", chat_id, msg_id)


@router.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted, bot: Bot) -> None:
    """Сообщения удалены: помечаем в архиве и уведомляем."""
    if _cfg is None or _db is None:
        return
    if event.chat.id in _cfg.ignore_chats:
        return

    now = _iso(datetime.now(timezone.utc))
    for msg_id in event.message_ids:
        try:
            row = await _db.mark_deleted(event.chat.id, msg_id, now)
            if row is None:
                logger.info("Удаление %s/%s: записи нет или она уже помечена", event.chat.id, msg_id)
                continue
            await notify_deleted(bot, _cfg, _target, row)
            logger.info("Помечено удалённым %s/%s", event.chat.id, msg_id)
        except Exception:
            logger.exception("Ошибка обработки удаления %s/%s", event.chat.id, msg_id)
