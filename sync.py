"""Шифрованный снимок базы и синхронизация с репозиторием.

Репозиторий публичный, поэтому открытая база туда не попадает: перед отправкой
SQLite шифруется ключом из переменной окружения DB_KEY (Fernet).
Если ключа нет, синхронизация просто не включается — бот работает локально.
"""

import logging
import os
import subprocess
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

import store

log = logging.getLogger("gamma.sync")

SNAPSHOT = Path(os.getenv("DB_SNAPSHOT", "data/bot.db.enc"))


def _fernet() -> Fernet | None:
    key = os.getenv("DB_KEY", "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        log.warning("DB_KEY не похож на ключ Fernet — синхронизация выключена")
        return None


def restore() -> bool:
    f = _fernet()
    if f is None or not SNAPSHOT.exists():
        return False
    if store.DB_PATH.exists():
        return False
    try:
        data = f.decrypt(SNAPSHOT.read_bytes())
    except InvalidToken:
        log.error("Снимок базы не расшифровался: ключ не тот")
        return False
    store.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    store.DB_PATH.write_bytes(data)
    log.info("База восстановлена из снимка: %d байт", len(data))
    return True


def dump() -> bool:
    f = _fernet()
    if f is None or not store.DB_PATH.exists():
        return False
    store.save_touch()
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_bytes(f.encrypt(store.DB_PATH.read_bytes()))
    return True


def _git(*args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", *args], capture_output=True, text=True, timeout=120)
    except Exception as exc:
        log.warning("git %s не выполнился: %s", args[0], type(exc).__name__)
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def push() -> bool:
    if not dump():
        return False

    _git("config", "user.name", "gamma-bot")
    _git("config", "user.email", "bot@users.noreply.github.com")
    _git("add", str(SNAPSHOT))
    rc, out = _git("status", "--porcelain", str(SNAPSHOT))
    if rc != 0 or not out:
        return True

    _git("pull", "--rebase", "--autostash", "--quiet")
    rc, _ = _git("commit", "--quiet", "-m", "state: база бота [skip ci]")
    if rc != 0:
        return False
    rc, out = _git("push", "--quiet")
    if rc != 0:
        log.warning("Не удалось отправить базу в репозиторий: %s", out[:200])
        return False
    log.info("База сохранена в репозитории")
    return True
