from __future__ import annotations

import json
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from career_opportunity_monitor.ranking import parse_job
from career_opportunity_monitor.repository import SourceObservation
from career_opportunity_monitor.runtime import run
from career_opportunity_monitor.source import SourceError, SourceFetchResult
from career_opportunity_monitor.sqlite_repository import SQLiteRepository

ROOT = Path(__file__).parents[1]


def _runtime_environment(tmp_path: Path) -> dict[str, str]:
    profile_directory = tmp_path / "profile"
    profile_directory.mkdir()
    shutil.copyfile(
        ROOT / "examples" / "resume_facts.yaml",
        profile_directory / "resume_facts.yaml",
    )
    config_directory = tmp_path / "config"
    shutil.copytree(ROOT / "examples" / "strategy" / "v1", config_directory)
    data_directory = tmp_path / "data"
    data_directory.mkdir()
    return {
        "CAREER_MONITOR_RESUME_PATH": str(profile_directory / "resume_facts.yaml"),
        "CAREER_MONITOR_CONFIG_DIR": str(config_directory),
        "CAREER_MONITOR_DATA_DIR": str(data_directory),
    }


def test_validate_accepts_valid_runtime_mounts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = _runtime_environment(tmp_path)

    exit_code = run(("validate",), environment=environment)

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "command": "validate",
        "configured_sources": 2,
        "enabled_source_ids": ["example-workday"],
        "profile_id": "alex-chen-fictional",
        "status": "ok",
        "strategy_id": "taiwan-software-fictional",
    }


def test_daily_dry_run_writes_a_quiet_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = _runtime_environment(tmp_path)
    environment["CAREER_MONITOR_RUN_ID"] = "daily-fixture"
    environment["CAREER_MONITOR_NOW"] = "2026-08-27T03:30:00Z"

    exit_code = run(("daily", "--dry-run"), environment=environment)

    assert exit_code == 0
    receipt_path = (
        Path(environment["CAREER_MONITOR_DATA_DIR"])
        / "receipts"
        / "daily-daily-fixture.json"
    )
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == {
        "command": "daily",
        "dry_run": True,
        "finished_at": "2026-08-27T03:30:00Z",
        "quiet": True,
        "run_id": "daily-fixture",
        "status": "completed",
    }
    assert json.loads(capsys.readouterr().out) == {
        "command": "daily",
        "receipt": str(receipt_path),
        "status": "completed",
    }


@dataclass(frozen=True)
class FixtureSource:
    name: str = "nvidia-workday"

    def fetch(self) -> SourceFetchResult:
        job = parse_job(
            {
                "schema_version": 1,
                "source_name": self.name,
                "source_job_id": "JR-FICTIONAL-1",
                "source_url": "https://jobs.example/JR-FICTIONAL-1",
                "company": "NVIDIA",
                "title": "Software Engineer",
                "description_text": "Python testing and data processing",
                "location": {
                    "country_code": "TW",
                    "region": "Hsinchu",
                    "work_mode": "office",
                },
                "employment_type": "full-time",
                "posted_at": "2026-08-26T00:00:00Z",
                "compensation": None,
            }
        )
        observation = SourceObservation(
            job=job,
            observed_at="2026-08-27T03:30:00Z",
            raw_payload=b'{"fixture":true}',
        )
        return SourceFetchResult(
            observations=(observation,),
            receipt={
                "source": self.name,
                "listed": 1,
                "accepted": 1,
                "request_count": 2,
                "max_pages": 10,
                "max_requests": 200,
                "failures": [],
                "partial": False,
                "total": 1,
            },
        )


@dataclass(frozen=True)
class EmptyPartialSource:
    name: str = "nvidia-workday"

    def fetch(self) -> SourceFetchResult:
        return SourceFetchResult(
            observations=(),
            receipt={
                "source": self.name,
                "failures": ["one detail failed"],
                "partial": True,
            },
        )


@dataclass(frozen=True)
class FailedSource:
    name: str = "failed-workday"

    def fetch(self) -> SourceFetchResult:
        raise SourceError("source collection failed: temporary outage")


