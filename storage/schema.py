"""storage/schema.py — управление миграциями БД."""

import os
import sqlite3
from pathlib import Path

from storage.db import get_connection, transaction


MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _get_applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Получить номера уже применённых миграций."""
    try:
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        return {row["version"] for row in rows}
    except sqlite3.OperationalError:
        # Таблица schema_version ещё не существует
        return set()


def _get_migration_files() -> list[tuple[int, str, Path]]:
    """
    Найти все файлы миграций в папке migrations/.
    Формат имени: NNN_name.sql или NNN_name.py
    Возвращает список (version, name, path), отсортированный по version.
    """
    migrations = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        # Имя файла: 001_init.sql -> version=1, name="init"
        stem = path.stem
        parts = stem.split("_", 1)
        if len(parts) != 2:
            continue
        try:
            version = int(parts[0])
        except ValueError:
            continue
        name = parts[1]
        migrations.append((version, name, path))
    # Также поддерживаем .py миграции (для редких data migration)
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        stem = path.stem
        parts = stem.split("_", 1)
        if len(parts) != 2:
            continue
        try:
            version = int(parts[0])
        except ValueError:
            continue
        name = parts[1]
        migrations.append((version, name, path))
    return sorted(migrations, key=lambda x: x[0])


def apply_migrations() -> None:
    """
    Применить все невыполненные миграции в порядке версий.
    Каждая миграция выполняется в отдельной транзакции.
    Перед применением новых миграций делает копию БД (если включено).
    """
    conn = get_connection()
    applied = _get_applied_versions(conn)
    migrations = _get_migration_files()

    # Опциональный бэкап перед миграциями (если задана переменная)
    if os.getenv("JAUAP_BACKUP_BEFORE_MIGRATE", "1") == "1":
        from storage.db import backup, get_db_path
        from datetime import datetime
        backup_path = get_db_path().parent / f"backups/pre_migrate_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            backup(backup_path)
            print(f"[migrations] Backup created: {backup_path}")
        except Exception as e:
            print(f"[migrations] Warning: backup failed: {e}")

    for version, name, path in migrations:
        if version in applied:
            continue

        print(f"[migrations] Applying {version}: {name}...")
        try:
            with transaction() as tx:
                if path.suffix == ".sql":
                    sql = path.read_text(encoding="utf-8")
                    # Выполняем все statements в скрипте
                    tx.executescript(sql)
                elif path.suffix == ".py":
                    # Python миграция: ожидает функцию run(conn)
                    import importlib.util
                    spec = importlib.util.spec_from_file_location(f"migration_{version}", path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    if hasattr(module, "run"):
                        module.run(tx)
                    else:
                        raise RuntimeError(f"Migration {path} has no run(conn) function")
                else:
                    continue

                # Записать версию в schema_version
                tx.execute(
                    "INSERT INTO schema_version (version, name) VALUES (?, ?)",
                    (version, name),
                )
            print(f"[migrations] Applied {version}: {name} OK")
        except Exception as e:
            print(f"[migrations] FAILED {version}: {name}: {e}")
            raise

    print(f"[migrations] All migrations applied. Current version: {max(applied) if applied else 0}")


def get_current_version() -> int:
    """Текущая версия схемы (максимальный номер применённой миграции)."""
    conn = get_connection()
    applied = _get_applied_versions(conn)
    return max(applied) if applied else 0


def create_schema_version_table() -> None:
    """Создать таблицу schema_version если её нет (вызывается при первом запуске)."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()