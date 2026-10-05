"""storage/db.py — подключение к SQLite, WAL, пул соединений, утилиты."""

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

# Глобальный пул соединений (один на поток, thread-local)
_thread_local = threading.local()
# Глубина вложенной transaction() — чтобы коммит делал только внешний блок.
_tx_local = threading.local()


def get_db_path() -> Path:
    """Путь к файлу БД.
    На Railway: использует RAILWAY_VOLUME_MOUNT_PATH (путь монтирования тома) + подпапка db/jauap.db.
    Локально: JAUAP_DB_PATH или /data/jauap.db.
    """
    # Приоритет: явный env -> Railway volume mount path -> дефолт /data
    if explicit := os.getenv("JAUAP_DB_PATH"):
        path = Path(explicit)
    elif railway_mount := os.getenv("RAILWAY_VOLUME_MOUNT_PATH"):
        path = Path(railway_mount) / "db" / "jauap.db"
    else:
        path = Path("/data/jauap.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Настройки соединения: WAL, FK, busy_timeout, row_factory."""
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")  # 5 сек ожидание блокировки
    conn.row_factory = sqlite3.Row


def get_connection() -> sqlite3.Connection:
    """
    Получить соединение для текущего потока.
    Соединение создаётся лениво и кешируется в thread-local.
    """
    if not hasattr(_thread_local, "conn") or _thread_local.conn is None:
        path = get_db_path()
        conn = sqlite3.connect(str(path), check_same_thread=False)
        _configure_connection(conn)
        _thread_local.conn = conn
    return _thread_local.conn


def close_connection() -> None:
    """Закрыть соединение текущего потока (для тестов / graceful shutdown)."""
    if hasattr(_thread_local, "conn") and _thread_local.conn is not None:
        _thread_local.conn.close()
        _thread_local.conn = None


@contextmanager
def transaction():
    """
    Контекстный менеджер транзакции.
    Использование:
        with transaction() as conn:
            conn.execute(...)
    При исключении — rollback, иначе — commit.

    Вложенность поддерживается: коммит делает только внешний блок.
    """
    conn = get_connection()
    depth = getattr(_tx_local, "depth", 0)
    _tx_local.depth = depth + 1
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        if depth == 0:
            conn.commit()
    finally:
        _tx_local.depth = depth


def _is_write(sql: str) -> bool:
    """Запрос меняет данные (нужен commit)."""
    head = sql.lstrip()[:8].upper()
    return head.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """Выполнить запрос.

    Запросы, меняющие данные, коммитятся сразу — иначе транзакция остаётся
    висеть (следующая запись получает «database is locked», а при рестарте
    данные теряются). Внутри transaction() коммит делает внешний блок, так
    что несколько execute() остаются одной атомарной операцией.
    """
    conn = get_connection()
    cursor = conn.execute(sql, params)
    if getattr(_tx_local, "depth", 0) == 0 and _is_write(sql):
        conn.commit()
    return cursor


def executemany(sql: str, params_list: list[tuple]) -> sqlite3.Cursor:
    """Выполнить много запросов одной транзакцией."""
    conn = get_connection()
    cursor = conn.executemany(sql, params_list)
    if getattr(_tx_local, "depth", 0) == 0 and _is_write(sql):
        conn.commit()
    return cursor


def fetchone(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    """Получить одну строку."""
    return execute(sql, params).fetchone()


def fetchall(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Получить все строки."""
    return execute(sql, params).fetchall()


def init_db() -> None:
    """Инициализация БД: создать файл, запустить миграции."""
    from storage.schema import apply_migrations
    apply_migrations()


def vacuum() -> None:
    """VACUUM для реорганизации БД (вызывать редко)."""
    conn = get_connection()
    conn.execute("VACUUM;")


def backup(backup_path: Path) -> None:
    """
    Создать бэкап БД через SQLite Backup API (онлайн, не блокирует записи).
    """
    src = get_connection()
    dst = sqlite3.connect(str(backup_path))
    try:
        src.backup(dst)
    finally:
        dst.close()