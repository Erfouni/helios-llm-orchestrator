#!/usr/bin/env python3
"""Validate a Helios archive, or atomically restore it to a new directory."""

from __future__ import annotations

import argparse
import ctypes
import errno
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile

_spec = importlib.util.spec_from_file_location("helios_backup", Path(__file__).with_name("backup-memory.py"))
backup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise backup.BackupError("Duplicate manifest key")
        result[key] = value
    return result


def validate_archive(path: Path, stage: Path) -> dict:
    """Extract only to private staging; verify inventory, bytes, SQLite and references."""
    backup.private_directory(stage)
    with backup.source_file(path.parent, path.name) as source, tarfile.open(fileobj=source, mode="r:gz") as archive:
        members = {}
        # Never apply archive permissions, ownership or links with extractall().
        for member in archive:
            name = backup.safe_relative(member.name)
            if not member.isfile() or member.linkname or member.issparse():
                raise backup.BackupError("Archive links and non-regular members are forbidden")
            if name in members or len(members) >= 100000:
                raise backup.BackupError("Duplicate archive member or excessive file count")
            if name not in ("manifest.json", "helios.db") and not name.startswith(("artifacts/", "runtime/", "memory/")):
                raise backup.BackupError("Unexpected archive path")
            members[name] = member
        manifest_member = members.get("manifest.json")
        if manifest_member is None or manifest_member.size > 16 * 1024 * 1024:
            raise backup.BackupError("Missing or oversized manifest")
        manifest = json.load(archive.extractfile(manifest_member), object_pairs_hook=unique_object)
        if not isinstance(manifest, dict) or manifest.get("format_version") != backup.FORMAT_VERSION:
            raise backup.BackupError("Unsupported backup format")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != set(members) - {"manifest.json"} or "helios.db" not in files:
            raise backup.BackupError("Archive inventory differs from manifest")
        for name, expected in files.items():
            backup.safe_relative(name)
            if not isinstance(expected, dict) or type(expected.get("size")) is not int or expected["size"] < 0 or not isinstance(expected.get("sha256"), str) or not re.fullmatch("[a-f0-9]{64}", expected["sha256"]):
                raise backup.BackupError("Invalid manifest file metadata")
            if members[name].size != expected["size"]:
                raise backup.BackupError("Archive checksum/size mismatch")
            if any(backup.credential_name(part) for part in name.split("/")[1:]):
                raise backup.BackupError("Credential or hidden paths are forbidden")
            target = stage / name
            backup.private_directory(target.parent)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as destination, archive.extractfile(members[name]) as payload:
                shutil.copyfileobj(payload, destination, backup.CHUNK)
            if backup.checksum(target) != expected:
                raise backup.BackupError("Archive checksum mismatch")
        backup.private_write(stage / "manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    schema = backup.check_database(stage / "helios.db")
    if schema != manifest.get("schema"):
        raise backup.BackupError("Database schema differs from manifest")
    try:
        artifact_root = backup.absolute(manifest["configuration"]["source_roots"]["artifacts"])
    except (KeyError, TypeError) as exc:
        raise backup.BackupError("Missing artifact root metadata") from exc
    referenced = set()
    for _, original, expected in backup.artifact_rows(stage / "helios.db"):
        relative = backup.artifact_relative(original, artifact_root)
        name = "artifacts/" + relative
        if name not in files or files[name]["sha256"] != expected:
            raise backup.BackupError("Artifact reference checksum differs from archive")
        referenced.add(name)
    if referenced != {name for name in files if name.startswith("artifacts/")}:
        raise backup.BackupError("Archive contains unreferenced project artifacts")
    return manifest


def rebase_artifacts(stage: Path, manifest: dict) -> None:
    original_root = backup.absolute(manifest["configuration"]["source_roots"]["artifacts"])
    # Verify original archive hashes before changing this isolated DB.
    with sqlite3.connect(stage / "helios.db") as database:
        database.execute("PRAGMA trusted_schema = OFF")
        for artifact_id, original, _ in backup.artifact_rows(stage / "helios.db"):
            relative = backup.artifact_relative(original, original_root)
            if original != relative:
                database.execute("UPDATE artifacts SET path = ? WHERE id = ?", (relative, artifact_id))
    backup.check_database(stage / "helios.db")


def publish_directory(staged: Path, destination: Path) -> None:
    """Use Linux renameat2 so a racing creator can never be overwritten."""
    backup.require_supported_platform()
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise backup.BackupError("Atomic no-replace directory restore requires Linux renameat2")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    with backup.directory_fd(staged.parent) as source_parent, backup.directory_fd(destination.parent) as target_parent:
        result = rename(source_parent, os.fsencode(staged.name), target_parent, os.fsencode(destination.name), 1)
        if result:
            error = ctypes.get_errno()
            if error in (errno.ENOSYS, errno.EINVAL):
                raise backup.BackupError("Filesystem does not support atomic no-replace restore")
            raise OSError(error, os.strerror(error), str(destination))
        os.fsync(target_parent)


def restore(path: Path, destination: Path | None = None) -> dict:
    backup.require_supported_platform()
    # Validate before even creating the destination's parent.
    with tempfile.TemporaryDirectory(prefix="helios-validate-") as temporary:
        validated = Path(temporary) / "payload"
        manifest = validate_archive(path, validated)
        if destination is None:
            return {"ok": True, "validated": True, "file_count": len(manifest["files"]), "warnings": manifest.get("warnings", [])}
        backup.no_symlinks(destination)
        if destination.exists():
            raise backup.BackupError("Restore destination already exists; choose a new directory")
        backup.private_directory(destination.parent)
        parent_info = destination.parent.stat()
        if parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) & 0o022:
            raise backup.BackupError("Destination parent must be owned by the restoring user and not group/world writable")
        with tempfile.TemporaryDirectory(prefix=".helios-restore-", dir=destination.parent) as temporary_destination:
            staged = Path(temporary_destination) / "payload"
            shutil.copytree(validated, staged)
            rebase_artifacts(staged, manifest)
            for directory in (staged / "artifacts", staged / "runtime", staged / "memory"):
                backup.private_directory(directory)
            for item in staged.rglob("*"):
                if item.is_file():
                    with item.open("rb") as payload:
                        os.fsync(payload.fileno())
            for directory in sorted((item for item in staged.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
                backup.fsync_directory(directory)
            backup.fsync_directory(staged)
            backup.no_symlinks(destination)
            if destination.exists():
                raise backup.BackupError("Restore destination appeared during validation")
            publish_directory(staged, destination)
    return {"ok": True, "destination": str(destination), "warnings": manifest.get("warnings", [])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=backup.absolute)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--validate-only", action="store_true")
    action.add_argument("--destination", type=backup.absolute)
    options = parser.parse_args(argv)
    try:
        print(json.dumps(restore(options.archive, options.destination), sort_keys=True))
        return 0
    except (backup.BackupError, OSError, sqlite3.Error, tarfile.TarError, ValueError, EOFError) as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
