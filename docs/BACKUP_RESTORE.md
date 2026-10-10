# Backup and restore

Run these tools with Python 3.10+ on Linux. They use only the standard library. Backup publishes a `helios-<UTC>-<unique>.tar.gz` only after reading back and validating its contents. The embedded `manifest.json` records SHA-256 and byte length for each payload, the SQLite schema fingerprint/version, release version, dependency requirements, source roots, exclusions and unavailable components. Environment variables and credential files are never copied as configuration.

Each archive contains an online SQLite snapshot; every artifact referenced by that snapshot, checked against its database checksum; `runtime/benchmark_registry.json` and timestamped `runtime/benchmark_history/*.json`; and the complete configured canonical-memory tree, including Markdown, metadata, JSONL registries and handoff documents. Hidden paths, credential/secret/key filenames and key-container extensions are excluded and listed in the manifest. A forbidden referenced project artifact makes backup fail. Ordinary files under the configured roots are preserved user data; this is not content redaction or secret discovery. Keep credentials outside these data roots.

The database and referenced artifacts are consistent with the SQLite snapshot. Benchmarks are copied under their existing refresh lock; an active refresh makes backup fail for a later retry. Canonical memory is checked for inventory/file changes during capture and fails if changes are detected. It has no transaction shared with SQLite: stop canonical-memory writers when an exact cross-component recovery point is required.

```sh
python3 scripts/backup-memory.py \
  --data-dir /var/lib/helios \
  --memory-root /var/lib/helios/canonical-memory --require-memory
```

`--database`, `--artifact-root`, `--state-dir`, `--memory-root`, `--backup-dir` and `--keep` override the matching settings in [the backup environment example](../deploy/helios-backup.env.example). The runtime setting is `HELIOS_STATE_DIR`. Without a configured/accessible memory root, the CLI reports a manifest warning; `--require-memory` or `HELIOS_BACKUP_REQUIRE_MEMORY=1` instead makes that an error. Missing database, missing referenced artifacts or corrupt artifact bytes always fail. Exclusions are reported as a count in command output and individually in the manifest.

The backup directory must belong to the invoking user and cannot be group/world writable. Archives are mode `0600`; staging and restored directories are `0700`. Symlinks and special files are rejected. Nothing is pruned after a failed backup. Retention defaults to the latest 14 archives **and**, separately, the latest 14 legacy `helios-*.db` files. Legacy files remain database-only recovery points; this tool does not manufacture missing artifacts, benchmarks or canonical memory from them. Preserve their matching original data directories if using an older SQLite snapshot for manual recovery.

The systemd unit uses `/etc/helios/backup.env`, without loading the gateway's credential environment. It requires canonical memory and retains `ProtectHome=true` and `ProtectSystem=strict`. Before enabling the timer, expose the intended canonical root to the unit at `/var/lib/helios/canonical-memory` with a read-only bind mount, and grant the backup account read access. For a host with canonical memory under `/srv/helios-memory`, a drop-in can contain:

```ini
[Service]
BindReadOnlyPaths=/srv/helios-memory:/var/lib/helios/canonical-memory
```

The deployment operator must check the mounted files are readable by `helios`. If root ownership of canonical files requires a root backup unit, explicitly set `User=root` and `Group=root` in a drop-in and give that account its own root-owned backup directory; do not grant the unit gateway credentials. Changing user alone does not satisfy the directory ownership check. Clear `StateDirectory=` in a root-user drop-in so the gateway's data directory remains owned by its service account. Reading restricted canonical files and acquiring a service-owned benchmark lock may require `CapabilityBoundingSet=CAP_DAC_OVERRIDE`; retain the read-only canonical bind, `ProtectHome=true`, `ProtectSystem=strict`, no gateway credential environment, and the narrow writable data path. This is an explicit host-specific override, not a default privilege increase. Choose retention for the available space and preserve existing DB-only recovery points during migration. An explicit external destination works with `HELIOS_BACKUP_DIR=/mounted-backups/helios` plus `ReadWritePaths=/mounted-backups/helios` and suitable ownership. Merely choosing a different directory does not establish off-host protection; output keeps `off_host_verified=false`.

Validate a trusted archive, then restore into a **new** directory under a parent owned by the restoring user with no group/world write permission:

```sh
python3 scripts/restore-memory.py /path/to/helios-backup.tar.gz --validate-only
python3 scripts/restore-memory.py /path/to/helios-backup.tar.gz \
  --destination /var/lib/helios-recovery/generation-1
```

Validation checks the entire inventory, safe paths, every file checksum, SQLite integrity/foreign keys/schema and each artifact reference before touching the destination or its parent. Archive ownership and permissions are ignored; links, duplicate entries, traversal and unexpected roots are forbidden. Restore stages the validated files, rebases legacy absolute artifact paths to relative paths **after** validating the original database bytes, checks integrity again, then atomically publishes without replacing any existing destination. The platform/filesystem must support Linux `renameat2(RENAME_NOREPLACE)`; otherwise restore fails closed. The manifest remains evidence of the original archive, so a rebased database can have a different hash afterward. Hashes detect corruption, not archive authenticity; use an archive from a trusted recovery location.

The restored layout is `helios.db`, `artifacts/`, `runtime/`, and `memory/`. Inspect it and perform an isolated read/write check before draining the service and switching `HELIOS_DATABASE_PATH`, `HELIOS_ARTIFACT_ROOT`, `HELIOS_STATE_DIR`, and the canonical-memory consumer to those directories. Files belong to the restoring account; the service account needs ownership/read-write access before switching. Credentials remain independently provisioned. Restore itself never restarts a service, contacts providers, or merges into live state. Retain the previous live generation for rollback.
