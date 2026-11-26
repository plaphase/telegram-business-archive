"""Уведомления владельцу в личку с ботом: HTML, экранирование, обрезка."""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import FSInputFile, LinkPreviewOptions

from config import Config

logger = logging.getLogger(__name__)

MAX_BODY = 3000          # обрезка текста сообщения
MAX_EDIT_PART = 1400     # обрезка каждой версии в правке
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


@dataclass
class NotifyTarget:
    """Куда и от чьего имени слать уведомления (заполняется из business_connection)."""

    chat_id: int | None = None
    user_id: int | None = None
    name: str = ""
    username: str = ""


# kind -> (эмодзи, название)
KINDS: dict[str, tuple[str, str]] = {
    "text": ("✉️", "Текст"),
    "photo": ("🖼", "Фото"),
    "video": ("🎬", "Видео"),
    "sticker": ("🎭", "Стикер"),
    "voice": ("🎤", "Голосовое"),
    "video_note": ("⭕️", "Кружок"),
    "document": ("📄", "Документ"),
    "audio": ("🎵", "Аудио"),
    "location": ("📍", "Геометка"),
    "contact": ("👤", "Контакт"),
    "other": ("📎", "Другое"),
}

# Короткие подписи зон; для остальных берётся последний сегмент имени зоны.
TZ_LABELS: dict[str, str] = {
    "Europe/Moscow": "МСК",
    "Europe/Kaliningrad": "Калининград",
    "Europe/Samara": "Самара",
    "Asia/Yekaterinburg": "Екатеринбург",
    "Asia/Omsk": "Омск",
    "Asia/Novosibirsk": "Новосибирск",
    "Asia/Krasnoyarsk": "Красноярск",
    "Asia/Irkutsk": "Иркутск",
    "Asia/Vladivostok": "Владивосток",
    "Europe/Minsk": "Минск",
    "Europe/Kyiv": "Киев",
    "Asia/Almaty": "Алматы",
    "UTC": "UTC",
}


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _clip(text: str, limit: int = MAX_BODY) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _escaped_clip(text: str | None, limit: int = MAX_BODY) -> str:
    """Обрезка и экранирование; результат по длине не превышает limit."""
    raw = _clip(text or "", limit)
    escaped = _esc(raw)
    # HTML-сущности (&amp; и т.п.) раздувают текст — при переполнении режем сырой текст
    while raw and len(escaped) > limit:
        raw = raw[: int(len(raw) * 0.9)]
        escaped = _esc(raw)
    if len(escaped) > limit:
        escaped = escaped[: limit - 1] + "…"
    return escaped


