from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import stat
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import cast

from .models import CanonicalJob
from .repository import (
    ObservationResult,
    RepositoryError,
    RepositoryLockedError,
    SourceObservation,
    StoredJob,
    StoredSourceRun,
)

_MIGRATION_1 = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE source_runs (
    id INTEGER PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,
    source_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    receipt_json BLOB,
    error TEXT
) STRICT;

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    source_name TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK (state IN ('new', 'changed', 'unchanged', 'stale', 'closed')),
    current_version_id INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (source_name, source_job_id),
    FOREIGN KEY (current_version_id, id) REFERENCES job_versions(id, job_id)
) STRICT;

CREATE TABLE job_versions (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    canonical_json BLOB NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (job_id, content_hash),
    UNIQUE (id, job_id)
) STRICT;

CREATE TABLE source_observations (
    id INTEGER PRIMARY KEY,
    source_run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    job_version_id INTEGER NOT NULL,
    source_name TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source_url TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    raw_json BLOB NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('new', 'changed', 'unchanged')),
    UNIQUE (source_run_id, source_name, source_job_id),
    FOREIGN KEY (job_version_id, job_id) REFERENCES job_versions(id, job_id)
) STRICT;

CREATE TABLE profile_snapshots (
    profile_hash TEXT PRIMARY KEY,
    snapshot_bytes BLOB NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE strategy_snapshots (
    strategy_hash TEXT PRIMARY KEY,
    snapshot_bytes BLOB NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE evaluations (
    id INTEGER PRIMARY KEY,
    job_version_id INTEGER NOT NULL REFERENCES job_versions(id) ON DELETE CASCADE,
    profile_hash TEXT NOT NULL REFERENCES profile_snapshots(profile_hash),
    strategy_hash TEXT NOT NULL REFERENCES strategy_snapshots(strategy_hash),
    evaluation_bytes BLOB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (job_version_id, profile_hash, strategy_hash)
) STRICT;

CREATE TABLE reports (
    id INTEGER PRIMARY KEY,
    report_key TEXT NOT NULL UNIQUE,
    report_bytes BLOB NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE feedback (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    feedback_key TEXT NOT NULL,
    feedback_bytes BLOB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (job_id, feedback_key)
) STRICT;

CREATE TABLE llm_assessments (
    id INTEGER PRIMARY KEY,
    evaluation_id INTEGER NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
    assessment_key TEXT NOT NULL,
    assessment_bytes BLOB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (evaluation_id, assessment_key)
) STRICT;

CREATE TABLE writer_lock (
    lock_name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
) STRICT;
"""

_MIGRATION_2 = """
CREATE TABLE sources_snapshots (
    sources_hash TEXT PRIMARY KEY,
    snapshot_bytes BLOB NOT NULL,
    created_at TEXT NOT NULL
) STRICT;
"""

_LEGACY_SOURCES_SNAPSHOT = (
    b'{"schema_version":0,"status":"unavailable","reason":'
    b'"source configuration predates durable correlation"}'
)
_LEGACY_SOURCES_HASH = (
    "aa374f95cb38ced06ca7350ec2bf7c123d74a3d335f1e92c182bf99f67e2d559"
)

_MIGRATION_3 = f"""
INSERT INTO sources_snapshots(sources_hash, snapshot_bytes, created_at)
VALUES (
    '{_LEGACY_SOURCES_HASH}',
    X'{_LEGACY_SOURCES_SNAPSHOT.hex()}',
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
)
ON CONFLICT(sources_hash) DO NOTHING;

CREATE TABLE source_runs_v3 (
    id INTEGER PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,
    source_name TEXT NOT NULL,
    sources_hash TEXT NOT NULL REFERENCES sources_snapshots(sources_hash),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    receipt_json BLOB,
    error TEXT
) STRICT;

INSERT INTO source_runs_v3(
    id, run_key, source_name, sources_hash, started_at, finished_at,
    status, receipt_json, error
)
SELECT
    id, run_key, source_name, '{_LEGACY_SOURCES_HASH}', started_at, finished_at,
    status, receipt_json, error
FROM source_runs;

DROP TABLE source_runs;
ALTER TABLE source_runs_v3 RENAME TO source_runs;
"""

_MIGRATION_4 = """
CREATE TABLE delivery_attempts (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    report_key TEXT NOT NULL REFERENCES reports(report_key) ON DELETE CASCADE,
    report_hash TEXT NOT NULL,
    destination_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    chunk_count INTEGER NOT NULL CHECK (chunk_count > 0 AND chunk_index < chunk_count),
    chunk_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'acknowledged', 'failed', 'rate_limited', 'retryable_failure'
    )),
    attempted_at TEXT NOT NULL,
    http_class TEXT NOT NULL,
    idempotency_state TEXT NOT NULL CHECK (
        idempotency_state IN ('pending', 'acknowledged')
    )
) STRICT;
CREATE INDEX delivery_attempt_ack_lookup ON delivery_attempts(
    report_key, report_hash, destination_id, chunk_index, chunk_hash, status
);
"""

_MIGRATION_5 = """
ALTER TABLE delivery_attempts RENAME TO delivery_attempts_v4;
CREATE TABLE delivery_attempts (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    report_key TEXT NOT NULL REFERENCES reports(report_key) ON DELETE CASCADE,
    report_hash TEXT NOT NULL,
    destination_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    chunk_count INTEGER NOT NULL CHECK (chunk_count > 0 AND chunk_index < chunk_count),
    chunk_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'acknowledged', 'failed', 'rate_limited', 'retryable_failure'
    )),
    attempted_at TEXT NOT NULL,
    http_class TEXT NOT NULL,
    idempotency_state TEXT NOT NULL CHECK (
        idempotency_state IN ('pending', 'acknowledged', 'indeterminate')
    )
) STRICT;
INSERT INTO delivery_attempts(
    id, report_id, report_key, report_hash, destination_id, chunk_index,
    chunk_count, chunk_hash, status, attempted_at, http_class, idempotency_state
)
SELECT
    id, report_id, report_key, report_hash, destination_id, chunk_index,
    chunk_count, chunk_hash, status, attempted_at, http_class, idempotency_state
