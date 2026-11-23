# DialogSpy — локальный архив сообщений Telegram Business

Локальный архив сообщений личных чатов Telegram Business. Python 3.11+, aiogram 3,
SQLite и systemd; архив остаётся на вашем сервере.

Бот на aiogram 3 подключается к вашему аккаунту как Business-бот (Secretary Mode) и
складывает копии сообщений из личных чатов в локальный SQLite. Всё работает на вашем
сервере: без вебхуков, облака и сторонних сервисов.

Что умеет:

- сохранять новые сообщения (`business_message`);
- хранить историю правок (`edited_business_message`) — старая и новая версии;
- помечать удалённые сообщения (`deleted_business_messages`) — Telegram присылает
  только ID, поэтому копия сохраняется заранее;
- присылать уведомления вам в личку с ботом (удаление, правка, подключение);
- по желанию скачивать медиа в `data/media/` (`DOWNLOAD_MEDIA=1`);
- удалять старые записи по `RETENTION_DAYS`;
- игнорировать указанные чаты (`IGNORE_CHATS`).

Бот **ничего не отправляет в архивные чаты**: только читает и пишет вам.

---

## Что понадобится

- Telegram Premium (Business-аккаунт) и ваш аккаунт Telegram.
- Домашний сервер с Linux и Python **3.11+**.
- systemd (для автозапуска).

## 1. Создание бота

