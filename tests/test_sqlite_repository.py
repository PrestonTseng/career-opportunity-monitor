from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import cast

import pytest
import yaml

import career_opportunity_monitor.sqlite_repository as sqlite_repository_module
from career_opportunity_monitor.config import load_configuration
from career_opportunity_monitor.ranking import parse_job
from career_opportunity_monitor.repository import (
    Repository,
    RepositoryError,
    RepositoryLockedError,
    SourceObservation,
)
from career_opportunity_monitor.sqlite_repository import SQLiteRepository

EXPECTED_TABLES = {
    "delivery_attempts",
    "evaluations",
    "feedback",
    "job_versions",
    "jobs",
    "llm_assessments",
    "profile_snapshots",
    "reports",
    "schema_migrations",
    "source_observations",
    "source_runs",
    "sources_snapshots",
    "strategy_snapshots",
    "writer_lock",
}

ROOT = Path(__file__).parents[1]


def _create_legacy_database(path: Path, version: int) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        sqlite_repository_module._MIGRATION_1  # pyright: ignore[reportPrivateUsage]
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
        ("2026-08-26T00:00:00Z",),
    )
    if version == 2:
        connection.executescript(
            sqlite_repository_module._MIGRATION_2  # pyright: ignore[reportPrivateUsage]
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)",
            ("2026-08-26T00:01:00Z",),
        )
    connection.execute(f"PRAGMA user_version = {version}")
    connection.execute(
        "INSERT INTO source_runs"
        "(run_key, source_name, started_at, status) VALUES (?, ?, ?, 'failed')",
        ("legacy-run", "legacy-source", "2026-08-26T00:02:00Z"),
    )
    connection.commit()
    connection.close()


def _assert_legacy_database_migrates_to_latest(path: Path) -> None:
    repository = SQLiteRepository(path)

    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone() == (5,)
    sources_hash_column = next(
        row
        for row in connection.execute("PRAGMA table_info(source_runs)")
        if row[1] == "sources_hash"
    )
    assert sources_hash_column[3] == 1
    assert connection.execute(
        "SELECT count(*) FROM source_runs AS run "
        "JOIN sources_snapshots AS snapshot "
        "ON snapshot.sources_hash = run.sources_hash"
    ).fetchone() == (1,)
    connection.close()
    assert repository.integrity_check() == "ok"
    assert repository.foreign_key_check() == ()
    assert repository.get_source_run(1).sources_hash
    repository.close()


def test_open_migrates_version_1_database_to_latest_with_source_correlation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    _create_legacy_database(path, 1)

    _assert_legacy_database_migrates_to_latest(path)


def test_open_migrates_version_2_database_to_latest_with_source_correlation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    _create_legacy_database(path, 2)

    _assert_legacy_database_migrates_to_latest(path)


def _observation(
    *,
    observed_at: str,
    title: str = "Software Engineer",
    source_name: str = "fictional-workday",
    source_job_id: str = "example-001",
) -> SourceObservation:
    value = yaml.safe_load((ROOT / "examples" / "job.yaml").read_text())
    assert isinstance(value, dict)
    raw = cast(dict[str, object], value)
    raw["title"] = title
    raw["source_name"] = source_name
    raw["source_job_id"] = source_job_id
    return SourceObservation(
        job=parse_job(raw),
        observed_at=observed_at,
        raw_payload=(
            f'{{"source_job_id":"{source_job_id}","title":"{title}"}}'
        ).encode(),
    )


