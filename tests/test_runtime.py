from __future__ import annotations

import json
import shutil
import socket
import stat
from dataclasses import dataclass
from http.client import BadStatusLine
from pathlib import Path
from typing import Never
from unittest.mock import patch

import pytest

from career_opportunity_monitor.ranking import parse_job
from career_opportunity_monitor.repository import SourceObservation
from career_opportunity_monitor.runtime import run
from career_opportunity_monitor.source import SourceError, SourceFetchResult
from career_opportunity_monitor.sqlite_repository import SQLiteRepository
from career_opportunity_monitor.workday import UrlLibTransport, WorkdaySource

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
        "configured_destinations": 1,
        "configured_sources": 2,
        "enabled_destination_ids": [],
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
    name: str = "example-workday"

    def fetch(self) -> SourceFetchResult:
        job = parse_job(
            {
                "schema_version": 1,
                "source_name": self.name,
                "source_job_id": "JR-FICTIONAL-1",
                "source_url": "https://jobs.example/JR-FICTIONAL-1",
                "company": "Example Company",
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
    name: str = "example-workday"

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


class MalformedExternalPathTransport:
    def request(self, url: str, body: bytes | None) -> bytes:
        assert body is not None
        return b'{"total":1,"jobPostings":[{"externalPath":"https://["}]}'


class BadStatusLineConnection:
    def request(
        self, method: str, path: str, body: bytes | None, headers: dict[str, str]
    ) -> None:
        return None

    def getresponse(self) -> Never:
        raise BadStatusLine("ATTACKER-CONTROLLED-STATUS-LINE")

    def close(self) -> None:
        return None


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
    source_runs = [repository.get_source_run(index) for index in (1, 2, 3)]
    assert [source_run.status for source_run in source_runs] == [
        "completed",
        "failed",
        "completed",
    ]
    assert {source_run.sources_hash for source_run in source_runs} == {
        receipt["sources_hash"]
    }
    repository.close()


def test_daily_isolates_malformed_workday_data_between_valid_sources(
    tmp_path: Path,
) -> None:
    environment = _runtime_environment(tmp_path)
    environment["CAREER_MONITOR_RUN_ID"] = "malformed-middle"
    environment["CAREER_MONITOR_NOW"] = "2026-08-27T03:30:00Z"
    malformed = WorkdaySource(
        name="malformed-workday",
        origin="https://malformed.example.com",
        tenant="malformed",
        site="ExternalCareerSite",
        company="Malformed Example",
        search_text="Taiwan",
        transport=MalformedExternalPathTransport(),
        retries=0,
    )

    exit_code = run(
        ("daily",),
        environment=environment,
        sources=(
            FixtureSource("first-workday"),
            malformed,
            FixtureSource("second-workday"),
        ),
    )

    assert exit_code == 0
    data_directory = Path(environment["CAREER_MONITOR_DATA_DIR"])
    report = (data_directory / "reports" / "daily-2026-08-27.md").read_text()
    assert report.index("- first-workday.") < report.index("- malformed-workday.")
    assert report.index("- malformed-workday.") < report.index("- second-workday.")
    receipt = json.loads(
        (data_directory / "receipts" / "daily-malformed-middle.json").read_text()
    )
    assert receipt["accepted"] == 2
    assert receipt["source_partial"] is True
    repository = SQLiteRepository(data_directory / "history.sqlite3")
    assert [repository.get_source_run(index).status for index in (1, 2, 3)] == [
        "completed",
        "completed",
        "completed",
    ]
    malformed_run = repository.get_source_run(2)
    assert malformed_run.receipt is not None
    malformed_receipt = json.loads(malformed_run.receipt)
    assert malformed_receipt["partial"] is True
    assert "unsafe externalPath" in malformed_receipt["failures"][0]
    repository.close()


def test_daily_isolates_bad_http_response_between_valid_sources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = _runtime_environment(tmp_path)
    environment["CAREER_MONITOR_RUN_ID"] = "bad-http-middle"
    environment["CAREER_MONITOR_NOW"] = "2026-08-27T03:30:00Z"
    malformed = WorkdaySource(
        name="malformed-workday",
        origin="https://malformed.example.com",
        tenant="malformed",
        site="ExternalCareerSite",
        company="Malformed Example",
        search_text="Taiwan",
        transport=UrlLibTransport(
            approved_origin="https://malformed.example.com",
            timeout_seconds=10,
            response_limit_bytes=1_000_000,
            connection_factory=lambda host, address, timeout: BadStatusLineConnection(),
        ),
        retries=0,
    )

    with patch(
        "career_opportunity_monitor.workday.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    ):
        exit_code = run(
            ("daily",),
            environment=environment,
            sources=(
                FixtureSource("first-workday"),
                malformed,
                FixtureSource("second-workday"),
            ),
        )

    assert exit_code == 0
    data_directory = Path(environment["CAREER_MONITOR_DATA_DIR"])
    report = (data_directory / "reports" / "daily-2026-08-27.md").read_text()
    assert report.index("- first-workday.") < report.index("- malformed-workday.")
    assert report.index("- malformed-workday.") < report.index("- second-workday.")
    receipt_text = (
        data_directory / "receipts" / "daily-bad-http-middle.json"
    ).read_text()
    receipt = json.loads(receipt_text)
    assert receipt["accepted"] == 2
    assert receipt["source_failures"] == 1
    repository = SQLiteRepository(data_directory / "history.sqlite3")
    source_runs = [repository.get_source_run(index) for index in (1, 2, 3)]
    assert [source_run.status for source_run in source_runs] == [
        "completed",
        "failed",
        "completed",
    ]
    malformed_run = source_runs[1]
    assert malformed_run.sources_hash == receipt["sources_hash"]
    assert malformed_run.error == (
        "source collection failed: list page 0: "
        "request failed due to invalid HTTP response"
    )
    captured = capsys.readouterr()
    assert "ATTACKER-CONTROLLED" not in "".join(
        (report, receipt_text, malformed_run.error, captured.out, captured.err)
    )
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
    assert "- example-workday. Status: complete." in report

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


def test_daily_report_id_uses_configured_local_calendar_date(tmp_path: Path) -> None:
    environment = _runtime_environment(tmp_path)
    environment["CAREER_MONITOR_RUN_ID"] = "daily-local-date"
    environment["CAREER_MONITOR_NOW"] = "2026-08-30T16:00:00Z"

    assert run(("daily",), environment=environment, source=FixtureSource()) == 0

    data_directory = Path(environment["CAREER_MONITOR_DATA_DIR"])
    report_path = data_directory / "reports" / "daily-2026-08-31.md"
    assert report_path.is_file()
    repository = SQLiteRepository(data_directory / "history.sqlite3")
    assert repository.get_report("daily:2026-08-31") == report_path.read_bytes()
    repository.close()


def test_weekly_role_aggregates_history_without_collecting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = _runtime_environment(tmp_path)
    environment["CAREER_MONITOR_RUN_ID"] = "weekly-fixture"
    environment["CAREER_MONITOR_NOW"] = "2026-08-31T01:00:00Z"
    data_directory = Path(environment["CAREER_MONITOR_DATA_DIR"])
    repository = SQLiteRepository(data_directory / "history.sqlite3")
    repository.store_report("daily:2026-08-24", b"# Fictional Monday\n", "one")
    repository.store_report("daily:2026-08-25", b"# Fictional Tuesday\n", "two")
    repository.close()

    exit_code = run(("weekly",), environment=environment, source=FailedSource())

    assert exit_code == 0
    report_path = data_directory / "reports" / "weekly-2026-08-24.md"
    report = report_path.read_text(encoding="utf-8")
    assert report.index("Fictional Monday") < report.index("Fictional Tuesday")
    receipt = json.loads(
        (data_directory / "receipts" / "weekly-weekly-fixture.json").read_text()
    )
    assert receipt["week_start"] == "2026-08-24"
    assert receipt["week_end"] == "2026-08-31"
    assert json.loads(capsys.readouterr().out)["report"] == str(report_path)


def test_weekly_role_selects_previous_local_week_across_spring_dst(
    tmp_path: Path,
) -> None:
    environment = _runtime_environment(tmp_path)
    environment["CAREER_MONITOR_RUN_ID"] = "weekly-spring-dst"
    environment["CAREER_MONITOR_NOW"] = "2026-03-09T04:00:00Z"
    schedule_path = Path(environment["CAREER_MONITOR_CONFIG_DIR"]) / "schedule.yaml"
    schedule_path.write_text(
        schedule_path.read_text(encoding="utf-8").replace(
            "timezone: Asia/Taipei", "timezone: America/New_York"
        ),
        encoding="utf-8",
    )
    data_directory = Path(environment["CAREER_MONITOR_DATA_DIR"])
    repository = SQLiteRepository(data_directory / "history.sqlite3")
    repository.store_report("daily:2026-02-23", b"wrong week\n", "one")
    repository.store_report("daily:2026-03-02", b"expected week\n", "two")
    repository.close()

    assert run(("weekly",), environment=environment) == 0

    report_path = data_directory / "reports" / "weekly-2026-03-02.md"
    assert report_path.is_file()
    assert b"expected week" in report_path.read_bytes()


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
    assert repository.get_job("example-workday", "JR-FICTIONAL-1").state == "new"
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
    assert isinstance(error["sources_hash"], str)
    assert len(error["sources_hash"]) == 64
    assert "source collection failed" in error["error"]
    assert "source collection failed" in capsys.readouterr().err
    repository = SQLiteRepository(data_directory / "history.sqlite3")
    source_run = repository.get_source_run(1)
    assert source_run.status == "failed"
    assert source_run.error == "source collection failed: temporary outage"
    assert source_run.sources_hash == error["sources_hash"]
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