1. Откройте [@BotFather](https://t.me/BotFather), отправьте `/newbot`, задайте имя и
   username. Сохраните токен — он пойдёт в `BOT_TOKEN`.
2. В BotFather: `/mybots` → выберите бота → **Bot Settings** → включите
   **Business Mode** (в интерфейсе может называться Secretary Mode).
3. `OWNER_ID` и `OWNER_USERNAME` указывать не нужно: бот получает владельца и id
   личного чата из `business_connection` и сохраняет их в SQLite.

## 2. Подключение к аккаунту

В Telegram: **Настройки → Бизнес → Автоматизация чатов** (Chat Automation) → выберите
бота → разрешите ему доступ и отметьте чаты, которые он может видеть (нужны личные).

После подключения бот пришлёт вам «✅ Бот подключён». В этом же событии Telegram передаёт
id владельца и id личного чата с ботом — бот сохраняет их в SQLite и дальше шлёт
уведомления туда, в том числе после перезапуска.

## 3. Установка на Linux

### Автоматическая установка (рекомендуется)

1. Подготовьте сервер с Ubuntu/Debian, Python 3.11+ и systemd.
2. Установите Git и Python, затем клонируйте исходники в отдельный каталог и запустите установщик:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
mkdir -p ~/src
cd ~/src
git clone https://github.com/plaphase/telegram-business-archive.git
cd telegram-business-archive
sudo bash deploy/install.sh
```

Каталог клона (`~/src/telegram-business-archive`) содержит исходники. Установщик копирует
приложение в `/opt/dialogspy`, создаёт там виртуальное окружение и systemd-сервис.
При первой установке он по шагам попросит только обязательный `BOT_TOKEN` (ввод скрыт).
Получите его у @BotFather командой `/newbot`. Данные владельца вводить не нужно: бот
сам получит их после подключения в Telegram. Токен не попадает в GitHub; серверный
`/opt/dialogspy/.env` закрыт правами доступа.

Можно задать другой каталог и имя системного пользователя перед запуском, например:

```bash
sudo INSTALL_DIR=/opt/dialogspy SERVICE_USER=dialogspy bash deploy/install.sh
```

При повторном запуске установщик обновляет файлы и перезапускает сервис, сохраняя
существующий `/opt/dialogspy/.env` и архив данных. Локальный `.env` из каталога клона
не копируется.
### Ручная установка

```bash
git clone https://github.com/plaphase/telegram-business-archive.git
cd telegram-business-archive
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env                 # заполните BOT_TOKEN на сервере
python main.py            # проверка запуска; остановка — Ctrl+C
```

Не публикуйте `.env` и не вставляйте токен в Issues, обсуждения или логи. Для
автозапуска вручную настройте systemd по следующему разделу.

## 4. Автозапуск (systemd)

```bash
sudo cp deploy/dialogspy.service /etc/systemd/system/dialogspy.service
sudo nano /etc/systemd/system/dialogspy.service   # User, WorkingDirectory, ExecStart
sudo systemctl daemon-reload
sudo systemctl enable --now dialogspy
```

Проверка и логи:

```bash
systemctl status dialogspy
journalctl -u dialogspy -f
tail -f /opt/dialogspy/data/bot.log
```

Перезапуск после изменения `.env`: `sudo systemctl restart dialogspy`.

## 5. Где архив и как смотреть

База: `data/archive.db`. Удобно через `sqlite3`:

```bash
sqlite3 data/archive.db
```

Таблицы:

- `msgs` — сообщения: `chat_id, chat_title, msg_id, sender_id, sender_name, date, kind,
  text, caption, reply_to_msg_id, edited_at, deleted_at` (+ служебные `sender_username`,
  `file_id`, `file_name`, `file_size`, `sticker_emoji`, `media_path`);
- `edits` — история правок: `chat_id, msg_id, old_text, new_text, edited_at`;
- `connections` — история подключений бота к аккаунту.

`kind` может быть: `text`, `photo`, `video`, `voice`, `video_note`, `document`,
`sticker`, `audio`, `location`, `contact`, `other`. Дата хранится в ISO (UTC).

### Примеры SQL

Удалённые за последнюю неделю:

```sql
SELECT chat_title, sender_name, text, caption, date
  FROM msgs
 WHERE deleted_at IS NOT NULL
   AND date(date) >= date('now', '-7 days')
 ORDER BY date DESC;
```

История правок конкретного сообщения:

```sql
SELECT old_text, new_text, edited_at
  FROM edits
 WHERE chat_id = 123456789 AND msg_id = 42
 ORDER BY edited_at;
```

Топ-10 чатов по числу сообщений:

```sql
SELECT chat_title, COUNT(*) AS cnt
  FROM msgs
 GROUP BY chat_id
 ORDER BY cnt DESC
 LIMIT 10;
```

Поиск по тексту и подписям:

```sql
SELECT chat_title, sender_name, date, COALESCE(text, caption) AS body
  FROM msgs
 WHERE text LIKE '%договор%' OR caption LIKE '%договор%'
 ORDER BY date DESC;
```

Все сообщения одного чата:

```sql
SELECT msg_id, sender_name, date, kind, text, caption, deleted_at
  FROM msgs
 WHERE chat_id = 123456789
 ORDER BY msg_id;
```

Последнее подключение бота:

```sql
SELECT user_name, is_enabled, connected_at
  FROM connections
 ORDER BY connected_at DESC
 LIMIT 5;
```

## 6. Как выглядят уведомления

HTML-сообщение в личку с ботом, без превью ссылок. Удаление:

> 🗑 **Сообщение удалено**
> 👤 Отправитель: Иван Петров @ivan_p
> 💼 Владельцу: @my_account
> 📎 Тип: ✉️ Текст
> 🕐 23.11.2026 14:05:11 (МСК, UTC+3)
>
> Привет, встретимся в 19:00?

Правка:

> ✏️ **Сообщение изменено**
> 👤 Отправитель: Иван Петров @ivan_p
> 💼 Владельцу: @my_account
> 📎 Тип: ✉️ Текст
> 🕐 23.11.2026 14:07:02 (МСК, UTC+3)
>
> Было: ~~встретимся в 19:00?~~
> Стало: встретимся в 20:00?

Подключение:

> ✅ **Бот подключён**
> 👤 Отправитель: Вы @my_account
> 💼 Владельцу: @my_account
> 🕐 23.11.2026 13:59:40 (МСК, UTC+3)
>
> Бизнес-бот подключён к аккаунту и готов сохранять сообщения.

## 7. Резервная копия

База в режиме WAL, поэтому копируйте её при остановленном сервисе:

```bash
sudo systemctl stop dialogspy
cp data/archive.db data/archive-$(date +%F).db
sudo systemctl start dialogspy
```

Можно просто периодически копировать весь каталог `data/` — при остановленном сервисе
это безопасно.

## 8. Важные ограничения и безопасность

- Бот видит **только личные чаты**, которые вы разрешили при подключении, и **только
  сообщения, отправленные/полученные после подключения**. Историю до подключения
  Telegram не отдаёт — обойти это нельзя.
- В архиве лежат чужие персональные данные. Не держите `data/` в публичной папке или
  в синхронизируемом облачном каталоге, ограничьте доступ:
  `chmod 700 data && chmod 600 data/archive.db`.
- `.env`, `.cloud-attachments/`, `.freebuff/`, `data/`, журналы и базы исключены через `.gitignore` и не публикуются в репозитории.
- Бот не отвечает в чатах и не меняет их содержимое.

## Структура проекта

```
main.py                 запуск, логирование, long polling, retention
config.py               чтение .env и проверка настроек
db.py                   SQLite (aiosqlite, WAL, схема)
handlers.py             бизнес-события Telegram
notifications.py        HTML-уведомления в личку
requirements.txt        aiogram, aiosqlite, python-dotenv
.env.example            шаблон настроек
deploy/dialogspy.service  systemd-юнит
```
