#!/usr/bin/env python3
"""Create an online SQLite backup and prune old local copies."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    data_dir = Path(os.environ.get("HELIOS_DATA_DIR", "/var/lib/helios")).resolve()
    database = Path(
        os.environ.get("HELIOS_DATABASE_PATH", str(data_dir / "helios.db"))
    ).resolve()
    backup_dir = (data_dir / "backups").resolve()
    if data_dir not in backup_dir.parents:
        raise RuntimeError("Backup path escaped HELIOS_DATA_DIR")
    backup_dir.mkdir(parents=True, exist_ok=True)
    if not database.exists():
        print(f"database not present yet: {database}")
        return 0

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / f"helios-{timestamp}.db"
    with sqlite3.connect(database) as source, sqlite3.connect(destination) as target:
        source.backup(target)
        row = target.execute("PRAGMA integrity_check").fetchone()
        if row is None or row[0] != "ok":
            raise RuntimeError(f"Backup integrity check failed: {row}")

    keep = int(os.environ.get("HELIOS_LOCAL_BACKUPS_TO_KEEP", "14"))
    backups = sorted(backup_dir.glob("helios-*.db"), key=lambda item: item.stat().st_mtime)
    for old_backup in backups[: max(0, len(backups) - keep)]:
        old_backup.unlink()
    print(f"created {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
