"""Minimal dependency-free Telegram Bot API client (long polling + uploads)."""

import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid


class TelegramError(Exception):
    def __init__(self, method, code, description, parameters=None):
        super().__init__(f"{method}: [{code}] {description}")
        self.method = method
        self.code = code
        self.description = description
        self.parameters = parameters or {}


class BotAPI:
    def __init__(self, token, api_base="https://api.telegram.org", request_timeout=90):
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.request_timeout = request_timeout
        self._method_url = f"{self.api_base}/bot{token}/"

    # ------------------------------------------------------------------ core
    def _post(self, method, body, content_type, timeout=None):
        req = urllib.request.Request(
            self._method_url + method,
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.request_timeout) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode())
            except Exception:
                raise TelegramError(method, exc.code, exc.reason) from exc
        except urllib.error.URLError as exc:
            raise TelegramError(method, 0, f"network error: {exc.reason}") from exc
        if not payload.get("ok"):
            raise TelegramError(
                method,
                payload.get("error_code", 0),
                payload.get("description", "unknown error"),
                payload.get("parameters"),
            )
        return payload["result"]

    def call(self, method, params=None, files=None, timeout=None):
        params = params or {}
        if files is None:
            body = json.dumps(params).encode()
            return self._post(method, body, "application/json", timeout)
        body = self._encode_multipart(params, files)
        return self._post(method, body, body.content_type, timeout)

    class _Multipart(bytes):
        content_type = ""

    def _encode_multipart(self, params, files):
        boundary = "----tgbot" + uuid.uuid4().hex
        out = bytearray()
        for key, value in params.items():
            if value is None:
                continue
            out += f"--{boundary}\r\n".encode()
            out += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
            out += str(value).encode() + b"\r\n"
        for key, (filename, data, ctype) in files.items():
            out += f"--{boundary}\r\n".encode()
            out += (
                f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
            ).encode()
            out += f"Content-Type: {ctype or 'application/octet-stream'}\r\n\r\n".encode()
            out += data + b"\r\n"
        out += f"--{boundary}--\r\n".encode()
        result = self._Multipart(out)
        result.content_type = f"multipart/form-data; boundary={boundary}"
        return result

    # ------------------------------------------------------------- updating
    def get_updates(self, offset=None, timeout=30, allowed_updates=None):
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates:
            params["allowed_updates"] = allowed_updates
        return self.call("getUpdates", params, timeout=timeout + 30)

    def get_me(self):
        return self.call("getMe")

    def get_file(self, file_id):
        return self.call("getFile", {"file_id": file_id})

    def download(self, file_path):
        url = f"{self.api_base}/file/bot{self.token}/{file_path}"
        with urllib.request.urlopen(url, timeout=self.request_timeout) as resp:
            return resp.read()

    def get_chat_member(self, chat_id, user_id):
        return self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    # ------------------------------------------------------------- messaging
    def send_message(self, chat_id, text, **kwargs):
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return self.call("sendMessage", params)

    def edit_message_text(self, chat_id, message_id, text):
        return self.call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"},
        )

    def delete_message(self, chat_id, message_id):
        return self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def _send_file(self, method, field, chat_id, path, filename=None, extra=None):
        filename = filename or os.path.basename(path)
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            data = fh.read()
        params = {"chat_id": chat_id}
        params.update(extra or {})
        return self.call(method, params, {field: (filename, data, ctype)}, timeout=300)

    def send_document(self, chat_id, path, caption=None):
        return self._send_file(
            "sendDocument", "document", chat_id, path, extra={"caption": caption}
        )

    def send_photo(self, chat_id, path, caption=None):
        return self._send_file("sendPhoto", "photo", chat_id, path, extra={"caption": caption})

    def send_video(self, chat_id, path, caption=None):
        return self._send_file("sendVideo", "video", chat_id, path, extra={"caption": caption})

    def send_audio(self, chat_id, path, caption=None):
        return self._send_file("sendAudio", "audio", chat_id, path, extra={"caption": caption})

    def send_voice(self, chat_id, path, caption=None):
        return self._send_file("sendVoice", "voice", chat_id, path, extra={"caption": caption})

    def send_video_note(self, chat_id, path):
        return self._send_file("sendVideoNote", "video_note", chat_id, path)

    def send_animation(self, chat_id, path, caption=None):
        return self._send_file(
            "sendAnimation", "animation", chat_id, path, extra={"caption": caption}
        )
