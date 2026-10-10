#!/usr/bin/env python3
"""Publish complete, checksummed Helios recovery archives without credentials."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
import tarfile
import tempfile
import uuid

FORMAT_VERSION = 1
CHUNK = 1024 * 1024
REPO = Path(__file__).resolve().parents[1]


class BackupError(Exception):
    pass


def absolute(path: str | Path) -> Path:
    # resolve() would hide symlinks before the confinement check.
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise BackupError("Unsafe archive or artifact path")
    if any(part in ("", ".", "..") for part in value.split("/")) or PurePosixPath(value).is_absolute():
        raise BackupError("Unsafe archive or artifact path")
    return value


def no_symlinks(path: Path) -> None:
    for parent in reversed((path, *path.parents)):
        if parent.is_symlink():
            raise BackupError("Symlink paths are not permitted")


@contextmanager
def directory_fd(path: Path):
    """Walk every component with O_NOFOLLOW, including the configured root."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise BackupError("Secure backup and restore require POSIX O_NOFOLLOW support")
    path = absolute(path)
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def source_file(root: Path, relative: str):
    parts = safe_relative(relative).split("/")
    with directory_fd(root.joinpath(*parts[:-1])) as parent:
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise BackupError("Only regular files may be backed up")
            yield source


def fingerprint(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def checksum(path: Path) -> dict:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(CHUNK), b""):
            length += len(block)
            digest.update(block)
    return {"size": length, "sha256": digest.hexdigest()}


def private_directory(path: Path) -> None:
    path = absolute(path)
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    finally:
        os.close(descriptor)


def private_write(path: Path, content: bytes) -> None:
    private_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(content)


def copy_checked(root: Path, relative: str, target: Path, expected: str | None = None) -> None:
    private_directory(target.parent)
    with source_file(root, relative) as source:
        before = fingerprint(os.fstat(source.fileno()))
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "wb") as destination:
            for block in iter(lambda: source.read(CHUNK), b""):
                destination.write(block)
                digest.update(block)
        if before != fingerprint(os.fstat(source.fileno())):
            raise BackupError("A source file changed during backup; retry after writers finish")
        if expected is not None and digest.hexdigest() != expected:
            raise BackupError("Artifact checksum differs from the database snapshot")


def credential_name(name: str) -> bool:
    lower = name.lower()
    return (
        lower.startswith(".")
        or lower.startswith(("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"))
        or lower.endswith((".env", ".key", ".pem", ".p12", ".pfx", ".kdbx", ".keystore"))
        or bool(re.search(r"(?:^|[-_.])(secrets?|credentials?|api[-_]?keys?|access[-_]?tokens?|private[-_]?keys?)(?:$|[-_.])", lower))
    )


def memory_inventory(root: Path) -> tuple[dict, list]:
    found, excluded = {}, []

    def walk(directory: Path, relative: str = "") -> None:
        with directory_fd(directory) as descriptor:
            for name in sorted(os.listdir(descriptor)):
                member = f"{relative}/{name}" if relative else name
                safe_relative(member)
                if credential_name(name):
                    excluded.append({"path": "memory/" + member, "reason": "credential or hidden path"})
                    continue
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    walk(directory / name, member)
                elif stat.S_ISREG(info.st_mode):
                    found[member] = fingerprint(info)
                else:
                    raise BackupError("Canonical memory contains a symlink or non-regular file")

    walk(root)
    return found, excluded


