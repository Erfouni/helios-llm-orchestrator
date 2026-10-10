import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest

from agent.project_memory import ProjectStore


REPO = Path(__file__).resolve().parents[1]
BACKUP = REPO / "scripts" / "backup-memory.py"
RESTORE = REPO / "scripts" / "restore-memory.py"


class BackupMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "live"
        self.artifacts = self.data / "artifacts"
        self.runtime = self.data / "runtime"
        self.memory = self.root / "canonical-memory"
        self.backups = self.root / "external-backups"
        self.runtime.mkdir(parents=True)
        (self.runtime / "benchmark_history").mkdir()
        self.memory.mkdir()
        (self.memory / "registry").mkdir()
        (self.memory / "artifacts").mkdir()
        self.store = ProjectStore(self.data / "helios.db", self.artifacts)
        self.project = self.store.create_project(
            {"name": "Restore reference", "objective": "Retain evidence", "budget_usd": 2},
            "backup-fixture",
        )
        with sqlite3.connect(self.data / "helios.db") as db:
            db.row_factory = sqlite3.Row
            self.artifact = self.store._write_artifact(
                db, self.project["id"], None, "evidence.txt", "text/plain",
                b"Verified artifact bytes\n", {"source": "test"},
            )
        self.registry = b'{"schema_version":2,"categories":{}}\n'
        (self.runtime / "benchmark_registry.json").write_bytes(self.registry)
        (self.runtime / "benchmark_history" / "20261010T120000Z.json").write_bytes(self.registry)
        (self.memory / "MEMORY.md").write_text("# Canonical durable memory\n")
        (self.memory / "metadata.json").write_text('{"revision": 1}\n')
        (self.memory / "registry" / "projects.jsonl").write_text('{"project": "reference"}\n')
        (self.memory / "artifacts" / "handoff.pdf").write_bytes(b"report bytes")
        self.env = dict(os.environ)
        self.env.update({
            "HELIOS_DATA_DIR": str(self.data),
            "HELIOS_DATABASE_PATH": str(self.data / "helios.db"),
            "HELIOS_ARTIFACT_ROOT": str(self.artifacts),
            "HELIOS_STATE_DIR": str(self.runtime),
            "HELIOS_MEMORY_ROOT": str(self.memory),
            "HELIOS_BACKUP_DIR": str(self.backups),
            "HELIOS_BACKUP_REQUIRE_MEMORY": "1",
            "HELIOS_LOCAL_BACKUPS_TO_KEEP": "2",
        })

    def backup(self, success=True, *extra):
        result = subprocess.run(
            [sys.executable, str(BACKUP), *extra], env=self.env,
            capture_output=True, text=True, timeout=30,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            archives = sorted(self.backups.glob("helios-*.tar.gz"))
            self.assertTrue(archives, "Backup must publish a self-contained archive")
            return archives[-1]
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def restore(self, archive, destination=None, success=True):
        command = [sys.executable, str(RESTORE), str(archive)]
        command += ["--validate-only"] if destination is None else ["--destination", str(destination)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def archive_files(self, archive):
        with tarfile.open(archive, "r:gz") as tar:
            return {entry.name: tar.extractfile(entry).read() for entry in tar if entry.isfile()}

    def rewrite_archive(self, archive, files, extra=None):
        with tarfile.open(archive, "w:gz") as tar:
            for name, content in files.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                tar.addfile(entry, io.BytesIO(content))
            if extra is not None:
                tar.addfile(extra)

    def test_round_trip_restores_usable_store_artifacts_benchmarks_and_canonical_memory(self):
        archive = self.backup()
        files = self.archive_files(archive)
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(manifest["format_version"], 1)
        self.assertIn("release", manifest)
        self.assertIn("schema", manifest)
        self.assertIn("dependencies", manifest)
        self.assertEqual(set(manifest["files"]), set(files) - {"manifest.json"})
        for name, metadata in manifest["files"].items():
            self.assertEqual(metadata["sha256"], hashlib.sha256(files[name]).hexdigest())
            self.assertEqual(metadata["size"], len(files[name]))
        self.restore(archive)
        destination = self.root / "restored"
        self.restore(archive, destination)
        restored = ProjectStore(destination / "helios.db", destination / "artifacts")
        self.assertEqual(restored.get_project(self.project["id"])["name"], "Restore reference")
        artifact = restored.list_artifacts(self.project["id"])["artifacts"][0]
        self.assertFalse(Path(artifact["path"]).is_absolute())
        self.assertEqual((destination / "artifacts" / artifact["path"]).read_bytes(), b"Verified artifact bytes\n")
        self.assertEqual((destination / "runtime" / "benchmark_registry.json").read_bytes(), self.registry)
        self.assertEqual((destination / "runtime" / "benchmark_history" / "20261010T120000Z.json").read_bytes(), self.registry)
        for path in self.memory.rglob("*"):
            if path.is_file():
                self.assertEqual((destination / "memory" / path.relative_to(self.memory)).read_bytes(), path.read_bytes())
        added = restored.create_project({"name": "Post-restore", "objective": "New writes"}, "after-restore")
        self.assertEqual(restored.get_project(added["id"])["name"], "Post-restore")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((destination / "helios.db").stat().st_mode), 0o600)
            for directory in (path for path in destination.rglob("*") if path.is_dir()):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, str(directory))

    def test_corruption_is_rejected_before_destination_or_parent_is_created(self):
        archive = self.backup()
        files = self.archive_files(archive)
        name = "artifacts/" + self.artifact["path"]
        files[name] += b"corrupted"
        self.rewrite_archive(archive, files)
        destination = self.root / "untouched-parent" / "restored"
        result = self.restore(archive, destination, success=False)
        self.assertIn("checksum", result.stderr.lower())
        self.assertFalse(destination.parent.exists())

    def test_archive_traversal_and_links_are_rejected_without_writes(self):
        source = self.backup()
        original = self.archive_files(source)
        for name in ("../outside", "/absolute", "memory/../outside", "memory\\outside", "C:/escape"):
            with self.subTest(name=name):
                files = dict(original)
                manifest = json.loads(files["manifest.json"])
                files[name] = b"hostile"
                manifest["files"][name] = {"size": 7, "sha256": hashlib.sha256(b"hostile").hexdigest()}
                files["manifest.json"] = json.dumps(manifest).encode()
                archive = self.root / "hostile.tar.gz"
                self.rewrite_archive(archive, files)
                destination = self.root / "bad-target"
                self.restore(archive, destination, success=False)
                self.assertFalse(destination.exists())
        link = tarfile.TarInfo("memory/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        self.rewrite_archive(source, original, link)
        self.restore(source, self.root / "symlink-target", success=False)
        self.assertFalse((self.root / "symlink-target").exists())

    def test_credentials_and_unrelated_runtime_files_are_excluded_and_reported(self):
        marker = "test-credential-" + "do-not-copy"
        self.env["OPENROUTER_API_KEY"] = marker
        self.env["HELIOS_AUTH_TOKEN"] = marker
        for name in (".env", "credentials.json", "secrets.md", "id_rsa"):
            (self.memory / name).write_text(marker)
        (self.memory / ".ssh").mkdir()
        (self.memory / ".ssh" / "config").write_text(marker)
        (self.runtime / "credentials.json").write_text(marker)
        (self.runtime / "unrelated.log").write_text(marker)
        (self.data / "server.env").write_text(marker)
        archive = self.backup()
        files = self.archive_files(archive)
        self.assertNotIn(marker.encode(), b"".join(files.values()))
        manifest = json.loads(files["manifest.json"])
        self.assertTrue(manifest["excluded"])
        self.assertEqual(manifest["components"]["memory"]["status"], "included")

    def test_missing_or_changed_referenced_artifacts_never_publish_or_prune(self):
        self.backups.mkdir()
        legacy = self.backups / "helios-20260101T000000Z.db"
        legacy.write_bytes(b"legacy recovery point")
        path = self.artifacts / self.artifact["path"]
        path.write_bytes(b"Changed bytes")
        self.backup(False)
        self.assertEqual(list(self.backups.iterdir()), [legacy])
        path.unlink()
        self.backup(False)
        self.assertEqual(list(self.backups.iterdir()), [legacy])

    def test_artifact_traversal_or_symlink_source_is_rejected(self):
        outside = self.root / "outside-secret"
        outside.write_bytes(b"must remain outside")
        for unsafe in ("../../outside-secret", str(outside)):
            with self.subTest(unsafe=unsafe):
                with sqlite3.connect(self.data / "helios.db") as db:
                    db.execute("UPDATE artifacts SET path = ?", (unsafe,))
                self.backup(False)
        with sqlite3.connect(self.data / "helios.db") as db:
            db.execute("UPDATE artifacts SET path = ?", (self.artifact["path"],))
        path = self.artifacts / self.artifact["path"]
        path.unlink()
        path.symlink_to(outside)
        self.backup(False)
        self.assertFalse(list(self.backups.glob("*.tar.gz")))

    def test_absolute_artifact_paths_are_rebased_without_changing_source_database(self):
        absolute = str(self.artifacts / self.artifact["path"])
        with sqlite3.connect(self.data / "helios.db") as db:
            db.execute("UPDATE artifacts SET path = ?", (absolute,))
        archive = self.backup()
        destination = self.root / "rebased"
        self.restore(archive, destination)
        with sqlite3.connect(destination / "helios.db") as db:
            self.assertEqual(db.execute("SELECT path FROM artifacts").fetchone()[0], self.artifact["path"])
        with sqlite3.connect(self.data / "helios.db") as db:
            self.assertEqual(db.execute("SELECT path FROM artifacts").fetchone()[0], absolute)

    def test_restore_refuses_existing_destination_and_symlinked_parent(self):
        archive = self.backup()
        destination = self.root / "existing"
        destination.mkdir()
        (destination / "keep.txt").write_text("keep")
        self.restore(archive, destination, success=False)
        self.assertEqual((destination / "keep.txt").read_text(), "keep")
        linked = self.root / "linked"
        linked.symlink_to(destination, target_is_directory=True)
        self.restore(archive, linked / "restored", success=False)
        self.assertFalse((destination / "restored").exists())

    def test_restore_refuses_writable_by_others_destination_parent(self):
        archive = self.backup()
        unsafe = self.root / "unsafe-parent"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        self.restore(archive, unsafe / "restored", success=False)
        self.assertEqual(list(unsafe.iterdir()), [])

    def test_atomic_publication_never_replaces_even_an_empty_directory(self):
        spec = importlib.util.spec_from_file_location("restore_under_test", RESTORE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(callable(getattr(module, "publish_directory", None)), "Restore needs an atomic no-replace publication primitive")
        staged = self.root / "staged"
        staged.mkdir()
        (staged / "verified.txt").write_text("validated")
        destination = self.root / "already-published"
        destination.mkdir()
        with self.assertRaises(FileExistsError):
            module.publish_directory(staged, destination)
        self.assertTrue((staged / "verified.txt").exists())
        self.assertEqual(list(destination.iterdir()), [])
        destination.rmdir()
        module.publish_directory(staged, destination)
        self.assertEqual((destination / "verified.txt").read_text(), "validated")
        self.assertFalse(staged.exists())

    def test_database_integrity_and_reference_hashes_are_verified_after_archive_hashes(self):
        archive = self.backup()
        original = self.archive_files(archive)
        for corrupt in ("sqlite", "reference"):
            with self.subTest(corrupt=corrupt):
                files = dict(original)
                if corrupt == "sqlite":
                    files["helios.db"] = b"invalid database"
                else:
                    database = self.root / "edited.db"
                    database.write_bytes(files["helios.db"])
                    with sqlite3.connect(database) as db:
                        db.execute("UPDATE artifacts SET checksum_sha256 = ?", ("0" * 64,))
                    files["helios.db"] = database.read_bytes()
                manifest = json.loads(files["manifest.json"])
                manifest["files"]["helios.db"] = {"size": len(files["helios.db"]), "sha256": hashlib.sha256(files["helios.db"]).hexdigest()}
                files["manifest.json"] = json.dumps(manifest).encode()
                self.rewrite_archive(archive, files)
                destination = self.root / "invalid-db"
                self.restore(archive, destination, success=False)
                self.assertFalse(destination.exists())

    def test_retention_keeps_recent_archives_and_legacy_database_recovery_points(self):
        self.backups.mkdir()
        for index in range(4):
            path = self.backups / f"helios-2026010{index + 1}T000000Z.db"
            path.write_bytes(b"legacy")
            os.utime(path, (index + 1, index + 1))
        for _ in range(3):
            self.backup()
        self.assertEqual(len(list(self.backups.glob("helios-*.tar.gz"))), 2)
        self.assertEqual(len(list(self.backups.glob("helios-*.db"))), 2)
        self.assertTrue((self.backups / "helios-20260104T000000Z.db").exists())

    def test_required_memory_missing_fails_and_optional_missing_is_visible(self):
        self.env["HELIOS_MEMORY_ROOT"] = str(self.root / "absent")
        self.backup(False)
        self.env["HELIOS_BACKUP_REQUIRE_MEMORY"] = "0"
        archive = self.backup()
        manifest = json.loads(self.archive_files(archive)["manifest.json"])
        self.assertEqual(manifest["components"]["memory"]["status"], "unavailable")
        self.assertTrue(manifest["warnings"])


if __name__ == "__main__":
    unittest.main()
