#!/usr/bin/env python3
"""
Миграция management_token: plaintext -> SHA-256 с префиксом sha256:.
- Делает копию clients/ в clients/.migrate_backup_<timestamp>/
- Хэширует токены через hash_token() (SHA-256 hex с префиксом sha256:)
- Пишет через ту же логику, что PUT /admin/clients/{pid} (_atomic_write)
- Идемпотентно: уже с префиксом sha256: не трогает.
- Логирует изменения в stdout.
"""

import hashlib
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from admin.api import _atomic_write, hash_token, TOKEN_HASH_PREFIX

CLIENTS_DIR = Path(__file__).resolve().parent.parent / "clients"
BACKUP_DIR = CLIENTS_DIR / f".migrate_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def is_hashed_token(token: str) -> bool:
    """Токен уже имеет префикс sha256:."""
    return token.startswith("sha256:")


def migrate_tokens(dry_run: bool = False) -> dict:
    """Миграция токенов в clients/.
    Возвращает статистику: {total, migrated, skipped, errors}.
    """
    stats = {"total": 0, "migrated": 0, "skipped": 0, "errors": []}

    if not CLIENTS_DIR.exists():
        stats["errors"].append(f"Папка {CLIENTS_DIR} не существует")
        return stats

    # 1. Создаём копию папки
    if not dry_run:
        try:
            shutil.copytree(CLIENTS_DIR, BACKUP_DIR, ignore=shutil.ignore_patterns("*.bak", "__pycache__"))
            print(f"[BACKUP] Создана копия: {BACKUP_DIR}")
        except Exception as e:
            stats["errors"].append(f"Не удалось создать бэкап: {e}")
            return stats

    # 2. Проходим по файлам
    for name in sorted(os.listdir(CLIENTS_DIR)):
        if not name.lower().endswith((".yaml", ".yml")) or name.startswith("_"):
            continue
        if name in (BACKUP_DIR.name,):  # не заходим в свою же папку бэкапа
            continue

        path = CLIENTS_DIR / name
        stats["total"] += 1

        try:
            with open(path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as e:
            stats["errors"].append(f"{name}: нечитаемый YAML — {e}")
            continue

        token = str(cfg.get("management_token") or "").strip()
        if not token:
            stats["skipped"] += 1
            continue

        if is_hashed_token(token):
            stats["skipped"] += 1
            continue

        # Хэшируем
        hashed = "sha256:" + hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
        cfg["management_token"] = hashed

        # Пишем атомарно (как в PUT /admin/clients/{pid})
        if not dry_run:
            try:
                # Используем _atomic_write из admin.api
                from admin.api import _atomic_write
                _atomic_write(path, cfg)
                print(f"[MIGRATED] {name}: management_token -> sha256:<hex>")
                stats["migrated"] += 1
            except Exception as e:
                stats["errors"].append(f"{name}: ошибка записи — {e}")
        else:
            print(f"[DRY-RUN] {name}: будет замигрирован")
            stats["migrated"] += 1

    return stats


if __name__ == "__main__":
    import argparse
    from datetime import datetime

    parser = argparse.ArgumentParser(description="Миграция management_token в SHA-256 с префиксом sha256:")
    parser.add_argument("--dry-run", action="store_true", help="Только показать что будет сделано")
    args = parser.parse_args()

    print(f"=== Миграция management_token ===")
    print(f"Папка клиентов: {CLIENTS_DIR}")
    print(f"Dry-run: {args.dry_run}")
    print()

    result = migrate_tokens(dry_run=args.dry_run)

    print()
    print(f"=== Итог ===")
    print(f"Всего файлов: {result['total']}")
    print(f"Замигрировано: {result['migrated']}")
    print(f"Пропущено (уже с префиксом sha256:/пусто): {result['skipped']}")
    if result["errors"]:
        print(f"Ошибки: {len(result['errors'])}")
        for e in result["errors"]:
            print(f"  - {e}")

    if result["errors"]:
        sys.exit(1)
    print("Готово.")