@contextmanager
def benchmark_guard(root: Path):
    import fcntl

    with directory_fd(root) as parent:
        descriptor = os.open("benchmark_registry.json.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise BackupError("Benchmark lock is not a regular file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackupError("Benchmark refresh is active; retry backup after it finishes") from exc
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def check_database(database: Path) -> dict:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA trusted_schema = OFF")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("SQLite integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("SQLite foreign key check failed")
        schema = db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        if not {"projects", "tasks", "artifacts"}.issubset({row[1] for row in schema if row[0] == "table"}):
            raise BackupError("Database is not a Helios project store")
        return {
            "user_version": db.execute("PRAGMA user_version").fetchone()[0],
            "application_id": db.execute("PRAGMA application_id").fetchone()[0],
            "sha256": hashlib.sha256(json.dumps(schema, separators=(",", ":")).encode()).hexdigest(),
        }


def artifact_rows(database: Path) -> list:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA trusted_schema = OFF")
        return db.execute("SELECT id, path, checksum_sha256 FROM artifacts ORDER BY id").fetchall()


def artifact_relative(path: str, root: Path) -> str:
    if Path(path).is_absolute():
        # Archive the original DB; restore alone rewrites legacy absolute paths.
        try:
            path = str(Path(path).relative_to(root))
        except ValueError as exc:
            raise BackupError("Artifact path escaped its configured root") from exc
    relative = safe_relative(path)
    if any(credential_name(part) for part in relative.split("/")):
        raise BackupError("A referenced artifact has a credential or hidden path")
    return relative


def snapshot_database(source: Path, target: Path) -> None:
    no_symlinks(source)
    with source_file(source.parent, source.name) as descriptor:
        before = os.fstat(descriptor.fileno())
        private_write(target, b"")
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live:
            after = source.stat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise BackupError("Database changed identity during backup")
            with closing(sqlite3.connect(target)) as snapshot:
                live.backup(snapshot)
                snapshot.execute("PRAGMA journal_mode = DELETE")
    check_database(target)


def release_metadata() -> tuple[dict, dict]:
    package = json.loads((REPO / "package.json").read_text())
    dependencies = {}
    for name, version in package.get("dependencies", {}).items():
        if re.fullmatch(r"[@A-Za-z0-9/_.-]+", name):
            dependencies[name] = version if isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z.+*^~<>=| -]+", version) else "non-registry specification omitted"
    version = str(package.get("version", "unknown"))
    if not re.fullmatch(r"[A-Za-z0-9.+_-]+", version):
        version = "unknown"
    return {"version": version}, {
        "python": ".".join(map(str, sys.version_info[:3])),
        "sqlite": sqlite3.sqlite_version,
        "node_requirement": ">=22",
        "node_packages": dependencies,
        "package_lock_sha256": checksum(REPO / "package-lock.json")["sha256"] if (REPO / "package-lock.json").is_file() else None,
    }


def capture_components(stage: Path, options) -> dict:
    manifest = {"format_version": FORMAT_VERSION, "created_at": datetime.now(timezone.utc).isoformat(), "files": {}, "components": {}, "excluded": [], "warnings": []}
    snapshot_database(options.database, stage / "helios.db")
    manifest["schema"] = check_database(stage / "helios.db")
    manifest["release"], manifest["dependencies"] = release_metadata()
    manifest["configuration"] = {
        "source_roots": {"database": str(options.database), "artifacts": str(options.artifact_root), "runtime": str(options.state_dir), "memory": str(options.memory_root) if options.memory_root else None},
        "retention_per_format": options.keep,
        "require_memory": options.require_memory,
    }
    manifest["consistency"] = {
        "database": "SQLite online snapshot",
        "artifacts": "All snapshot references verified against their database checksums",
        "benchmarks": "Registry and history captured under the refresh process lock",
        "memory": "Stable inventory and file metadata; independent from the SQLite transaction",
    }
    for _, path, expected in artifact_rows(stage / "helios.db"):
        relative = artifact_relative(path, options.artifact_root)
        target = stage / "artifacts" / relative
        if target.exists():
            if checksum(target)["sha256"] != expected:
                raise BackupError("Conflicting artifact checksums in database")
        else:
            copy_checked(options.artifact_root, relative, target, expected)
    manifest["components"]["artifacts"] = {"status": "included"}
    if options.state_dir.exists():
        with benchmark_guard(options.state_dir):
            registry = options.state_dir / "benchmark_registry.json"
            if registry.exists() or registry.is_symlink():
                copy_checked(options.state_dir, registry.name, stage / "runtime" / registry.name)
            history = options.state_dir / "benchmark_history"
            if history.exists() or history.is_symlink():
                with directory_fd(history) as descriptor:
                    for name in sorted(os.listdir(descriptor)):
                        if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z\.json", name):
                            copy_checked(options.state_dir, "benchmark_history/" + name, stage / "runtime" / "benchmark_history" / name)
                        else:
                            manifest["excluded"].append({"path": "runtime/benchmark_history/" + name, "reason": "not a benchmark history snapshot"})
            manifest["components"]["benchmarks"] = {"status": "included" if registry.exists() else "unavailable"}
    else:
        manifest["components"]["benchmarks"] = {"status": "unavailable"}
    if manifest["components"]["benchmarks"]["status"] == "unavailable":
        manifest["warnings"].append("Benchmark registry is unavailable")
    if options.memory_root is None:
        manifest["components"]["memory"] = {"status": "unconfigured"}
        manifest["warnings"].append("Canonical memory root is not configured")
    else:
        try:
            inventory, excluded = memory_inventory(options.memory_root)
        except (FileNotFoundError, PermissionError):
            manifest["components"]["memory"] = {"status": "unavailable"}
            manifest["warnings"].append("Canonical memory root is unavailable")
        else:
            for relative in inventory:
                copy_checked(options.memory_root, relative, stage / "memory" / relative)
            if (inventory, excluded) != memory_inventory(options.memory_root):
                raise BackupError("Canonical memory changed during backup; retry after writers finish")
            manifest["excluded"].extend(excluded)
            manifest["components"]["memory"] = {"status": "included", "file_count": len(inventory)}
    if options.require_memory and manifest["components"]["memory"]["status"] != "included":
        raise BackupError("Required canonical memory is not available")
    for path in sorted(stage.rglob("*")):
        if path.is_file():
            manifest["files"][str(path.relative_to(stage))] = checksum(path)
    return manifest


