#!/usr/bin/env bash
# Установка DialogSpy на Linux-сервер.
#
# Использование:
#   sudo bash deploy/install.sh
#
# Необязательные переменные окружения:
#   INSTALL_DIR=/opt/dialogspy   каталог установки
#   SERVICE_USER=dialogspy       системный пользователь сервиса
#
# Скрипт идемпотентен: повторный запуск обновляет код, зависимости и юнит,
# затем перезапускает сервис. Серверный .env при этом не перезаписывается.

set -euo pipefail
umask 027

INSTALL_DIR="${INSTALL_DIR:-/opt/dialogspy}"
SERVICE_USER="${SERVICE_USER:-dialogspy}"
SERVICE_NAME="dialogspy"
ENV_FILE="$INSTALL_DIR/.env"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\n==> %s\n' "$*"; }
die() { printf '\nОшибка: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запустите от root: sudo bash deploy/install.sh"
command -v python3 >/dev/null 2>&1 || die "не найден python3"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "нужен Python 3.11 или новее"
command -v systemctl >/dev/null 2>&1 || die "systemd не найден"
[ -f "$SRC_DIR/requirements.txt" ] || die "запускайте скрипт из проекта DialogSpy"

mkdir -p "$INSTALL_DIR"
if [ "$SRC_DIR" -ef "$INSTALL_DIR" ]; then
    # Повторный запуск прямо из каталога установки — копировать нечего.
    log "Проект уже в $INSTALL_DIR, копирование пропускаю"
elif command -v rsync >/dev/null 2>&1; then
    log "Копирую файлы в $INSTALL_DIR"
    rsync -a \
        --exclude '.git' --exclude '.venv' --exclude 'data' \
        --exclude '__pycache__' --exclude '*.pyc' \
        --exclude '.cloud-attachments' \
        --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \
        --exclude '.env' \
        "$SRC_DIR"/ "$INSTALL_DIR"/
else
    log "Копирую файлы в $INSTALL_DIR"
    ( cd "$SRC_DIR" && tar -cf - \
        --exclude='./.git' --exclude='./.venv' --exclude='./data' \
        --exclude='./__pycache__' --exclude='*.pyc' \
        --exclude='./.cloud-attachments' \
        --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
        --exclude='./.env' . ) | tar -C "$INSTALL_DIR" -xf -
fi

# .env: сохраняем серверный конфиг при обновлении; на первой установке
# берём локальный .env, если он есть, иначе создаём из публичного шаблона.
if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$SRC_DIR/.env" ]; then
        cp "$SRC_DIR/.env" "$ENV_FILE"
    elif [ -f "$SRC_DIR/.env.example" ]; then
        cp "$SRC_DIR/.env.example" "$ENV_FILE"
    else
        die "не найден ни .env, ни .env.example"
    fi
fi

# BOT_TOKEN обязателен; остальные параметры имеют безопасные значения по умолчанию.
if ! grep -Eq '^BOT_TOKEN=.+$' "$ENV_FILE"; then
    [ -t 0 ] || die "в $ENV_FILE не заполнено: BOT_TOKEN; запустите установщик в интерактивном терминале"
    printf '\nНастройка DialogSpy. Токен можно получить у @BotFather командой /newbot.\n'
    printf 'BOT_TOKEN (ввод скрыт): '
    IFS= read -r -s bot_token
    printf '\n'
    [ -n "$bot_token" ] || die "BOT_TOKEN не может быть пустым"
    [[ "$bot_token" == *:* ]] || die "BOT_TOKEN должен быть вида 123456:ABC..."
    if grep -q '^BOT_TOKEN=' "$ENV_FILE"; then
        sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=${bot_token}|" "$ENV_FILE"
    else
        printf '\nBOT_TOKEN=%s\n' "$bot_token" >> "$ENV_FILE"
    fi
    unset bot_token
fi

log "Создаю системного пользователя $SERVICE_USER"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" \
        || useradd --system --home-dir "$INSTALL_DIR" --shell /sbin/nologin "$SERVICE_USER"
fi

# useradd --system может не создать одноимённую группу — берём основную группу пользователя.
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
[ -n "$SERVICE_GROUP" ] || die "не удалось определить группу пользователя $SERVICE_USER"
log "Пользователь: $SERVICE_USER, группа: $SERVICE_GROUP"

mkdir -p "$INSTALL_DIR/data"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR"
# Каталог, база и .env не должны быть доступны остальным пользователям системы.
chmod -R o-rwx "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR/data"
chmod 600 "$ENV_FILE"

as_user() {
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$SERVICE_USER" -- env HOME="$INSTALL_DIR" PIP_NO_CACHE_DIR=1 "$@"
    else
        sudo -u "$SERVICE_USER" env HOME="$INSTALL_DIR" PIP_NO_CACHE_DIR=1 "$@"
    fi
}

log "Устанавливаю зависимости в venv"
if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
    as_user python3 -m venv "$INSTALL_DIR/.venv"
fi
as_user "$INSTALL_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
as_user "$INSTALL_DIR/.venv/bin/python" -m pip install --quiet -r "$INSTALL_DIR/requirements.txt"

log "Устанавливаю systemd-юнит"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=DialogSpy - архив сообщений Telegram Business
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
if systemctl is-active --quiet "$SERVICE_NAME"; then
    systemctl restart "$SERVICE_NAME"
else
    systemctl start "$SERVICE_NAME"
fi

sleep 3
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager || true
    die "сервис не запустился, смотрите логи выше"
fi

log "Готово. Статус сервиса:"
systemctl --no-pager --full status "$SERVICE_NAME" | head -n 12 || true

printf '\nЛоги: journalctl -u %s -f  или  tail -f %s/data/bot.log\n' "$SERVICE_NAME" "$INSTALL_DIR"
printf 'Осталось подключить бота: @BotFather → /mybots → Bot Settings → Business Mode,\n'
printf 'затем Telegram → Настройки → Бизнес → Автоматизация чатов.\n'
