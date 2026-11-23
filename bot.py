#!/usr/bin/env python3
"""Telegram relay bot: sends every message it sees to the owner right away.

Media is downloaded to a temp file, uploaded to the owner as a normal file, and
deleted immediately — nothing stays on disk. A small SQLite journal keeps track
of what was relayed, muted chats and the update offset.

Run:
    BOT_TOKEN=123:abc OWNER_ID=123456 python3 bot.py
"""

import argparse
import html
import json
import mimetypes
import os
import queue
import shutil
import signal
import sys
import tempfile
import threading
import time

from store import Store
from tgapi import BotAPI, TelegramError

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # cloud Bot API limit for getFile
MEDIA_FIELDS = ("video", "animation", "video_note", "voice", "audio", "document", "sticker")
TEXT_LIMIT = 4000
CAPTION_LIMIT = 1000


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, file=sys.stderr, flush=True)


def human(size):
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def display_name(entity):
    if not entity:
        return None
    name = " ".join(
        p for p in (entity.get("first_name"), entity.get("last_name")) if p
    )
    return name or entity.get("title") or entity.get("username") or str(entity.get("id"))


def pick_media(msg):
    if msg.get("photo"):
        return "photo", msg["photo"][-1]
    for key in MEDIA_FIELDS:
        if key in msg:
            return key, msg[key]
    return None, None


