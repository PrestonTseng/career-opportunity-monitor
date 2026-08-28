from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from career_opportunity_monitor.reporting import (
    DeliveryError,
    WeeklyReportService,
    weekly_window,
)
from career_opportunity_monitor.sqlite_repository import SQLiteRepository


class FailingDelivery:
    def deliver(self, content: bytes) -> None:
        raise OSError("fictional outage")


class RecordingDelivery:
    def __init__(self) -> None:
        self.contents: list[bytes] = []

    def deliver(self, content: bytes) -> None:
        self.contents.append(content)


def test_weekly_window_is_local_monday_across_dst_transitions() -> None:
    timezone = ZoneInfo("America/New_York")

    spring = weekly_window(datetime(2026, 3, 8, 7, 30, tzinfo=UTC), timezone)
    fall = weekly_window(datetime(2026, 11, 1, 6, 30, tzinfo=UTC), timezone)

    assert spring == ("2026-03-02", "2026-03-09")
    assert fall == ("2026-10-26", "2026-11-02")


def test_weekly_report_aggregates_stored_daily_reports_in_deterministic_order(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    repository.store_report(
        "daily:2026-08-25", b"# Tuesday\n\n- source. Status: partial.\n", "later"
    )
    repository.store_report("daily:2026-08-24", b"# Monday\n", "earlier")
    repository.store_report("daily:2026-08-31", b"outside\n", "outside")

    content = WeeklyReportService(repository).create_and_deliver(
        week_start="2026-08-24",
        week_end="2026-08-31",
        timezone="Asia/Taipei",
        created_at="2026-08-31T01:00:00Z",
        deliveries=(),
    )

    assert content == (
        b"# Weekly career report: 2026-08-24 to 2026-08-30\n\n"
        b"Timezone: Asia/Taipei.\nDaily reports: 2.\n\n"
        b"## Daily report history\n\n"
        b"### 2026-08-24\n\n# Monday\n\n"
        b"### 2026-08-25\n\n# Tuesday\n\n- source. Status: partial.\n"
    )
    assert repository.get_report("weekly:2026-08-24") == content
    repository.close()


def test_weekly_report_empty_week_is_persisted_and_immutable(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    service = WeeklyReportService(repository)

    content = service.create_and_deliver(
        week_start="2026-08-24",
        week_end="2026-08-31",
        timezone="UTC",
        created_at="2026-08-31T00:00:00Z",
        deliveries=(),
    )

    assert b"Daily reports: 0." in content
    assert b"The week has no stored daily reports." in content
    with pytest.raises(Exception, match="conflicting content"):
        repository.store_report("weekly:2026-08-24", b"changed", "2026-08-31T00:00:00Z")
    repository.close()


def test_weekly_delivery_failure_keeps_exact_report_for_retry(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    repository.store_report("daily:2026-08-24", b"# Monday\n", "earlier")
    service = WeeklyReportService(repository)

    with pytest.raises(DeliveryError, match="fictional outage"):
        service.create_and_deliver(
            week_start="2026-08-24",
            week_end="2026-08-31",
            timezone="UTC",
            created_at="2026-08-31T00:00:00Z",
            deliveries=(FailingDelivery(),),
        )

    stored = repository.get_report("weekly:2026-08-24")
    retry = RecordingDelivery()
    assert service.retry_delivery("weekly:2026-08-24", (retry,)) == stored
    assert retry.contents == [stored]
    repository.close()