def fsync_directory(path: Path) -> None:
    with directory_fd(path) as descriptor:
        os.fsync(descriptor)


def prune(backups: Path, keep: int) -> None:
    # Keep a separate DB-only generation during migration.
    for pattern in (r"helios-[0-9]{8}T[0-9]{6}Z\.db", r"helios-[0-9]{8}T[0-9]{12}Z-[a-f0-9]{8}\.tar\.gz"):
        paths = [path for path in backups.iterdir() if re.fullmatch(pattern, path.name) and not path.is_symlink() and path.is_file()]
        paths.sort(key=lambda path: (path.stat().st_mtime_ns, path.name))
        for path in paths[:max(0, len(paths) - keep)]:
            path.unlink()


def create_backup(options) -> dict:
    for source in (options.artifact_root, options.state_dir, options.memory_root):
        if source and (options.backup_dir == source or source in options.backup_dir.parents):
            raise BackupError("Backup destination must not be inside a captured source root")
    private_directory(options.backup_dir)
    directory_info = options.backup_dir.stat()
    if directory_info.st_uid != os.geteuid() or stat.S_IMODE(directory_info.st_mode) & 0o022:
        raise BackupError("Backup directory must be owned by the backup user and not group/world writable")
    with tempfile.TemporaryDirectory(prefix=".helios-backup-", dir=options.backup_dir) as temporary:
        temporary_path = Path(temporary)
        stage = temporary_path / "payload"
        private_directory(stage)
        manifest = capture_components(stage, options)
        private_write(stage / "manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
        pending = temporary_path / "archive.tar.gz"
        private_write(pending, b"")
        with tarfile.open(pending, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    info = archive.gettarinfo(str(path), arcname=str(path.relative_to(stage)))
                    info.mode, info.uid, info.gid, info.uname, info.gname = 0o600, 0, 0, "", ""
                    with path.open("rb") as source:
                        archive.addfile(info, source)
        # Verify serialized bytes before a recovery point becomes visible.
        import importlib.util
        spec = importlib.util.spec_from_file_location("helios_restore", REPO / "scripts" / "restore-memory.py")
        restore = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(restore)
        try:
            restore.validate_archive(pending, temporary_path / "verified")
        except restore.backup.BackupError as exc:
            raise BackupError(str(exc)) from exc
        with pending.open("rb") as completed:
            os.fsync(completed.fileno())
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = options.backup_dir / f"helios-{stamp}-{uuid.uuid4().hex[:8]}.tar.gz"
        os.link(pending, destination, follow_symlinks=False)
        fsync_directory(options.backup_dir)
    prune(options.backup_dir, options.keep)
    return {"ok": True, "archive": str(destination), "warnings": manifest["warnings"], "excluded_count": len(manifest["excluded"]), "off_host_verified": False}


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.environ.get("HELIOS_DATA_DIR", "/var/lib/helios"))
    parser.add_argument("--database", default=os.environ.get("HELIOS_DATABASE_PATH"))
    parser.add_argument("--artifact-root", default=os.environ.get("HELIOS_ARTIFACT_ROOT"))
    parser.add_argument("--state-dir", default=os.environ.get("HELIOS_STATE_DIR"))
    parser.add_argument("--memory-root", default=os.environ.get("HELIOS_MEMORY_ROOT"))
    parser.add_argument("--backup-dir", default=os.environ.get("HELIOS_BACKUP_DIR"))
    parser.add_argument("--keep", type=int, default=os.environ.get("HELIOS_LOCAL_BACKUPS_TO_KEEP", "14"))
    parser.add_argument("--require-memory", action="store_true", default=os.environ.get("HELIOS_BACKUP_REQUIRE_MEMORY", "0").lower() in ("1", "true", "yes"))
    options = parser.parse_args(argv)
    if options.keep < 1:
        parser.error("--keep must be at least 1")
    options.data_dir = absolute(options.data_dir)
    options.database = absolute(options.database or options.data_dir / "helios.db")
    options.artifact_root = absolute(options.artifact_root or options.data_dir / "artifacts")
    options.state_dir = absolute(options.state_dir or options.data_dir / "runtime")
    options.backup_dir = absolute(options.backup_dir or options.data_dir / "backups")
    options.memory_root = absolute(options.memory_root) if options.memory_root else None
    return options


def main(argv=None) -> int:
    try:
        print(json.dumps(create_backup(arguments(argv)), sort_keys=True))
        return 0
    except (BackupError, OSError, sqlite3.Error, tarfile.TarError, ValueError) as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