class RelayBot:
    def __init__(self, api, store, data_dir, owner_id=None, max_download=MAX_DOWNLOAD_BYTES):
        self.api = api
        self.store = store
        self.owner_id = int(owner_id) if owner_id else None
        self.max_download = max_download
        self.tmp_dir = os.path.join(data_dir, "tmp")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self._purge_tmp()
        self.queue = queue.Queue(maxsize=1000)
        self._stop = None
        self._worker = threading.Thread(target=self._work, name="relay", daemon=True)

    # ------------------------------------------------------------------ setup
    def _purge_tmp(self):
        for name in os.listdir(self.tmp_dir):
            path = os.path.join(self.tmp_dir, name)
            try:
                shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
            except OSError:
                pass

    def _owner(self):
        if self.owner_id:
            return self.owner_id
        value = self.store.meta_get("owner_id")
        return int(value) if value else None

    # ------------------------------------------------------------------- loop
    def run(self, stop_event=None, poll_timeout=30):
        self._stop = stop_event
        self._worker.start()
        me = self.api.get_me()
        log(f"bot @{me.get('username')} id={me.get('id')} -> owner {self._owner() or '?'}")
        offset = self.store.meta_get("offset")
        offset = int(offset) if offset else None
        while not (stop_event and stop_event.is_set()):
            try:
                updates = self.api.get_updates(
                    offset,
                    timeout=poll_timeout,
                    allowed_updates=["message", "edited_message"],
                )
            except TelegramError as exc:
                log(f"getUpdates: {exc}")
                time.sleep(3)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except Exception as exc:
                    log(f"update {update.get('update_id')}: {type(exc).__name__}: {exc}")
            if updates:
                self.store.meta_set("offset", offset)
        self.queue.put(None)
        self._worker.join(timeout=10)

    def handle_update(self, update):
        if "edited_message" in update:
            self.relay_edit(update["edited_message"])
            return
        msg = update.get("message")
        if not msg:
            return
        if (msg.get("chat") or {}).get("type") != "private":
            return
        if self.maybe_command(msg):
            return
        try:
            self.queue.put(msg, timeout=5)
        except queue.Full:
            log("queue full, dropping message")

    def relay_edit(self, msg):
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        owner = self._owner()
        user = msg.get("from") or {}
        if chat.get("type") != "private" or chat_id is None or not owner:
            return
        if self.store.is_muted(chat_id):
            return
        if chat_id == owner and user.get("id") == owner:
            return
        header = self._header(chat, user, msg)
        text = msg.get("text") or msg.get("caption") or ""
        body = html.escape(text) if text else "<i>[медиа без подписи]</i>"
        self._send_text(owner, f"{header}\n\n{body}")

    def _work(self):
        while True:
            msg = self.queue.get()
            if msg is None:
                return
            try:
                self.relay(msg)
            except Exception as exc:
                log(f"relay failed: {type(exc).__name__}: {exc}")
            finally:
                self.queue.task_done()

    # ---------------------------------------------------------------- commands
    def maybe_command(self, msg):
        text = msg.get("text") or ""
        if not text.startswith("/"):
            return False
        chat = msg.get("chat") or {}
        user = msg.get("from") or {}
        if chat.get("type") != "private" or not user.get("id"):
            return False
        head, _, args = text.partition(" ")
        name = head[1:].split("@", 1)[0].lower()
        handler = getattr(self, f"cmd_{name}", None)
        if handler is None:
            return False
        owner = self._owner()
        if not owner:
            if name != "start":
                return False
            self.store.meta_set("owner_id", user["id"])
            self.owner_id = int(user["id"])
            log(f"owner claimed: {user['id']}")
            owner = self.owner_id
        if int(user["id"]) != int(owner):
            return False
        try:
            handler(chat["id"], msg, args.strip())
        except TelegramError as exc:
            log(f"/{name}: {exc}")
            self._send_text(chat["id"], f"⚠️ {html.escape(str(exc))}")
        return True

    def cmd_start(self, chat_id, msg, args):
        self._send_text(
            chat_id,
            "📡 <b>ЛС-релей включён</b>\n\n"
            "Присылаю сюда каждое личное сообщение, которое мне пишут: текст, фото, "
            "видео, голосовые, кружки, документы, стикеры. Группы и каналы игнорирую.\n\n"
            "Медиа скачивается во временный файл, отправляется сюда и сразу удаляется "
            "с сервера — на диске ничего не остаётся.\n\n"
            "Одноразовые медиа сохраняются автоматически, в момент получения.\n\n"
            "/stats — статистика\n"
            "/mute &lt;user_id&gt; — не пересылать от пользователя\n"
            "/unmute &lt;user_id&gt;\n"
            "/muted — список выключенных",
        )

    cmd_help = cmd_start

    def cmd_stats(self, chat_id, msg, args):
        st = self.store.stats()
        lines = [f"📊 всего: <b>{st['total']}</b>"]
        for status, count in sorted(st["by_status"].items()):
            lines.append(f"  {status}: {count}")
        lines.append(f"🔇 выключено пользователей: <b>{st['muted']}</b>")
        if st["last"]:
            lines.append(
                "🕒 последнее: " + time.strftime("%d.%m %H:%M", time.localtime(st["last"]))
            )
        used = sum(
            os.path.getsize(os.path.join(self.tmp_dir, n))
            for n in os.listdir(self.tmp_dir)
            if os.path.isfile(os.path.join(self.tmp_dir, n))
        )
        lines.append(f"💾 временный буфер: {human(used)}")
        self._send_text(chat_id, "\n".join(lines))

    def cmd_mute(self, chat_id, msg, args):
        try:
            target = int(args)
        except ValueError:
            self._send_text(chat_id, "usage: /mute &lt;user_id&gt; (id виден в шапке пересланного)")
            return
        self.store.mute(target)
        self._send_text(chat_id, f"🔇 <code>{target}</code> выключен")

    def cmd_unmute(self, chat_id, msg, args):
        try:
            target = int(args)
        except ValueError:
            self._send_text(chat_id, "usage: /unmute &lt;chat_id&gt;")
            return
        self.store.unmute(target)
        self._send_text(chat_id, f"🔔 <code>{target}</code> включён")

    def cmd_muted(self, chat_id, msg, args):
        rows = self.store.muted_list()
        if not rows:
            self._send_text(chat_id, "выключенных нет")
            return
        self._send_text(
            chat_id,
            "\n".join(f"<code>{r['chat_id']}</code> {html.escape(str(r['title'] or ''))}" for r in rows),
        )

    # ------------------------------------------------------------------ relay
    def relay(self, msg):
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        message_id = msg.get("message_id")
        owner = self._owner()
        if chat_id is None or message_id is None or not owner:
            return
        if self.store.is_muted(chat_id):
            return

        media_type, media = pick_media(msg)
        text = msg.get("text") or msg.get("caption") or ""
        user = msg.get("from") or {}

        # messages the owner sends in the bot chat: text is pointless to echo,
        # media is re-uploaded so the owner keeps a permanent copy
        if chat_id == owner and user.get("id") == owner and not media_type:
            return

        title = display_name(chat)
        sender = display_name(user)
        if not self.store.begin(chat_id, message_id, title, sender, media_type or "text"):
            return

        header = self._header(chat, user, msg)
        try:
            if media_type:
                size = (media or {}).get("file_size") or 0
                if size and size > self.max_download:
                    self._call(self.api.call, "forwardMessage", {
                        "chat_id": owner, "from_chat_id": chat_id, "message_id": message_id,
                    })
                    status = "forwarded"
                else:
                    path = self._fetch(media)
                    try:
                        self._send_media(owner, path, media_type, header, text)
                    finally:
                        self._unlink(path)
                    status = "sent"
            else:
                self._send_text(owner, self._compose(header, text, msg))
                status = "sent"
            self.store.finish(chat_id, message_id, status)
        except TelegramError as exc:
            try:
                self._call(self.api.call, "forwardMessage", {
                    "chat_id": owner, "from_chat_id": chat_id, "message_id": message_id,
                })
                self.store.finish(chat_id, message_id, "forwarded", str(exc))
            except TelegramError as exc2:
                log(f"relay {chat_id}/{message_id} failed: {exc2}")
                self.store.finish(chat_id, message_id, "error", str(exc2))

    # ------------------------------------------------------------------ media
    def _fetch(self, media):
        info = self._call(self.api.get_file, media["file_id"])
        remote = info["file_path"]
        try:
            data = self._call(self.api.download, remote)
        except TelegramError:
            raise
        except Exception as exc:
            raise TelegramError("download", 0, str(exc)) from exc
        ext = os.path.splitext(remote)[1] or mimetypes.guess_extension(
            media.get("mime_type") or ""
        ) or ".bin"
        fd, path = tempfile.mkstemp(dir=self.tmp_dir, suffix=ext)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return path

    def _send_media(self, chat_id, path, media_type, header, body):
        caption = header
        if body:
            room = CAPTION_LIMIT - len(header) - 4
            if room > 0:
                caption += "\n\n" + body[:room]
        senders = {
            "photo": self.api.send_photo,
            "video": self.api.send_video,
            "animation": self.api.send_animation,
            "voice": self.api.send_voice,
            "audio": self.api.send_audio,
        }
        if media_type == "video_note":
            # кружок: документом, чтобы не потерять подпись и источник
            self._call(self.api.send_document, chat_id, path, caption)
        elif media_type in senders:
            self._call(senders[media_type], chat_id, path, caption)
        else:
            self._call(self.api.send_document, chat_id, path, caption)

    def _unlink(self, path):
        try:
            os.remove(path)
        except OSError:
            pass

    # ------------------------------------------------------------------- text
    def _header(self, chat, user, msg):
        title = display_name(chat) or str(chat.get("id"))
        parts = [f"📥 <b>{html.escape(title)}</b>"]
        sender = display_name(user)
        if sender and sender != title:
            parts.append(f"👤 {html.escape(sender)}")
        stamps = time.strftime("%d.%m %H:%M", time.localtime(msg.get("date") or time.time()))
        parts.append(stamps)
        if msg.get("edit_date"):
            parts.append("✏️ изменено")
        return " · ".join(parts)

    def _compose(self, header, text, msg):
        if text:
            return f"{header}\n\n{html.escape(text)}"
        kind = "service"
        for key in ("poll", "location", "contact", "venue", "dice"):
            if msg.get(key):
                kind = key
                break
        return f"{header}\n\n<i>[{kind}]</i>"

    def _send_text(self, chat_id, text):
        for chunk in self._split(text):
            self._call(self.api.send_message, chat_id, chunk)

    def _split(self, text):
        if len(text) <= TEXT_LIMIT:
            return [text]
        chunks, current = [], ""
        for line in text.split("\n"):
            while len(line) > TEXT_LIMIT:
                chunks.append(line[:TEXT_LIMIT])
                line = line[TEXT_LIMIT:]
            if len(current) + len(line) + 1 > TEXT_LIMIT:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)
        return chunks

    # ------------------------------------------------------------------ retry
    def _call(self, fn, *args, **kwargs):
        delay = 1
        last = None
        for _ in range(6):
            try:
                return fn(*args, **kwargs)
            except TelegramError as exc:
                last = exc
                if exc.code == 429:
                    wait = int(exc.parameters.get("retry_after", 5))
                    log(f"flood wait {wait}s")
                    time.sleep(wait)
                    continue
                if exc.code == 0 or 500 <= exc.code < 600:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise
        raise last


def main():
    parser = argparse.ArgumentParser(description="telegram relay bot")
    parser.add_argument("--token", default=os.environ.get("BOT_TOKEN"))
    parser.add_argument("--owner", default=os.environ.get("OWNER_ID"))
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "data"))
    parser.add_argument("--api-base", default=os.environ.get("API_BASE", "https://api.telegram.org"))
    parser.add_argument("--max-download-mb", type=int, default=20)
    args = parser.parse_args()

    if not args.token:
        parser.error("BOT_TOKEN is required")

    os.makedirs(args.data_dir, exist_ok=True)
    store = Store(os.path.join(args.data_dir, "relay.db"))
    api = BotAPI(args.token, args.api_base)
    bot = RelayBot(
        api,
        store,
        args.data_dir,
        owner_id=args.owner,
        max_download=args.max_download_mb * 1024 * 1024,
    )
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    bot.run(stop_event=stop)


if __name__ == "__main__":
    main()
