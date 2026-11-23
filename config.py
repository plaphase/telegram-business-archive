"""Чтение настроек из .env и их проверка."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Понятная ошибка конфигурации."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int | None
    owner_username: str
    db_path: Path
    notify: bool
    download_media: bool
    media_dir: Path
    ignore_chats: frozenset[int]
    retention_days: int
    tz: str
    log_level: str


def _parse_bool(name: str, value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(name: str, value: str | None, default: int, minimum: int = 0) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        result = int(value.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть целым числом, получено: {value!r}") from exc
    if result < minimum:
        raise ConfigError(f"{name} должен быть >= {minimum}, получено: {result}")
    return result


def load_config(env_file: str | Path = ".env") -> Config:
    """Загружает .env и возвращает проверенную конфигурацию."""
    load_dotenv(env_file)

    if not os.getenv("BOT_TOKEN", "").strip():
        raise ConfigError(
            "Не задан BOT_TOKEN в .env. Скопируйте .env.example в .env и впишите токен от @BotFather."
        )

    bot_token = os.environ["BOT_TOKEN"].strip()
    if ":" not in bot_token:
        raise ConfigError("BOT_TOKEN не похож на токен от @BotFather (ожидается вида 123456:ABC...).")

    # Владелец обычно определяется из business_connection; эти значения — резерв.
    owner_id_raw = os.getenv("OWNER_ID", "").strip()
    owner_id = _parse_int("OWNER_ID", owner_id_raw, 0, minimum=1) if owner_id_raw else None
    owner_username = os.getenv("OWNER_USERNAME", "").strip().lstrip("@")

    tz = os.getenv("TZ", "Europe/Moscow").strip() or "Europe/Moscow"
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Неизвестная часовая зона TZ={tz!r}. Пример: Europe/Moscow.") from exc

    ignore_raw = os.getenv("IGNORE_CHATS", "")
    ignore_chats: list[int] = []
    for part in ignore_raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ignore_chats.append(int(part))
        except ValueError as exc:
            raise ConfigError(f"IGNORE_CHATS: {part!r} — не chat_id. Пример: -1001234567890,123456789") from exc

    return Config(
        bot_token=bot_token,
        owner_id=owner_id,
        owner_username=owner_username,
        db_path=Path(os.getenv("DB_PATH", "data/archive.db").strip() or "data/archive.db"),
        notify=_parse_bool("NOTIFY", os.getenv("NOTIFY"), True),
        download_media=_parse_bool("DOWNLOAD_MEDIA", os.getenv("DOWNLOAD_MEDIA"), False),
        media_dir=Path(os.getenv("MEDIA_DIR", "data/media").strip() or "data/media"),
        ignore_chats=frozenset(ignore_chats),
        retention_days=_parse_int("RETENTION_DAYS", os.getenv("RETENTION_DAYS"), 0),
        tz=tz,
        log_level=(os.getenv("LOG_LEVEL", "INFO").strip() or "INFO").upper(),
    )