def test_daily_collects_configured_sources_in_order_and_isolates_one_failure(
    tmp_path: Path,
) -> None:
    environment = _runtime_environment(tmp_path)
    environment["CAREER_MONITOR_RUN_ID"] = "multi-source"
    environment["CAREER_MONITOR_NOW"] = "2026-08-27T03:30:00Z"

    exit_code = run(
        ("daily",),
        environment=environment,
        sources=(
            FixtureSource("first-workday"),
            FailedSource(),
            FixtureSource("second-workday"),
        ),
    )

    assert exit_code == 0
    data_directory = Path(environment["CAREER_MONITOR_DATA_DIR"])
    report = (data_directory / "reports" / "daily-2026-08-27.md").read_text()
    assert report.index("- first-workday.") < report.index("- failed-workday.")
    assert report.index("- failed-workday.") < report.index("- second-workday.")
    receipt = json.loads(
        (data_directory / "receipts" / "daily-multi-source.json").read_text()
    )
    assert receipt["accepted"] == 2
    assert receipt["source_failures"] == 1
    repository = SQLiteRepository(data_directory / "history.sqlite3")
    assert [repository.get_source_run(index).status for index in (1, 2, 3)] == [
        "completed",
        "failed",
        "completed",
    ]
    repository.close()


def test_daily_live_run_collects_scores_and_writes_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = _runtime_environment(tmp_path)
    environment["CAREER_MONITOR_RUN_ID"] = "daily-live-fixture"
    environment["CAREER_MONITOR_NOW"] = "2026-08-27T03:30:00Z"

    exit_code = run(("daily",), environment=environment, source=FixtureSource())

    assert exit_code == 0
    data_directory = Path(environment["CAREER_MONITOR_DATA_DIR"])
    report_path = data_directory / "reports" / "daily-2026-08-27.md"
    report = report_path.read_text(encoding="utf-8")
    assert "Receipt: The run found 1 new or changed job." in report
    assert "Official link: https://jobs.example/JR-FICTIONAL-1" in report
    assert "Base score: 77.50." in report
    assert "LLM adjustment: +0 (off)." in report
    assert "Evidence IDs: fact-python-services." in report
    assert "Omitted jobs: 0." in report
    assert "- nvidia-workday. Status: complete." in report

    receipt_path = data_directory / "receipts" / "daily-daily-live-fixture.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["accepted"] == 1
    assert receipt["evaluated"] == 1
    assert receipt["new_or_changed"] == 1
    assert receipt["report"] == str(report_path)
    assert json.loads(capsys.readouterr().out)["report"] == str(report_path)
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(report_path.parent.stat().st_mode) == 0o700
    database_path = data_directory / "history.sqlite3"
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600

    repository = SQLiteRepository(database_path)
    assert repository.history_counts()["source_observations"] == 1
    assert repository.artifact_counts() == {
        "evaluations": 1,
        "feedback": 0,
        "llm_assessments": 1,
        "profile_snapshots": 1,
        "reports": 1,
        "sources_snapshots": 1,
        "strategy_snapshots": 1,
    }
    repository.close()


def test_weekly_role_is_rejected_without_a_completed_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = _runtime_environment(tmp_path)
    environment["CAREER_MONITOR_RUN_ID"] = "weekly-fixture"
    environment["CAREER_MONITOR_NOW"] = "2026-08-27T03:35:00Z"
    data_directory = Path(environment["CAREER_MONITOR_DATA_DIR"])

    exit_code = run(("weekly",), environment=environment)

    assert exit_code == 2
    assert not (data_directory / "receipts" / "weekly-weekly-fixture.json").exists()
    error_path = data_directory / "errors" / "weekly-weekly-fixture.json"
    error = json.loads(error_path.read_text(encoding="utf-8"))
    assert error["command"] == "weekly"
    assert error["failed_at"] == "2026-08-27T03:35:00Z"
    assert error["run_id"] == "weekly-fixture"
    assert error["status"] == "failed"
    assert "weekly maintenance is not implemented" in error["error"]
    assert "weekly maintenance is not implemented" in capsys.readouterr().err


