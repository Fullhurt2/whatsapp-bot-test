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
    pending = [(v, n, p) for v, n, p in migrations if v not in applied]
    if not pending:
        return

    # Опциональный бэкап перед миграциями (только если есть что накатывать)
    if os.getenv("JAUAP_BACKUP_BEFORE_MIGRATE", "1") == "1":
        from storage.db import backup, get_db_path
        from datetime import datetime
        backup_dir = get_db_path().parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"pre_migrate_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        try:
            backup(backup_path)
            print(f"[migrations] Backup created: {backup_path}")
            # Ротация: храним 5 последних pre_migrate бэкапов
            old_backups = sorted(backup_dir.glob("pre_migrate_*.db"), key=lambda p: p.stat().st_mtime)
            while len(old_backups) > 5:
                old_backups.pop(0).unlink(missing_ok=True)
        except Exception as e:
            print(f"[migrations] Warning: backup failed: {e}")

    for version, name, path in pending:
        print(f"[migrations] Applying {version}: {name}...")
        try:
            with transaction() as tx:
                if path.suffix == ".sql":
                    sql = path.read_text(encoding="utf-8")
                    # Выполняем statements по одному внутри tx (без неявного COMMIT от executescript)
                    statements = [s.strip() for s in sql.split(";") if s.strip()]
                    for stmt in statements:
                        tx.execute(stmt)
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
                applied.add(version)
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