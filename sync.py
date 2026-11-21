"""Постоянная база бота: шифрованный снимок в отдельной ветке репозитория.

Открытая SQLite в репозиторий не попадает — перед отправкой файл шифруется
ключом DB_KEY (Fernet). Снимок живёт в ветке `state`, которая каждый раз
перезаписывается одним коммитом, поэтому история не растёт, а данные
сохраняются без потерь (каждые DB_SYNC_SECONDS секунд).
"""

import logging
import os
import subprocess
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

import store

log = logging.getLogger("gamma.sync")

STATE_BRANCH = os.getenv("DB_BRANCH", "state")
STATE_NAME = "bot.db.enc"
SNAPSHOT = Path(os.getenv("DB_SNAPSHOT", f"data/{STATE_NAME}"))
SYNC_SECONDS = int(os.getenv("DB_SYNC_SECONDS", "30"))


def _fernet() -> Fernet | None:
    key = os.getenv("DB_KEY", "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        log.warning("DB_KEY не похож на ключ Fernet — синхронизация выключена")
        return None


def _run(args: list[str], text_input: str | bytes | None = None, text: bool = True):
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=text,
            input=text_input,
            timeout=120,
        )
    except Exception as exc:
        log.warning("git %s не выполнился: %s", args[0], type(exc).__name__)
        return None


def _git(*args: str) -> tuple[int, str]:
    proc = _run(list(args))
    if proc is None:
        return 1, "git недоступен"
    out = proc.stdout if isinstance(proc.stdout, str) else ""
    return proc.returncode, (out + (proc.stderr or "")).strip()


def restore() -> bool:
    if store._use_turso():
        return False
    f = _fernet()
    if f is None:
        return False
    if store.DB_PATH.exists():
        return False

    rc, _ = _git("fetch", "--depth=1", "origin", STATE_BRANCH)
    if rc != 0:
        return False

    proc = _run(["show", f"origin/{STATE_BRANCH}:{STATE_NAME}"], text=False)
    if proc is None or proc.returncode != 0 or not isinstance(proc.stdout, bytes):
        return False

    try:
        data = f.decrypt(proc.stdout)
    except InvalidToken:
        log.error("Снимок базы не расшифровался: ключ не тот")
        return False

    store.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    store.DB_PATH.write_bytes(data)
    log.info("База восстановлена из ветки %s: %d байт", STATE_BRANCH, len(data))
    return True


def dump() -> bool:
    f = _fernet()
    if f is None or not store.DB_PATH.exists():
        return False
    store.save_touch()
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_bytes(f.encrypt(store.DB_PATH.read_bytes()))
    return True


def push() -> bool:
    if store._use_turso():
        return False
    if not dump():
        return False

    _git("config", "user.name", "gamma-bot")
    _git("config", "user.email", "bot@users.noreply.github.com")

    blob = _run(["hash-object", "-w", str(SNAPSHOT)])
    if blob is None or blob.returncode != 0:
        log.warning("git hash-object не сработал")
        return False

    entry = f"100644 blob {blob.stdout.strip()}\t{STATE_NAME}\n".encode()
    tree = _run(["mktree"], text_input=entry, text=False)
    if tree is None or tree.returncode != 0 or not isinstance(tree.stdout, bytes):
        log.warning("git mktree не сработал")
        return False

    commit = _run(["commit-tree", tree.stdout.decode().strip(), "-m", "state: база бота"])
    if commit is None or commit.returncode != 0:
        log.warning("git commit-tree не сработал")
        return False

    rc, out = _git("push", "--force", "origin", f"{commit.stdout.strip()}:refs/heads/{STATE_BRANCH}")
    if rc != 0:
        log.warning("Не удалось отправить базу: %s", out[:200])
        return False
    log.info("База сохранена в ветке %s", STATE_BRANCH)
    return True