FROM delivery_attempts_v4;
DROP TABLE delivery_attempts_v4;
CREATE INDEX delivery_attempt_ack_lookup ON delivery_attempts(
    report_key, report_hash, destination_id, chunk_index, chunk_hash, status
);
"""


class SQLiteRepository:
    """SQLite-backed durable history for a single career monitor installation."""

    def __init__(self, path: str | Path) -> None:
        database_path = Path(path)
        _prepare_private_directory(database_path.parent)
        if database_path.exists() or database_path.is_symlink():
            existing = database_path.lstat()
            if database_path.is_symlink() or not stat.S_ISREG(existing.st_mode):
                raise RepositoryError("unsafe SQLite database path")
        descriptor = os.open(
            database_path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.close(descriptor)
        os.chmod(database_path, 0o600)
        self._lock_path = Path(f"{database_path}.lock")
        self._connection = sqlite3.connect(
            database_path, isolation_level=None, timeout=0.0
        )
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self._connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + _MIGRATION_1
                + "\n"
                + "INSERT INTO schema_migrations(version, applied_at) "
                + "VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\n"
                + "PRAGMA user_version = 1;\n"
                + "COMMIT;"
            )
            version = 1
        if version == 1:
            self._connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + _MIGRATION_2
                + "\n"
                + "INSERT INTO schema_migrations(version, applied_at) "
                + "VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\n"
                + "PRAGMA user_version = 2;\n"
                + "COMMIT;"
            )
            version = 2
        if version == 2:
            self._connection.executescript(
                "PRAGMA foreign_keys = OFF;\n"
                + "BEGIN IMMEDIATE;\n"
                + _MIGRATION_3
                + "\n"
                + "INSERT INTO schema_migrations(version, applied_at) "
                + "VALUES (3, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\n"
                + "PRAGMA user_version = 3;\n"
                + "COMMIT;\n"
                + "PRAGMA foreign_keys = ON;"
            )
            version = 3
        if version == 3:
            self._connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + _MIGRATION_4
                + "\nINSERT INTO schema_migrations(version, applied_at) "
                + "VALUES (4, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\n"
                + "PRAGMA user_version = 4;\nCOMMIT;"
            )
            version = 4
        if version == 4:
            self._connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + _MIGRATION_5
                + "\nINSERT INTO schema_migrations(version, applied_at) "
                + "VALUES (5, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\n"
                + "PRAGMA user_version = 5;\nCOMMIT;"
            )
            version = 5
        if version != 5:
            raise RepositoryError(f"unsupported SQLite schema version: {version}")
        legacy = self._connection.execute(
            "SELECT snapshot_bytes FROM sources_snapshots WHERE sources_hash = ?",
            (_LEGACY_SOURCES_HASH,),
        ).fetchone()
        if legacy is None or bytes(legacy[0]) != _LEGACY_SOURCES_SNAPSHOT:
            raise RepositoryError("legacy sources snapshot content is invalid")

    @property
    def foreign_keys_enabled(self) -> bool:
        return bool(self._connection.execute("PRAGMA foreign_keys").fetchone()[0])

    def table_names(self) -> set[str]:
        rows = self._connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        return {str(row[0]) for row in rows}

    def integrity_check(self) -> str:
        return str(self._connection.execute("PRAGMA integrity_check").fetchone()[0])

    def foreign_key_check(self) -> tuple[tuple[object, ...], ...]:
        return tuple(
            tuple(row) for row in self._connection.execute("PRAGMA foreign_key_check")
        )

    @contextmanager
    def writer_lock(
        self,
        owner: str,
        *,
        timeout_seconds: float = 0.25,
        lease_seconds: float = 30.0,
        poll_seconds: float = 0.01,
    ) -> Generator[None]:
        """Hold the OS writer lock; lease time only timestamps SQLite evidence."""
        timings = (timeout_seconds, lease_seconds, poll_seconds)
        if (
            not owner
            or not all(math.isfinite(value) for value in timings)
            or timeout_seconds < 0
            or lease_seconds <= 0
            or poll_seconds <= 0
        ):
            raise ValueError("writer lock timing and owner must be valid")
        deadline = time.monotonic() + timeout_seconds
        lock_owner = f"{owner}:{uuid.uuid4().hex}"
        lock_fd = os.open(
            self._lock_path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(lock_fd, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RepositoryLockedError(
                            "writer lock was not available within "
                            f"{timeout_seconds:.3f}s"
                        ) from None
                    time.sleep(min(poll_seconds, remaining))

            now = time.time()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    "INSERT INTO writer_lock"
                    "(lock_name, owner, acquired_at, expires_at) "
                    "VALUES ('history-writer', ?, ?, ?) "
                    "ON CONFLICT(lock_name) DO UPDATE SET "
                    "owner = excluded.owner, acquired_at = excluded.acquired_at, "
                    "expires_at = excluded.expires_at",
                    (lock_owner, now, now + lease_seconds),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

            try:
                yield
            finally:
                self._connection.execute(
                    "DELETE FROM writer_lock "
                    "WHERE lock_name = 'history-writer' AND owner = ?",
                    (lock_owner,),
                )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def start_source_run(
        self,
        source_name: str,
        started_at: str,
        *,
        run_id: str,
        sources_hash: str = _LEGACY_SOURCES_HASH,
    ) -> int:
        if not source_name or not started_at or not run_id or not sources_hash:
            raise RepositoryError(
                "source name, start time, run ID, and sources hash are required"
            )
        self._connection.execute(
            "INSERT INTO source_runs"
            "(run_key, source_name, sources_hash, started_at, status) "
            "VALUES (?, ?, ?, ?, 'running') "
            "ON CONFLICT(run_key) DO NOTHING",
            (run_id, source_name, sources_hash, started_at),
        )
        row = self._connection.execute(
            "SELECT id, source_name, started_at, sources_hash "
            "FROM source_runs WHERE run_key = ?",
            (run_id,),
        ).fetchone()
        assert row is not None
        if row[1:] != (source_name, started_at, sources_hash):
            raise RepositoryError("run ID already identifies a different source run")
        return int(row[0])

    def record_observations(
        self, source_run_id: int, observations: tuple[SourceObservation, ...]
    ) -> tuple[ObservationResult, ...]:
        results: list[ObservationResult] = []
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            run = self._connection.execute(
                "SELECT source_name, status FROM source_runs WHERE id = ?",
                (source_run_id,),
            ).fetchone()
            if run is None:
                raise RepositoryError("source run does not exist")
            if run[1] != "running":
                raise RepositoryError("source run is not running")
            source_name = str(run[0])
            for observation in observations:
                if observation.job.source_name != source_name:
                    raise RepositoryError("observation source does not match its run")
                if not observation.observed_at or not observation.raw_payload:
                    raise RepositoryError(
                        "observation time and raw payload are required"
                    )
                results.append(self._record_observation(source_run_id, observation))
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        return tuple(results)

    def _record_observation(
        self, source_run_id: int, observation: SourceObservation
    ) -> ObservationResult:
        job = observation.job
        canonical_bytes = _canonical_job_bytes(job)
        content_hash = hashlib.sha256(canonical_bytes).hexdigest()
        raw_hash = hashlib.sha256(observation.raw_payload).hexdigest()
        repeated = self._connection.execute(
            "SELECT o.payload_hash, o.outcome, o.job_id, o.job_version_id, "
            "v.content_hash, o.observed_at "
            "FROM source_observations AS o "
            "JOIN job_versions AS v ON v.id = o.job_version_id "
            "WHERE o.source_run_id = ? AND o.source_name = ? "
            "AND o.source_job_id = ?",
            (source_run_id, job.source_name, job.source_job_id),
        ).fetchone()
        if repeated is not None:
            if repeated[0] != raw_hash:
                raise RepositoryError(
                    "one source run contains conflicting payloads "
                    "for one source identity"
                )
            if repeated[4] != content_hash:
                raise RepositoryError(
                    "one source run contains conflicting canonical content "
                    "for one source identity"
                )
            if repeated[5] != observation.observed_at:
                raise RepositoryError(
                    "one source run contains conflicting observation time "
                    "for one source identity"
                )
            return ObservationResult(
                source_name=job.source_name,
                source_job_id=job.source_job_id,
                job_id=int(repeated[2]),
                job_version_id=int(repeated[3]),
                outcome=cast(str, repeated[1]),  # type: ignore[arg-type]
            )

        existing = self._connection.execute(
            "SELECT id, current_version_id FROM jobs "
            "WHERE source_name = ? AND source_job_id = ?",
            (job.source_name, job.source_job_id),
        ).fetchone()
        if existing is None:
            cursor = self._connection.execute(
                "INSERT INTO jobs(source_name, source_job_id, source_url, state, "
                "first_seen_at, last_seen_at) VALUES (?, ?, ?, 'new', ?, ?)",
                (
                    job.source_name,
                    job.source_job_id,
                    job.source_url,
                    observation.observed_at,
                    observation.observed_at,
                ),
            )
            job_id = _required_lastrowid(cursor)
            outcome = "new"
        else:
            job_id = int(existing[0])
            current = self._connection.execute(
                "SELECT content_hash FROM job_versions WHERE id = ?",
                (existing[1],),
            ).fetchone()
            current_hash = None if current is None else str(current[0])
            outcome = "unchanged" if current_hash == content_hash else "changed"

        version = self._connection.execute(
            "SELECT id FROM job_versions WHERE job_id = ? AND content_hash = ?",
            (job_id, content_hash),
        ).fetchone()
        if version is None:
            cursor = self._connection.execute(
                "INSERT INTO job_versions(job_id, content_hash, canonical_json, "
                "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                (
                    job_id,
                    content_hash,
                    canonical_bytes,
                    observation.observed_at,
                    observation.observed_at,
                ),
            )
            version_id = _required_lastrowid(cursor)
        else:
            version_id = int(version[0])
            self._connection.execute(
                "UPDATE job_versions SET last_seen_at = ? WHERE id = ?",
                (observation.observed_at, version_id),
            )
        self._connection.execute(
            "UPDATE jobs SET source_url = ?, state = ?, current_version_id = ?, "
            "last_seen_at = ? WHERE id = ?",
            (job.source_url, outcome, version_id, observation.observed_at, job_id),
        )
        self._connection.execute(
            "INSERT INTO source_observations(source_run_id, job_id, job_version_id, "
            "source_name, source_job_id, observed_at, source_url, payload_hash, "
            "raw_json, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_run_id,
                job_id,
                version_id,
                job.source_name,
                job.source_job_id,
                observation.observed_at,
                job.source_url,
                raw_hash,
                observation.raw_payload,
                outcome,
            ),
        )
        return ObservationResult(
            source_name=job.source_name,
            source_job_id=job.source_job_id,
            job_id=job_id,
            job_version_id=version_id,
            outcome=cast(str, outcome),  # type: ignore[arg-type]
        )

    def complete_source_run(
        self,
        source_run_id: int,
        finished_at: str,
        *,
        complete: bool = True,
        receipt: bytes | None = None,
    ) -> None:
        if not finished_at:
            raise RepositoryError("source run finish time is required")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            run = self._connection.execute(
                "SELECT source_name, status, finished_at, receipt_json "
                "FROM source_runs WHERE id = ?",
                (source_run_id,),
            ).fetchone()
            if run is None:
                raise RepositoryError("source run does not exist")
            if run[1] == "completed":
                stored_receipt = None if run[3] is None else bytes(run[3])
                if run[2] != finished_at:
                    raise RepositoryError(
                        "completed source run has conflicting finish time"
                    )
                if stored_receipt != receipt:
                    raise RepositoryError(
                        "completed source run has conflicting receipt"
                    )
                self._connection.execute("COMMIT")
                return
            if run[1] != "running":
                raise RepositoryError("source run is not running")
            if complete:
                self._connection.execute(
                    "UPDATE jobs SET state = CASE WHEN state = 'stale' "
                    "THEN 'closed' ELSE 'stale' END "
                    "WHERE source_name = ? AND state != 'closed' AND id NOT IN "
                    "(SELECT job_id FROM source_observations WHERE source_run_id = ?)",
                    (run[0], source_run_id),
                )
            self._connection.execute(
                "UPDATE source_runs SET status = 'completed', finished_at = ?, "
                "receipt_json = ? "
                "WHERE id = ?",
                (finished_at, receipt, source_run_id),
            )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def fail_source_run(
        self,
        source_run_id: int,
        finished_at: str,
        *,
        error: str,
        receipt: bytes | None = None,
    ) -> None:
        run = self._connection.execute(
            "SELECT status, finished_at, error, receipt_json FROM source_runs "
            "WHERE id = ?",
            (source_run_id,),
        ).fetchone()
        if run is None:
            raise RepositoryError("source run does not exist")
        if run[0] == "failed":
            stored_receipt = None if run[3] is None else bytes(run[3])
            if (str(run[1]), str(run[2]), stored_receipt) != (
                finished_at,
                error,
                receipt,
            ):
                raise RepositoryError("failed source run has conflicting content")
            return
        if run[0] != "running":
            raise RepositoryError("source run is not running")
        self._connection.execute(
            "UPDATE source_runs SET status = 'failed', finished_at = ?, "
            "error = ?, receipt_json = ? WHERE id = ?",
            (finished_at, error, receipt, source_run_id),
        )

    def get_source_run(self, source_run_id: int) -> StoredSourceRun:
        row = self._connection.execute(
            "SELECT id, run_key, source_name, started_at, finished_at, status, "
            "receipt_json, error, sources_hash FROM source_runs WHERE id = ?",
            (source_run_id,),
        ).fetchone()
        if row is None:
            raise RepositoryError("source run does not exist")
        return StoredSourceRun(
            id=int(row[0]),
            run_id=str(row[1]),
            source_name=str(row[2]),
            started_at=str(row[3]),
            finished_at=None if row[4] is None else str(row[4]),
            status=cast(str, row[5]),  # type: ignore[arg-type]
            receipt=None if row[6] is None else bytes(row[6]),
            error=None if row[7] is None else str(row[7]),
            sources_hash=str(row[8]),
        )

    def get_job(self, source_name: str, source_job_id: str) -> StoredJob:
        row = self._connection.execute(
            "SELECT id, source_name, source_job_id, source_url, state, "
            "current_version_id, first_seen_at, last_seen_at FROM jobs "
            "WHERE source_name = ? AND source_job_id = ?",
            (source_name, source_job_id),
        ).fetchone()
        if row is None:
            raise RepositoryError("job does not exist")
        return StoredJob(
            id=int(row[0]),
            source_name=str(row[1]),
            source_job_id=str(row[2]),
            source_url=str(row[3]),
            state=cast(str, row[4]),  # type: ignore[arg-type]
            current_version_id=int(row[5]),
            first_seen_at=str(row[6]),
            last_seen_at=str(row[7]),
        )

    def history_counts(self) -> dict[str, int]:
        tables = ("jobs", "job_versions", "source_observations", "source_runs")
        return {
            table: int(
                self._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            )
            for table in tables
        }

    def store_profile_snapshot(
        self, profile_hash: str, snapshot_bytes: bytes, created_at: str
    ) -> None:
        self._store_snapshot(
            "profile_snapshots",
            "profile_hash",
            profile_hash,
            snapshot_bytes,
            created_at,
        )

    def store_strategy_snapshot(
        self, strategy_hash: str, snapshot_bytes: bytes, created_at: str
    ) -> None:
        self._store_snapshot(
            "strategy_snapshots",
            "strategy_hash",
            strategy_hash,
            snapshot_bytes,
            created_at,
        )

    def store_sources_snapshot(
        self, sources_hash: str, snapshot_bytes: bytes, created_at: str
    ) -> None:
        self._store_snapshot(
            "sources_snapshots",
            "sources_hash",
            sources_hash,
            snapshot_bytes,
            created_at,
        )

    def _store_snapshot(
        self,
        table: str,
        hash_column: str,
        expected_hash: str,
        snapshot_bytes: bytes,
        created_at: str,
    ) -> None:
        if hashlib.sha256(snapshot_bytes).hexdigest() != expected_hash:
            raise RepositoryError("snapshot hash does not match its content")
        existing = self._connection.execute(
            f"SELECT snapshot_bytes FROM {table} WHERE {hash_column} = ?",
            (expected_hash,),
        ).fetchone()
        if existing is not None:
            if bytes(existing[0]) != snapshot_bytes:
                raise RepositoryError("snapshot hash collision")
            return
        self._connection.execute(
            f"INSERT INTO {table}({hash_column}, snapshot_bytes, created_at) "
            "VALUES (?, ?, ?)",
            (expected_hash, snapshot_bytes, created_at),
        )

    def store_evaluation(
        self,
        job_version_id: int,
        profile_hash: str,
        strategy_hash: str,
        evaluation_bytes: bytes,
        created_at: str,
    ) -> int:
        if (
            self._connection.execute(
                "SELECT 1 FROM profile_snapshots WHERE profile_hash = ?",
                (profile_hash,),
            ).fetchone()
            is None
        ):
            raise RepositoryError("profile snapshot does not exist")
        if (
            self._connection.execute(
                "SELECT 1 FROM strategy_snapshots WHERE strategy_hash = ?",
                (strategy_hash,),
            ).fetchone()
            is None
        ):
            raise RepositoryError("strategy snapshot does not exist")
        existing = self._connection.execute(
            "SELECT id, evaluation_bytes FROM evaluations "
            "WHERE job_version_id = ? AND profile_hash = ? AND strategy_hash = ?",
            (job_version_id, profile_hash, strategy_hash),
        ).fetchone()
        if existing is not None:
            if bytes(existing[1]) != evaluation_bytes:
                raise RepositoryError("evaluation identity has conflicting content")
            return int(existing[0])
        try:
            cursor = self._connection.execute(
                "INSERT INTO evaluations(job_version_id, profile_hash, strategy_hash, "
                "evaluation_bytes, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    job_version_id,
                    profile_hash,
                    strategy_hash,
                    evaluation_bytes,
                    created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RepositoryError("job version does not exist") from exc
        return _required_lastrowid(cursor)

    def artifact_counts(self) -> dict[str, int]:
        tables = (
            "evaluations",
            "feedback",
            "llm_assessments",
            "profile_snapshots",
            "reports",
            "sources_snapshots",
            "strategy_snapshots",
        )
        counts = {
            table: int(
                self._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            )
            for table in tables
        }
        counts["sources_snapshots"] -= int(
            self._connection.execute(
                "SELECT count(*) FROM sources_snapshots WHERE sources_hash = ?",
                (_LEGACY_SOURCES_HASH,),
            ).fetchone()[0]
        )
        return counts

    def store_report(self, key: str, content: bytes, created_at: str) -> int:
        return self._store_keyed_artifact(
            table="reports",
            key_column="report_key",
            bytes_column="report_bytes",
            key=key,
            content=content,
            created_at=created_at,
        )

    def get_report(self, key: str) -> bytes:
        row = self._connection.execute(
            "SELECT report_bytes FROM reports WHERE report_key = ?", (key,)
        ).fetchone()
        if row is None:
            raise RepositoryError("report does not exist")
        return bytes(row[0])

    def delivery_chunk_acknowledged(
        self,
        report_key: str,
        report_hash: str,
        destination_id: str,
        chunk_index: int,
        chunk_hash: str,
    ) -> bool:
        return (
            self._delivery_chunk_state(
                report_key,
                report_hash,
                destination_id,
                chunk_index,
                chunk_hash,
            )
            == "acknowledged"
        )

    def delivery_chunk_indeterminate(
        self,
        report_key: str,
        report_hash: str,
        destination_id: str,
        chunk_index: int,
        chunk_hash: str,
    ) -> bool:
        return (
            self._delivery_chunk_state(
                report_key,
                report_hash,
                destination_id,
                chunk_index,
                chunk_hash,
            )
            == "indeterminate"
        )

    def _delivery_chunk_state(
        self,
        report_key: str,
        report_hash: str,
        destination_id: str,
        chunk_index: int,
        chunk_hash: str,
    ) -> str | None:
        row = self._connection.execute(
            "SELECT idempotency_state FROM delivery_attempts WHERE report_key = ? "
            "AND report_hash = ? AND destination_id = ? AND chunk_index = ? "
            "AND chunk_hash = ? ORDER BY id DESC LIMIT 1",
            (report_key, report_hash, destination_id, chunk_index, chunk_hash),
        ).fetchone()
        return None if row is None else str(row[0])

    def record_delivery_attempt(self, **values: object) -> None:
        report_key = values.get("report_key")
        report = self._connection.execute(
            "SELECT id FROM reports WHERE report_key = ?", (report_key,)
        ).fetchone()
        if report is None:
            raise RepositoryError("invalid delivery attempt evidence")
        columns = (
            "report_id",
            "report_key",
            "report_hash",
            "destination_id",
            "chunk_index",
            "chunk_count",
            "chunk_hash",
            "status",
            "attempted_at",
            "http_class",
            "idempotency_state",
        )
        try:
            self._connection.execute(
                "INSERT INTO delivery_attempts(" + ",".join(columns) + ") "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (int(report[0]), *(values[column] for column in columns[1:])),
            )
        except (KeyError, sqlite3.IntegrityError) as exc:
            raise RepositoryError("invalid delivery attempt evidence") from exc

    def store_feedback(
        self, job_id: int, key: str, content: bytes, created_at: str
    ) -> int:
        return self._store_keyed_artifact(
            table="feedback",
            key_column="feedback_key",
            bytes_column="feedback_bytes",
            key=key,
            content=content,
            created_at=created_at,
            parent_column="job_id",
            parent_id=job_id,
        )

    def store_llm_assessment(
        self, evaluation_id: int, key: str, content: bytes, created_at: str
    ) -> int:
        return self._store_keyed_artifact(
            table="llm_assessments",
            key_column="assessment_key",
            bytes_column="assessment_bytes",
            key=key,
            content=content,
            created_at=created_at,
            parent_column="evaluation_id",
            parent_id=evaluation_id,
        )

    def _store_keyed_artifact(
        self,
        *,
        table: str,
        key_column: str,
        bytes_column: str,
        key: str,
        content: bytes,
        created_at: str,
        parent_column: str | None = None,
        parent_id: int | None = None,
    ) -> int:
        identity_clause = f"{key_column} = ?"
        identity_values: tuple[object, ...] = (key,)
        if parent_column is not None:
            identity_clause = f"{parent_column} = ? AND {identity_clause}"
            identity_values = (parent_id, *identity_values)
        existing = self._connection.execute(
            f"SELECT id, {bytes_column} FROM {table} WHERE {identity_clause}",
            identity_values,
        ).fetchone()
        if existing is not None:
            if bytes(existing[1]) != content:
                raise RepositoryError(f"{table} identity has conflicting content")
            return int(existing[0])
        columns = f"{key_column}, {bytes_column}, created_at"
        placeholders = "?, ?, ?"
        values: tuple[object, ...] = (key, content, created_at)
        if parent_column is not None:
            columns = f"{parent_column}, {columns}"
            placeholders = "?, " + placeholders
            values = (parent_id, *values)
        try:
            cursor = self._connection.execute(
                f"INSERT INTO {table}({columns}) VALUES ({placeholders})",
                values,
            )
        except sqlite3.IntegrityError as exc:
            raise RepositoryError(f"{table} parent does not exist") from exc
        return _required_lastrowid(cursor)

    def close(self) -> None:
        self._connection.close()


def _prepare_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(existing.st_mode):
        raise RepositoryError("unsafe SQLite directory path")
    os.chmod(path, 0o700)


def _canonical_job_bytes(job: CanonicalJob) -> bytes:
    def encode(value: object) -> object:
        if isinstance(value, Decimal):
            return format(value, "f")
        raise TypeError(f"cannot encode {type(value).__name__}")

    return json.dumps(
        asdict(job),
        default=encode,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _required_lastrowid(cursor: sqlite3.Cursor) -> int:
    row_id = cursor.lastrowid
    if row_id is None:
        raise RepositoryError("SQLite did not return an inserted row ID")
    return row_id
