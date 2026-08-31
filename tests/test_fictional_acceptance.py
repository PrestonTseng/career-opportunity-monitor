from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from career_opportunity_monitor.ranking import parse_job
from career_opportunity_monitor.repository import SourceObservation
from career_opportunity_monitor.runtime import run
from career_opportunity_monitor.source import SourceFetchResult
from career_opportunity_monitor.sqlite_repository import SQLiteRepository

ROOT = Path(__file__).parents[1]


@dataclass
class FakeDiscordDelivery:
    report_key: str
    delivered: list[tuple[str, bytes]]

    def deliver(self, content: bytes) -> None:
        self.delivered.append((self.report_key, content))


@dataclass(frozen=True)
class FictionalWorkdaySource:
    name: str

    def fetch(self) -> SourceFetchResult:
        job = parse_job(
            {
                "schema_version": 1,
                "source_name": self.name,
                "source_job_id": "JR-FICTIONAL-1",
                "source_url": f"https://jobs.example/{self.name}/JR-FICTIONAL-1",
                "company": "Fictional Company",
                "title": "Software Engineer",
                "description_text": "Python testing and data processing",
                "location": {
                    "country_code": "TW",
                    "region": "Hsinchu",
                    "work_mode": "office",
                },
                "employment_type": "full-time",
                "posted_at": "2026-08-24T00:00:00Z",
                "compensation": None,
            }
        )
        return SourceFetchResult(
            observations=(
                SourceObservation(
                    job=job,
                    observed_at="2026-08-24T01:00:00Z",
                    raw_payload=b'{"fictional":true}',
                ),
            ),
            receipt={
                "accepted": 1,
                "failures": [],
                "listed": 1,
                "max_pages": 10,
                "max_requests": 420,
                "partial": False,
                "request_count": 2,
                "source": self.name,
                "total": 1,
            },
        )


def test_fictional_two_source_daily_weekly_retry_flow(tmp_path: Path) -> None:
    profile_directory = tmp_path / "profile"
    profile_directory.mkdir()
    shutil.copyfile(
        ROOT / "examples" / "resume_facts.yaml",
        profile_directory / "resume_facts.yaml",
    )
    config_directory = tmp_path / "config"
    shutil.copytree(ROOT / "examples" / "strategy" / "v1", config_directory)
    destinations_path = config_directory / "destinations.yaml"
    destinations_path.write_text(
        destinations_path.read_text(encoding="utf-8")
        .replace("enabled: false", "enabled: true")
        .replace("      - daily", "      - daily\n      - weekly"),
        encoding="utf-8",
    )
    data_directory = tmp_path / "data"
    data_directory.mkdir()
    environment = {
        "CAREER_MONITOR_CONFIG_DIR": str(config_directory),
        "CAREER_MONITOR_DATA_DIR": str(data_directory),
        "CAREER_MONITOR_MODE": "demo",
        "CAREER_MONITOR_RESUME_PATH": str(profile_directory / "resume_facts.yaml"),
    }
    delivered: list[tuple[str, bytes]] = []

    def fake_builder(
        *args: object, **kwargs: object
    ) -> tuple[FakeDiscordDelivery, ...]:
        report_key = kwargs["report_key"]
        assert isinstance(report_key, str)
        return (FakeDiscordDelivery(report_key, delivered),)

    sources = (
        FictionalWorkdaySource("example-workday"),
        FictionalWorkdaySource("second-example-workday"),
    )
    with patch(
        "career_opportunity_monitor.runtime.build_deliveries",
        side_effect=fake_builder,
    ):
        environment.update(
            CAREER_MONITOR_NOW="2026-08-24T01:00:00Z",
            CAREER_MONITOR_RUN_ID="fictional-daily-one",
        )
        assert run(("daily",), environment=environment, sources=sources) == 0

        environment.update(
            CAREER_MONITOR_NOW="2026-08-25T01:00:00Z",
            CAREER_MONITOR_RUN_ID="fictional-daily-two",
        )
        assert run(("daily",), environment=environment, sources=sources) == 0

        environment.update(
            CAREER_MONITOR_NOW="2026-08-31T01:00:00Z",
            CAREER_MONITOR_RUN_ID="fictional-weekly",
        )
        assert run(("weekly",), environment=environment) == 0

        environment["CAREER_MONITOR_RUN_ID"] = "fictional-weekly-retry"
        assert (
            run(
                (
                    "retry-delivery",
                    "--cadence",
                    "weekly",
                    "--report-date",
                    "2026-08-24",
                ),
                environment=environment,
            )
            == 0
        )

    repository = SQLiteRepository(data_directory / "history.sqlite3")
    assert repository.history_counts()["source_observations"] == 4
    assert repository.get_report("daily:2026-08-24")
    assert repository.get_report("daily:2026-08-25")
    stored_weekly = repository.get_report("weekly:2026-08-24")
    repository.close()

    assert [key for key, _ in delivered] == [
        "daily:2026-08-24",
        "daily:2026-08-25",
        "weekly:2026-08-24",
        "weekly:2026-08-24",
    ]
    assert delivered[-2][1] == stored_weekly
    assert delivered[-1][1] == stored_weekly
