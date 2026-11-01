"""Локальное хранилище сессии для CLI и MCP-сервера.

Сессия (cookies + токены Пульса) хранится в зашифрованном виде через
:class:`SessionCrypto` в ``$PYMIREA_HOME`` (по умолчанию
``$XDG_CONFIG_HOME/pymirea`` или ``~/.config/pymirea``):

* ``key`` — случайный HKDF-сид, создаётся при первом запуске (права 0600);
  можно не хранить на диске, а передать через ``PYMIREA_SESSION_KEYS``;
* ``session.json`` — ``{"login", "saved_at", "blob"}``, где ``blob`` —
  Fernet-токен с cookies.

Пароль никогда не сохраняется.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from . import _settings
from .config import Config
from .crypto import SessionCrypto


class NotLoggedIn(RuntimeError):
    """Локальной сессии нет или она не расшифровывается."""


def default_home() -> Path:
    env = os.environ.get("PYMIREA_HOME")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "pymirea"


def _write_private(path: Path, data: str) -> None:
    """Атомарно записать файл, доступный только текущему пользователю."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


@dataclass
class StoredSession:
    login: str | None
    saved_at: float | None
    cookies: dict


class SessionStore:
    def __init__(self, home: Path | str | None = None) -> None:
        self.home = Path(home).expanduser() if home else default_home()
        self.key_path = self.home / "key"
        self.session_path = self.home / "session.json"

    # --- keys / config ---

    def _ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.home, 0o700)
        except OSError:
            pass

    def session_keys(self) -> str:
        env = os.environ.get("PYMIREA_SESSION_KEYS")
        if env:
            return env
        if self.key_path.exists():
            return self.key_path.read_text(encoding="utf-8").strip()
        self._ensure_home()
        key = base64.b64encode(secrets.token_bytes(32)).decode()
        _write_private(self.key_path, key + "\n")
        return key

    def configure(self) -> None:
        """Сконфигурировать pymirea, если хост-приложение ещё этого не сделало."""
        if _settings._cfg is not None:
            return
        _settings.configure(
            Config(
                session_keys=self.session_keys(),
                mirea_proxy=os.environ.get("MIREA_PROXY") or None,
            )
        )

    def _crypto(self) -> SessionCrypto:
        return SessionCrypto(self.session_keys())

    # --- session ---

    def exists(self) -> bool:
        return self.session_path.exists()

    def load(self) -> StoredSession:
        if not self.session_path.exists():
            raise NotLoggedIn("Нет сохранённой сессии. Выполните `pymirea login`.")
        try:
            raw = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise NotLoggedIn(f"Файл сессии повреждён ({e}). Выполните `pymirea login`.") from e
        cookies = self._crypto().decrypt_session(raw.get("blob") or "")
        if not cookies:
            raise NotLoggedIn("Не удалось расшифровать сессию (сменился ключ?). Выполните `pymirea login`.")
        return StoredSession(login=raw.get("login"), saved_at=raw.get("saved_at"), cookies=cookies)

    def save(self, cookies: dict, *, login: str | None = None) -> None:
        self._ensure_home()
        if login is None and self.session_path.exists():
            try:
                login = json.loads(self.session_path.read_text(encoding="utf-8")).get("login")
            except (OSError, ValueError):
                login = None
        payload = {
            "login": login,
            "saved_at": time.time(),
            "blob": self._crypto().encrypt_session(cookies),
        }
        _write_private(self.session_path, json.dumps(payload, ensure_ascii=False))

    def clear(self) -> bool:
        if self.session_path.exists():
            self.session_path.unlink()
            return True
        return False


__all__ = ["NotLoggedIn", "SessionStore", "StoredSession", "default_home"]