def test_open_migrates_complete_history_schema_and_passes_integrity_check(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")

    assert repository.table_names() == EXPECTED_TABLES
    assert repository.integrity_check() == "ok"
    assert repository.foreign_key_check() == ()
    assert repository.foreign_keys_enabled
    assert isinstance(repository, Repository)

    repository.close()


def test_open_tightens_existing_private_directory_and_database_modes(
    tmp_path: Path,
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o755)
    database_path = private_directory / "history.sqlite3"
    database_path.touch(mode=0o644)
    lock_path = Path(f"{database_path}.lock")
    lock_path.touch(mode=0o644)

    repository = SQLiteRepository(database_path)
    with repository.writer_lock("mode-test"):
        pass

    assert stat.S_IMODE(private_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    repository.close()


def test_open_rejects_a_symlinked_private_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    private_directory = tmp_path / "private"
    private_directory.symlink_to(target, target_is_directory=True)

    with pytest.raises(RepositoryError, match="unsafe SQLite directory path"):
        SQLiteRepository(private_directory / "history.sqlite3")


def test_schema_rejects_current_version_owned_by_a_different_job(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    repository = SQLiteRepository(path)
    source_run = repository.start_source_run(
        "fictional-workday", "2026-08-27T00:00:00Z", run_id="run-1"
    )
    first, second = repository.record_observations(
        source_run,
        (
            _observation(
                observed_at="2026-08-27T00:01:00Z", source_job_id="example-001"
            ),
            _observation(
                observed_at="2026-08-27T00:01:00Z", source_job_id="example-002"
            ),
        ),
    )
    repository.close()

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE jobs SET current_version_id = ? WHERE id = ?",
            (second.job_version_id, first.job_id),
        )
    connection.close()


def test_repeated_source_identity_and_content_are_idempotent(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    first_run = repository.start_source_run(
        "fictional-workday", "2026-08-27T00:00:00Z", run_id="run-1"
    )
    observation = _observation(observed_at="2026-08-27T00:01:00Z")

    first = repository.record_observations(first_run, (observation,))
    repeated = repository.record_observations(first_run, (observation,))
    repository.complete_source_run(first_run, "2026-08-27T00:02:00Z")

    second_run = repository.start_source_run(
        "fictional-workday", "2026-08-28T00:00:00Z", run_id="run-2"
    )
    second = repository.record_observations(
        second_run,
        (_observation(observed_at="2026-08-28T00:01:00Z"),),
    )
    repository.complete_source_run(second_run, "2026-08-28T00:02:00Z")

    assert first == repeated
    assert first[0].outcome == "new"
    assert second[0].outcome == "unchanged"
    assert repository.history_counts() == {
        "jobs": 1,
        "job_versions": 1,
        "source_observations": 2,
        "source_runs": 2,
    }
    assert repository.get_job("fictional-workday", "example-001").state == "unchanged"

    repository.close()


def test_changed_canonical_content_creates_a_new_version(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    first_run = repository.start_source_run(
        "fictional-workday", "2026-08-27T00:00:00Z", run_id="run-1"
    )
    repository.record_observations(
        first_run,
        (_observation(observed_at="2026-08-27T00:01:00Z"),),
    )
    repository.complete_source_run(first_run, "2026-08-27T00:02:00Z")

    second_run = repository.start_source_run(
        "fictional-workday", "2026-08-28T00:00:00Z", run_id="run-2"
    )
    changed = repository.record_observations(
        second_run,
        (
            _observation(
                observed_at="2026-08-28T00:01:00Z",
                title="Senior Software Engineer",
            ),
        ),
    )

    assert changed[0].outcome == "changed"
    assert repository.history_counts()["job_versions"] == 2
    assert repository.get_job("fictional-workday", "example-001").state == "changed"

    repository.close()


def test_repeated_source_evidence_rejects_conflicting_canonical_content(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    source_run = repository.start_source_run(
        "fictional-workday", "2026-08-27T00:00:00Z", run_id="run-1"
    )
    first = _observation(observed_at="2026-08-27T00:01:00Z")
    conflicting = _observation(
        observed_at="2026-08-27T00:01:00Z", title="Senior Software Engineer"
    )
    conflicting = SourceObservation(
        job=conflicting.job,
        observed_at=conflicting.observed_at,
        raw_payload=first.raw_payload,
    )
    repository.record_observations(source_run, (first,))

    with pytest.raises(RepositoryError, match="conflicting canonical content"):
        repository.record_observations(source_run, (conflicting,))

    assert repository.history_counts() == {
        "jobs": 1,
        "job_versions": 1,
        "source_observations": 1,
        "source_runs": 1,
    }
    assert repository.get_job("fictional-workday", "example-001").state == "new"

    repository.close()


def test_repeated_source_evidence_rejects_conflicting_observation_time(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    source_run = repository.start_source_run(
        "fictional-workday", "2026-08-27T00:00:00Z", run_id="run-1"
    )
    first = _observation(observed_at="2026-08-27T00:01:00Z")
    conflicting = SourceObservation(
        job=first.job,
        observed_at="2026-08-27T00:02:00Z",
        raw_payload=first.raw_payload,
    )
    repository.record_observations(source_run, (first,))

    with pytest.raises(RepositoryError, match="conflicting observation time"):
        repository.record_observations(source_run, (conflicting,))

    assert repository.history_counts()["source_observations"] == 1
    assert repository.get_job("fictional-workday", "example-001").last_seen_at == (
        "2026-08-27T00:01:00Z"
    )

    repository.close()


def test_two_completed_runs_without_a_job_make_it_stale_then_closed(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    initial = repository.start_source_run(
        "fictional-workday", "2026-08-27T00:00:00Z", run_id="initial"
    )
    repository.record_observations(
        initial,
        (_observation(observed_at="2026-08-27T00:01:00Z"),),
    )
    repository.complete_source_run(initial, "2026-08-27T00:02:00Z")

    missing_once = repository.start_source_run(
        "fictional-workday", "2026-08-28T00:00:00Z", run_id="missing-1"
    )
    repository.complete_source_run(missing_once, "2026-08-28T00:02:00Z")
    assert repository.get_job("fictional-workday", "example-001").state == "stale"

    missing_twice = repository.start_source_run(
        "fictional-workday", "2026-08-29T00:00:00Z", run_id="missing-2"
    )
    repository.complete_source_run(missing_twice, "2026-08-29T00:02:00Z")
    assert repository.get_job("fictional-workday", "example-001").state == "closed"

    repository.close()


def test_incomplete_run_never_ages_unseen_jobs(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    initial = repository.start_source_run(
        "fictional-workday", "2026-08-27T00:00:00Z", run_id="initial"
    )
    repository.record_observations(
        initial,
        (_observation(observed_at="2026-08-27T00:01:00Z"),),
    )
    repository.complete_source_run(initial, "2026-08-27T00:02:00Z")
    incomplete = repository.start_source_run(
        "fictional-workday", "2026-08-28T00:00:00Z", run_id="incomplete"
    )

    repository.complete_source_run(incomplete, "2026-08-28T00:02:00Z", complete=False)

    assert repository.get_job("fictional-workday", "example-001").state == "new"
    repository.close()


def test_invalid_batch_rolls_back_every_observation(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    source_run = repository.start_source_run(
        "fictional-workday", "2026-08-27T00:00:00Z", run_id="run-1"
    )

    with pytest.raises(RepositoryError, match="does not match"):
        repository.record_observations(
            source_run,
            (
                _observation(observed_at="2026-08-27T00:01:00Z"),
                _observation(
                    observed_at="2026-08-27T00:02:00Z",
                    source_name="different-source",
                ),
            ),
        )

    assert repository.history_counts() == {
        "jobs": 0,
        "job_versions": 0,
        "source_observations": 0,
        "source_runs": 1,
    }
    assert repository.integrity_check() == "ok"

    repository.close()


def test_invalid_completion_rolls_back_job_aging_and_run_status(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    initial = repository.start_source_run(
        "fictional-workday", "2026-08-27T00:00:00Z", run_id="initial"
    )
    repository.record_observations(
        initial,
        (_observation(observed_at="2026-08-27T00:01:00Z"),),
    )
    repository.complete_source_run(initial, "2026-08-27T00:02:00Z")
    invalid = repository.start_source_run(
        "fictional-workday", "2026-08-28T00:00:00Z", run_id="invalid"
    )

    with pytest.raises(RepositoryError, match="finish time is required"):
        repository.complete_source_run(invalid, "")

    assert repository.get_source_run(invalid).status == "running"
    assert repository.get_job("fictional-workday", "example-001").state == "new"

    repository.close()


def test_repeated_completion_rejects_conflicting_finish_time(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    source_run = repository.start_source_run(
        "fictional-workday", "2026-08-27T00:00:00Z", run_id="run-1"
    )
    repository.complete_source_run(source_run, "2026-08-27T00:02:00Z")

    with pytest.raises(RepositoryError, match="conflicting finish time"):
        repository.complete_source_run(source_run, "2026-08-27T00:03:00Z")

    assert repository.get_source_run(source_run).finished_at == "2026-08-27T00:02:00Z"

    repository.close()


def test_failed_source_run_records_receipt_without_aging_jobs(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    initial = repository.start_source_run(
        "fictional-workday", "2026-08-27T00:00:00Z", run_id="initial"
    )
    repository.record_observations(
        initial,
        (_observation(observed_at="2026-08-27T00:01:00Z"),),
    )
    repository.complete_source_run(initial, "2026-08-27T00:02:00Z")
    failed = repository.start_source_run(
        "fictional-workday", "2026-08-28T00:00:00Z", run_id="failed"
    )

    repository.fail_source_run(
        failed,
        "2026-08-28T00:02:00Z",
        error="detail request failed",
        receipt=b'{"partial":true}',
    )

    stored_run = repository.get_source_run(failed)
    assert stored_run.status == "failed"
    assert stored_run.error == "detail request failed"
    assert stored_run.receipt == b'{"partial":true}'
    assert repository.get_job("fictional-workday", "example-001").state == "new"

    repository.close()


def test_writer_lock_collision_is_bounded_and_release_allows_next_writer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    scheduler = SQLiteRepository(path)
    manual = SQLiteRepository(path)

    with scheduler.writer_lock("scheduler", lease_seconds=1.0):
        started = time.monotonic()
        with (
            pytest.raises(RepositoryLockedError, match="within"),
            manual.writer_lock("manual", timeout_seconds=0.05),
        ):
            pass
        assert time.monotonic() - started < 0.3

    with manual.writer_lock("manual", timeout_seconds=0.05):
        pass

    scheduler.close()
    manual.close()


@pytest.mark.parametrize(
    ("timeout_seconds", "lease_seconds", "poll_seconds"),
    [
        (float("nan"), 1.0, 0.01),
        (float("inf"), 1.0, 0.01),
        (0.25, float("nan"), 0.01),
        (0.25, float("inf"), 0.01),
        (0.25, 1.0, float("nan")),
        (0.25, 1.0, float("inf")),
    ],
)
def test_writer_lock_rejects_non_finite_timing(
    tmp_path: Path,
    timeout_seconds: float,
    lease_seconds: float,
    poll_seconds: float,
) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")

    with (
        pytest.raises(ValueError, match="timing"),
        repository.writer_lock(
            "scheduler",
            timeout_seconds=timeout_seconds,
            lease_seconds=lease_seconds,
            poll_seconds=poll_seconds,
        ),
    ):
        pass

    repository.close()


def test_writer_lock_remains_exclusive_past_evidence_lease(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    scheduler = SQLiteRepository(path)
    manual = SQLiteRepository(path)

    with scheduler.writer_lock("scheduler", lease_seconds=0.05):
        time.sleep(0.08)
        with (
            pytest.raises(RepositoryLockedError),
            manual.writer_lock("manual", timeout_seconds=0.03),
        ):
            pass

    scheduler.close()
    manual.close()


def test_writer_lock_does_not_depend_on_evidence_renewal(tmp_path: Path) -> None:
    class RenewalFailureRepository(SQLiteRepository):
        def _renew_writer_lock(
            self,
            lock_owner: str,
            lease_seconds: float,
            poll_seconds: float,
            stop: threading.Event,
        ) -> None:
            return

    path = tmp_path / "history.sqlite3"
    scheduler = RenewalFailureRepository(path)
    manual = SQLiteRepository(path)

    with scheduler.writer_lock("scheduler", lease_seconds=0.01):
        time.sleep(0.02)
        with (
            pytest.raises(RepositoryLockedError),
            manual.writer_lock("manual", timeout_seconds=0.02),
        ):
            pass

    scheduler.close()
    manual.close()


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="no no-follow open flag")
def test_writer_lock_refuses_a_symlink_lock_path(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    repository = SQLiteRepository(path)
    victim = tmp_path / "victim"
    victim.write_text("unchanged")
    Path(f"{path}.lock").symlink_to(victim)

    with (
        pytest.raises(OSError),
        repository.writer_lock("scheduler", timeout_seconds=0),
    ):
        pass

    assert victim.read_text() == "unchanged"
    repository.close()


def test_writer_lock_recovers_stale_evidence_after_holder_crash(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    SQLiteRepository(path).close()
    script = """
import os
import sys

from career_opportunity_monitor.sqlite_repository import SQLiteRepository

repository = SQLiteRepository(sys.argv[1])
with repository.writer_lock("crasher", lease_seconds=60):
    os._exit(0)
"""

    crashed = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=False,
        timeout=5,
    )
    assert crashed.returncode == 0

    repository = SQLiteRepository(path)
    with repository.writer_lock("recovery", timeout_seconds=0.05):
        evidence_connection = sqlite3.connect(path)
        evidence = evidence_connection.execute(
            "SELECT owner FROM writer_lock WHERE lock_name = 'history-writer'"
        ).fetchone()
        evidence_connection.close()
        assert evidence is not None
        assert str(evidence[0]).startswith("recovery:")
    assert Path(f"{path}.lock").is_file()
    repository.close()


def test_evaluation_is_idempotent_and_bound_to_exact_snapshots(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    source_run = repository.start_source_run(
        "fictional-workday", "2026-08-27T00:00:00Z", run_id="run-1"
    )
    observed = repository.record_observations(
        source_run,
        (_observation(observed_at="2026-08-27T00:01:00Z"),),
    )[0]
    profile = b"canonical-profile"
    strategy = b"canonical-strategy"
    profile_hash = hashlib.sha256(profile).hexdigest()
    strategy_hash = hashlib.sha256(strategy).hexdigest()
    repository.store_profile_snapshot(profile_hash, profile, "2026-08-27T00:02:00Z")
    repository.store_strategy_snapshot(strategy_hash, strategy, "2026-08-27T00:02:00Z")

    first = repository.store_evaluation(
        observed.job_version_id,
        profile_hash,
        strategy_hash,
        b'{"base_score":"80.00"}',
        "2026-08-27T00:03:00Z",
    )
    repeated = repository.store_evaluation(
        observed.job_version_id,
        profile_hash,
        strategy_hash,
        b'{"base_score":"80.00"}',
        "2026-08-27T00:03:00Z",
    )

    assert first == repeated
    assert repository.artifact_counts() == {
        "evaluations": 1,
        "feedback": 0,
        "llm_assessments": 0,
        "profile_snapshots": 1,
        "reports": 0,
        "sources_snapshots": 0,
        "strategy_snapshots": 1,
    }
    with pytest.raises(RepositoryError, match="profile snapshot"):
        repository.store_evaluation(
            observed.job_version_id,
            "0" * 64,
            strategy_hash,
            b"{}",
            "2026-08-27T00:04:00Z",
        )

    repository.close()


def test_snapshots_accept_configuration_hash_domain(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    resume_path = ROOT / "examples" / "resume_facts.yaml"
    strategy_path = ROOT / "examples" / "strategy" / "v1" / "strategy.yaml"
    configuration = load_configuration(resume_path, strategy_path.parent)

    repository.store_profile_snapshot(
        configuration.profile_hash,
        configuration.profile_snapshot_bytes,
        "2026-08-27T00:00:00Z",
    )
    repository.store_strategy_snapshot(
        configuration.strategy_hash,
        configuration.strategy_snapshot_bytes,
        "2026-08-27T00:00:00Z",
    )
    repository.store_sources_snapshot(
        configuration.sources_hash,
        configuration.sources_snapshot_bytes,
        "2026-08-27T00:00:00Z",
    )

    assert repository.artifact_counts()["profile_snapshots"] == 1
    assert repository.artifact_counts()["strategy_snapshots"] == 1
    assert "sources_snapshots" in repository.table_names()

    repository.close()


def test_snapshot_storage_rejects_hash_content_mismatch(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")

    with pytest.raises(RepositoryError, match="snapshot hash does not match"):
        repository.store_profile_snapshot(
            hashlib.sha256(b"validated").hexdigest(),
            b"mutated",
            "2026-08-27T00:00:00Z",
        )

    assert repository.artifact_counts()["profile_snapshots"] == 0
    repository.close()


def test_reports_feedback_and_optional_assessments_are_idempotent(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    source_run = repository.start_source_run(
        "fictional-workday", "2026-08-27T00:00:00Z", run_id="run-1"
    )
    observed = repository.record_observations(
        source_run,
        (_observation(observed_at="2026-08-27T00:01:00Z"),),
    )[0]
    profile = b"canonical-profile"
    strategy = b"canonical-strategy"
    profile_hash = hashlib.sha256(profile).hexdigest()
    strategy_hash = hashlib.sha256(strategy).hexdigest()
    repository.store_profile_snapshot(profile_hash, profile, "2026-08-27T00:02:00Z")
    repository.store_strategy_snapshot(strategy_hash, strategy, "2026-08-27T00:02:00Z")
    evaluation_id = repository.store_evaluation(
        observed.job_version_id,
        profile_hash,
        strategy_hash,
        b"{}",
        "2026-08-27T00:03:00Z",
    )

    report_id = repository.store_report(
        "daily:2026-08-27", b"report", "2026-08-27T00:04:00Z"
    )
    feedback_id = repository.store_feedback(
        observed.job_id, "decision:example-001", b"interested", "2026-08-27T00:05:00Z"
    )
    assessment_id = repository.store_llm_assessment(
        evaluation_id, "provider:model:prompt-v1", b"assessment", "2026-08-27T00:06:00Z"
    )

    assert (
        repository.store_report("daily:2026-08-27", b"report", "2026-08-27T00:04:00Z")
        == report_id
    )
    assert (
        repository.store_feedback(
            observed.job_id,
            "decision:example-001",
            b"interested",
            "2026-08-27T00:05:00Z",
        )
        == feedback_id
    )
    assert (
        repository.store_llm_assessment(
            evaluation_id,
            "provider:model:prompt-v1",
            b"assessment",
            "2026-08-27T00:06:00Z",
        )
        == assessment_id
    )
    assert repository.artifact_counts() == {
        "evaluations": 1,
        "feedback": 1,
        "llm_assessments": 1,
        "profile_snapshots": 1,
        "reports": 1,
        "sources_snapshots": 0,
        "strategy_snapshots": 1,
    }

    repository.close()


def test_parent_scoped_artifact_keys_do_not_alias_different_parents(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    source_run = repository.start_source_run(
        "fictional-workday", "2026-08-27T00:00:00Z", run_id="run-1"
    )
    first, second = repository.record_observations(
        source_run,
        (
            _observation(
                observed_at="2026-08-27T00:01:00Z", source_job_id="example-001"
            ),
            _observation(
                observed_at="2026-08-27T00:01:00Z", source_job_id="example-002"
            ),
        ),
    )
    profile = b"canonical-profile"
    strategy = b"canonical-strategy"
    profile_hash = hashlib.sha256(profile).hexdigest()
    strategy_hash = hashlib.sha256(strategy).hexdigest()
    repository.store_profile_snapshot(profile_hash, profile, "2026-08-27T00:02:00Z")
    repository.store_strategy_snapshot(strategy_hash, strategy, "2026-08-27T00:02:00Z")
    first_evaluation = repository.store_evaluation(
        first.job_version_id,
        profile_hash,
        strategy_hash,
        b"{}",
        "2026-08-27T00:03:00Z",
    )
    second_evaluation = repository.store_evaluation(
        second.job_version_id,
        profile_hash,
        strategy_hash,
        b"{}",
        "2026-08-27T00:03:00Z",
    )

    first_feedback = repository.store_feedback(
        first.job_id, "decision", b"interested", "2026-08-27T00:04:00Z"
    )
    second_feedback = repository.store_feedback(
        second.job_id, "decision", b"interested", "2026-08-27T00:04:00Z"
    )
    first_assessment = repository.store_llm_assessment(
        first_evaluation, "provider:model:prompt-v1", b"good", "2026-08-27T00:05:00Z"
    )
    second_assessment = repository.store_llm_assessment(
        second_evaluation,
        "provider:model:prompt-v1",
        b"good",
        "2026-08-27T00:05:00Z",
    )

    assert first_feedback != second_feedback
    assert first_assessment != second_assessment
    assert repository.artifact_counts()["feedback"] == 2
    assert repository.artifact_counts()["llm_assessments"] == 2

    repository.close()