def _human_size(size: int | None) -> str:
    if not size:
        return ""
    value = float(size)
    units = ("Б", "КБ", "МБ", "ГБ")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _format_time(iso_value: str | None, cfg: Config) -> str:
    if not iso_value:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_value)
    except ValueError:
        return iso_value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(ZoneInfo(cfg.tz))
    offset = local.utcoffset() or timedelta(0)
    minutes_total = int(offset.total_seconds() // 60)
    sign = "+" if minutes_total >= 0 else "-"
    hours, minutes = divmod(abs(minutes_total), 60)
    offset_text = f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")
    label = TZ_LABELS.get(cfg.tz) or cfg.tz.rsplit("/", 1)[-1].upper()
    return f"{local:%d.%m.%Y %H:%M:%S} ({label}, {offset_text})"


def _owner_line(target: NotifyTarget) -> str:
    return f"@{_esc(target.username)}" if target.username else "@нет"


def _sender_line(row: Any, target: NotifyTarget) -> str:
    if target.user_id is not None and row["sender_id"] == target.user_id:
        return "Вы"
    name = _esc(row["sender_name"] or "Неизвестно")
    username = (row["sender_username"] or "").strip()
    handle = f"@{_esc(username)}" if username else "@нет"
    return f"{name} {handle}"


def _kind_label(row: Any) -> str:
    """«🎭 Стикер 😀» или «🖼 Фото» — для строки типа и тела уведомления."""
    kind = row["kind"] or "other"
    emoji, name = KINDS.get(kind, KINDS["other"])
    label = f"{emoji} {name}"
    if kind == "sticker" and row["sticker_emoji"]:
        label += f" {_esc(row['sticker_emoji'])}"
    return label


def _type_line(row: Any) -> str:
    return f"📎 Тип: {_kind_label(row)}"


def _body(row: Any) -> str:
    text = row["text"] or row["caption"]
    if text:
        return _escaped_clip(text)

    parts = [_kind_label(row)]
    if row["file_name"]:
        parts.append(f"«{_esc(row['file_name'])}»")
    size = _human_size(row["file_size"])
    if size:
        parts.append(size)
    return " · ".join(parts)


def _header(
    title: str,
    row: Any,
    cfg: Config,
    target: NotifyTarget,
    time_value: str | None = None,
    *,
    include_chat: bool = False,
) -> str:
    lines = [title]
    if include_chat:
        chat_title = row["chat_title"] or row["chat_id"]
        lines.append(f"💬 Чат: {_esc(chat_title)}")
    lines.extend(
        (
            f"👤 Отправитель: {_sender_line(row, target)}",
            f"💼 Владельцу: {_owner_line(target)}",
            _type_line(row),
            f"🕐 {_format_time(time_value or row['date'], cfg)}",
        )
    )
    return "\n".join(lines)


async def _send(bot: Bot, cfg: Config, target: NotifyTarget, text: str) -> None:
    """Отправка с ожиданием при flood control; ошибки только логируются."""
    if not cfg.notify:
        return
    if target.chat_id is None:
        logger.warning(
            "Получатель уведомлений неизвестен: добавьте бота в «Автоматизацию чатов» или задайте OWNER_ID"
        )
        return
    for attempt in (1, 2):
        try:
            await bot.send_message(
                chat_id=target.chat_id,
                text=text,
                parse_mode="HTML",
                link_preview_options=NO_PREVIEW,
            )
            return
        except TelegramRetryAfter as exc:
            logger.warning("Flood control: ждём %s с и повторяем", exc.retry_after)
            if attempt == 2:
                logger.error("Уведомление не отправлено из-за flood control")
                return
            await asyncio.sleep(exc.retry_after)
        except Exception:
            logger.exception("Не удалось отправить уведомление владельцу")
            return


async def notify_deleted(bot: Bot, cfg: Config, target: NotifyTarget, row: Any) -> None:
    text = _header("🗑 <b>Сообщение удалено</b>", row, cfg, target, include_chat=True) + "\n\n" + _body(row)
    media_path = row["media_path"]
    if media_path and Path(media_path).is_file() and cfg.notify and target.chat_id is not None:
        media = FSInputFile(media_path, filename=row["file_name"] or Path(media_path).name)
        try:
            if row["kind"] == "photo":
                await bot.send_photo(target.chat_id, photo=media, caption=text, parse_mode="HTML")
            elif row["kind"] == "video":
                await bot.send_video(target.chat_id, video=media, caption=text, parse_mode="HTML")
            else:
                await bot.send_document(target.chat_id, document=media, caption=text, parse_mode="HTML")
            return
        except Exception:
            logger.exception("Не удалось прикрепить файл к уведомлению об удалении")
    await _send(bot, cfg, target, text)


async def notify_captured_media(bot: Bot, cfg: Config, target: NotifyTarget, row: Any) -> None:
    """Отправляет файл, полученный из ответа на самоуничтожающееся медиа."""
    text = _header("📥 <b>Медиа из ответа сохранено</b>", row, cfg, target) + "\n\n" + _body(row)
    media_path = row["media_path"]
    if not cfg.notify or target.chat_id is None:
        return
    if not media_path or not Path(media_path).is_file():
        await _send(bot, cfg, target, text + "\n\nФайл не удалось скачать на сервер.")
        return
    media = FSInputFile(media_path, filename=row["file_name"] or Path(media_path).name)
    try:
        if row["kind"] == "photo":
            await bot.send_photo(target.chat_id, photo=media, caption=text, parse_mode="HTML")
        else:
            await bot.send_video(target.chat_id, video=media, caption=text, parse_mode="HTML")
    except Exception:
        logger.exception("Не удалось отправить медиа, полученное из reply_to_message")
        await _send(bot, cfg, target, text)


async def notify_edited(
    bot: Bot,
    cfg: Config,
    target: NotifyTarget,
    row: Any,
    old_text: str | None,
    new_text: str | None,
    edited_at: str | None = None,
) -> None:
    old = _escaped_clip(old_text or "—", MAX_EDIT_PART)
    new = _escaped_clip(new_text or "—", MAX_EDIT_PART)
    body = f"Было: <s>{old}</s>\nСтало: {new}"
    text = _header("✏️ <b>Сообщение изменено</b>", row, cfg, target, edited_at or row["edited_at"]) + "\n\n" + body
    await _send(bot, cfg, target, text)


async def notify_connected(bot: Bot, cfg: Config, target: NotifyTarget) -> None:
    username = f"@{_esc(target.username)}" if target.username else "@нет"
    lines = [
        "✅ <b>Бот подключён</b>",
        f"👤 Отправитель: {_esc(target.name) or 'Вы'} {username}",
        f"💼 Владельцу: {_owner_line(target)}",
        f"🕐 {_format_time(datetime.now(timezone.utc).isoformat(), cfg)}",
        "",
        "Бизнес-бот подключён к аккаунту и готов сохранять сообщения.",
    ]
    await _send(bot, cfg, target, "\n".join(lines))
