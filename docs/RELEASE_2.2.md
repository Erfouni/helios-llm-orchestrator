# Helios 2.2

This release hardens the execution boundary between approved projects, background jobs and paid provider calls.

- Validate before claiming work; reserve project cost/token capacity atomically.
- Fence late and duplicate completions, preserve cancellation, and reconcile uncertain bills with evidence.
- Persist explicitly enqueued jobs with bounded workers and leases; stop at artifact-bound verification.
- Enforce actual outbound concurrency and total transport deadlines, including Linux parent-death cleanup.
- Preserve verified predecessor context and require explicit project scope for global context.
- Select models using dated evidence, current capabilities and executable evaluation settings; add deterministic ARC-AGI-2 JSON extraction.
- Record observed provider usage and distinguish liveness from readiness.
- Back up database, referenced artifacts, benchmark state and configured canonical memory with checksum-verified isolated restore.

Validation includes the Python and MCP suites, plus regression checks for unsupported recovery platforms. Provider integration tests use local fixtures; they incur no live provider cost. Full backup/restore, migration compatibility and deployment health were also checked on a Linux host.

Read [API.md](API.md) for queue, evidence and reconciliation contracts and [BACKUP_RESTORE.md](BACKUP_RESTORE.md) before enabling full backups. Existing projects do not automatically enqueue work. Full backup and restore require Linux facilities and reject unsupported platforms before writing; Windows transport crash behavior was not covered by the Linux deployment checks. Unknown bills remain blocked, invalid benchmark evidence is not automatically repaired, and off-host backup is not configured by default.