def test_partial_daily_run_does_not_age_unseen_jobs(tmp_path: Path) -> None:
    environment = _runtime_environment(tmp_path)
    environment["CAREER_MONITOR_RUN_ID"] = "complete"
    environment["CAREER_MONITOR_NOW"] = "2026-08-26T03:30:00Z"
    assert run(("daily",), environment=environment, source=FixtureSource()) == 0

    environment["CAREER_MONITOR_RUN_ID"] = "partial"
    environment["CAREER_MONITOR_NOW"] = "2026-08-27T03:30:00Z"
    assert run(("daily",), environment=environment, source=EmptyPartialSource()) == 0

    repository = SQLiteRepository(
        Path(environment["CAREER_MONITOR_DATA_DIR"]) / "history.sqlite3"
    )
    assert repository.get_job("nvidia-workday", "JR-FICTIONAL-1").state == "new"
    repository.close()


def test_total_source_outage_exits_nonzero_and_keeps_failed_run_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = _runtime_environment(tmp_path)
    environment["CAREER_MONITOR_RUN_ID"] = "source-outage"
    environment["CAREER_MONITOR_NOW"] = "2026-08-27T03:30:00Z"

    exit_code = run(("daily",), environment=environment, source=FailedSource())

    assert exit_code == 1
    data_directory = Path(environment["CAREER_MONITOR_DATA_DIR"])
    assert not (data_directory / "receipts" / "daily-source-outage.json").exists()
    error = json.loads(
        (data_directory / "errors" / "daily-source-outage.json").read_text(
            encoding="utf-8"
        )
    )
    assert error["status"] == "failed"
    assert "source collection failed" in error["error"]
    assert "source collection failed" in capsys.readouterr().err
    repository = SQLiteRepository(data_directory / "history.sqlite3")
    source_run = repository.get_source_run(1)
    assert source_run.status == "failed"
    assert source_run.error == "source collection failed: temporary outage"
    repository.close()


def test_invalid_resume_mount_keeps_error_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = _runtime_environment(tmp_path)
    environment["CAREER_MONITOR_RESUME_PATH"] = str(tmp_path / "missing.yaml")
    environment["CAREER_MONITOR_RUN_ID"] = "invalid-mount"
    environment["CAREER_MONITOR_NOW"] = "2026-08-27T03:40:00Z"
    data_directory = Path(environment["CAREER_MONITOR_DATA_DIR"])

    exit_code = run(("daily", "--dry-run"), environment=environment)

    assert exit_code == 2
    error_path = data_directory / "errors" / "daily-invalid-mount.json"
    error = json.loads(error_path.read_text(encoding="utf-8"))
    assert error["command"] == "daily"
    assert error["failed_at"] == "2026-08-27T03:40:00Z"
    assert error["run_id"] == "invalid-mount"
    assert error["status"] == "failed"
    assert "cannot inspect configuration file" in error["error"]
    assert "runtime validation failed" in capsys.readouterr().err


def test_daily_delivery_failure_exits_nonzero_and_keeps_error_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = _runtime_environment(tmp_path)
    environment["CAREER_MONITOR_RUN_ID"] = "delivery-failure"
    environment["CAREER_MONITOR_NOW"] = "2026-08-27T03:45:00Z"
    data_directory = Path(environment["CAREER_MONITOR_DATA_DIR"])
    (data_directory / "reports").write_text("blocked", encoding="utf-8")

    exit_code = run(("daily",), environment=environment, source=FixtureSource())

    assert exit_code == 1
    error_path = data_directory / "errors" / "daily-delivery-failure.json"
    error = json.loads(error_path.read_text(encoding="utf-8"))
    assert error["status"] == "failed"
    assert "delivery failed" in error["error"]
    assert "delivery failed" in capsys.readouterr().err

    (data_directory / "reports").unlink()
    environment["CAREER_MONITOR_RUN_ID"] = "delivery-retry"
    retry_exit = run(
        ("retry-delivery", "--report-date", "2026-08-27"), environment=environment
    )

    assert retry_exit == 0
    report_path = data_directory / "reports" / "daily-2026-08-27.md"
    assert report_path.is_file()
    assert b"# Daily career report: 2026-08-27" in report_path.read_bytes()
    retry_receipt = json.loads(
        (data_directory / "receipts" / "retry-delivery-delivery-retry.json").read_text(
            encoding="utf-8"
        )
    )
    assert retry_receipt["status"] == "completed"
