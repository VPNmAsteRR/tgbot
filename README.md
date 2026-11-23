# Telegram DM relay bot

Присылает владельцу **каждое личное сообщение**, которое пишут боту: текст, фото,
видео, голосовые, кружки, документы, стикеры, опросы. Группы и каналы игнорируются.
Медиа скачивается во временный файл, отправляется в личку владельца и **сразу
удаляется** — на диске ничего не остаётся.

Одноразовые фото и видео сохраняются автоматически, в момент получения: боту они
приходят уже как обычный файл, поэтому переотправка снимает «одноразовость».
Никаких команд и подтверждений не нужно.

Зависимостей нет: Python 3.10+ и стандартная библиотека.

## Запуск

```bash
export BOT_TOKEN=123456789:AA...   # токен @BotFather
export OWNER_ID=123456789          # ваш user id
python3 bot.py
```

`OWNER_ID` — обязателен для предсказуемости; без него владельцем станет первый,
кто напишет боту `/start`. Узнать id: @userinfobot.

Никаких настроек Privacy Mode не нужно — они влияют только на группы. Достаточно,
чтобы бот был доступен по юзернейму: любой, кто напишет ему в личку, попадёт в релей.

## Как это работает

1. `message` с `chat.type == "private"` → в очередь (поллинг не блокируется).
2. Медиа: `getFile` → temp-файл → отправка владельцу как обычный файл → `unlink`.
3. Текст и подписи уходят с шапкой: отправитель, время, `✏️ изменено` при правке.
4. В SQLite пишется только факт: `sent` / `forwarded` / `error`.
5. Больше 20 МБ (лимит `getFile`) → `forwardMessage`, без диска.

Свои текстовые сообщения в чате с ботом не пересылаются (это команды и заметки),
свои медиа — пересылаются обратно, чтобы осталась нормальная копия.

## Команды (в личке владельца)

| Команда | Что делает |
| --- | --- |
| `/start`, `/help` | справка, захват владельца |
| `/stats` | счётчики по статусам, размер temp-буфера |
| `/mute <user_id>` | не пересылать от пользователя |
| `/unmute <user_id>` | вернуть пользователя |
| `/muted` | список выключенных |

`user_id` виден в шапке каждого пересланного сообщения. Сообщения от замьюченных
не журналируются и не занимают место.

## Файлы

```
data/
  relay.db     # журнал: что переслано, статус, мьюты, offset
  tmp/         # только на время одной отправки; чистится при старте и после каждой отправки
```

Медиа в `data/` не хранится.

## Настройки

| Переменная / флаг | По умолчанию | Значение |
| --- | --- | --- |
| `BOT_TOKEN` / `--token` | — | токен @BotFather |
| `OWNER_ID` / `--owner` | первый `/start` | кому пересылать |
| `DATA_DIR` / `--data-dir` | `data` | журнал и temp |
| `API_BASE` / `--api-base` | `https://api.telegram.org` | локальный Bot API server |
| `--max-download-mb` | `20` | выше — только `forwardMessage` |

## Ограничения

- **20 МБ** — максимум скачивания облачного Bot API; больше пересылается форвардом.
- **≈20 сообщений/мин в один чат** — лимит Telegram. Бот ловит `429` и ждёт
  `retry_after`, очередь при этом растёт.
- Удаление сообщений ботам не приходит, но копия уже отправлена в момент получения.
- Сообщения, отправленные боту до запуска, недоступны.

## Деплой на сервер

Нужен любой Linux VPS с Python 3.10+. Входящие порты открывать не нужно: бот сам
ходит на `api.telegram.org` по 443.

### 1. Закинуть файлы

С локальной машины (из корня проекта):

```bash
rsync -av --exclude data --exclude __pycache__ --exclude .env \
  tgbot/ user@server:/tmp/tgbot/
```

Можно и `scp -r tgbot user@server:/tmp/`.

### 2. Установить (systemd)

```bash
ssh user@server
sudo BOT_TOKEN=123456789:AA... OWNER_ID=123456789 bash /tmp/tgbot/deploy/install.sh
```

Скрипт создаёт системного пользователя `tg-relay`, раскладывает код в
`/opt/tg-relay`, state в `/var/lib/tg-relay`, env в `/etc/tg-relay.env` (chmod 600),
ставит unit `tg-relay.service` и запускает его. Повторный запуск безопасен:
env-файл не перезаписывается, код обновляется.

Если `python3` нет:

```bash
sudo apt install -y python3        # Debian/Ubuntu
sudo dnf install -y python3        # RHEL/Fedora
```

### 3. Проверить

```bash
systemctl status tg-relay
journalctl -u tg-relay -f
```

В логе должно быть `bot @username id=... -> owner ...`. Напишите боту в личку —
сообщение должно прийти владельцу.

### 4. Обновление

```bash
rsync -av --exclude data --exclude __pycache__ --exclude .env \
  tgbot/ user@server:/tmp/tgbot/
ssh user@server 'sudo bash /tmp/tgbot/deploy/install.sh'
```

### Вручную, без скрипта

```bash
sudo useradd --system --home /opt/tg-relay --shell /usr/sbin/nologin tg-relay
sudo install -d -o root -g root -m 755 /opt/tg-relay
sudo install -m 644 bot.py store.py tgapi.py /opt/tg-relay/
sudo install -d -o tg-relay -g tg-relay -m 700 /var/lib/tg-relay
printf 'BOT_TOKEN=%s\nOWNER_ID=%s\n' '123:abc' '123456789' | sudo tee /etc/tg-relay.env >/dev/null
sudo chmod 600 /etc/tg-relay.env
sudo install -m 644 deploy/tg-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-relay
```

### Docker

```bash
cp .env.example .env && $EDITOR .env      # BOT_TOKEN, OWNER_ID
docker compose up -d --build
docker compose logs -f
```

`compose.yaml` монтирует `./data` в `/data`, контейнер read-only, порты не нужны.
Обновление: `git pull`/rsync + `docker compose up -d --build`.

### Что где лежит

| Путь | Содержимое |
| --- | --- |
| `/opt/tg-relay` | код, только чтение для сервиса |
| `/var/lib/tg-relay` | `relay.db` + `tmp/`, единственное место записи |
| `/etc/tg-relay.env` | токен и owner, `0600` |
