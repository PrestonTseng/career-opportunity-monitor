from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import yaml

from career_opportunity_monitor.config import ConfigError, load_configuration
from career_opportunity_monitor.schedule import render_supercronic_schedule

ROOT = Path(__file__).parents[1]
RESUME = ROOT / "examples" / "resume_facts.yaml"
CONFIG = ROOT / "examples" / "strategy" / "v1"


def _configured(tmp_path: Path, schedule: dict[str, object]) -> Path:
    target = tmp_path / "config"
    shutil.copytree(CONFIG, target)
    (target / "schedule.yaml").write_text(yaml.safe_dump(schedule), encoding="utf-8")
    return target


def _schedule() -> dict[str, object]:
    value = yaml.safe_load((CONFIG / "schedule.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _set_leading_zero_overlap(value: dict[str, object]) -> None:
    cast(dict[str, object], value["daily"]).update(cron="0 01 * * *")


def _set_invalid_timezone(value: dict[str, object]) -> None:
    value.update(timezone="Not/AZone")


def _set_invalid_daily_cron(value: dict[str, object]) -> None:
    cast(dict[str, object], value["daily"]).update(cron="@daily")


def _set_overlapping_weekly_cron(value: dict[str, object]) -> None:
    cast(dict[str, object], value["weekly"]).update(cron="0 0 * * 1")


def test_schedule_renders_enabled_daily_and_weekly_in_local_wall_time() -> None:
    loaded = load_configuration(RESUME, CONFIG)

    assert loaded.schedule.timezone == "Asia/Taipei"
    assert render_supercronic_schedule(loaded.schedule) == (
        "CRON_TZ=Asia/Taipei\n"
        "0 0 * * * career-monitor daily\n"
        "0 1 * * 1 career-monitor weekly\n"
    )


def test_schedule_cadences_can_be_disabled(tmp_path: Path) -> None:
    schedule = _schedule()
    daily = cast(dict[str, object], schedule["daily"])
    weekly = cast(dict[str, object], schedule["weekly"])
    daily["enabled"] = False
    weekly["enabled"] = False

    loaded = load_configuration(RESUME, _configured(tmp_path, schedule))

    assert render_supercronic_schedule(loaded.schedule) == ""


def test_compose_scheduler_generates_schedule_from_validated_configuration() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    entrypoint = (ROOT / "deploy" / "scheduler-entrypoint.sh").read_text(
        encoding="utf-8"
    )

    assert "/usr/local/bin/career-monitor-scheduler" in compose
    assert "CAREER_MONITOR_CRONTAB_FILE" not in compose
    assert "career-monitor render-schedule" in entrypoint
    assert "supercronic" in entrypoint


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_set_invalid_timezone, "IANA timezone"),
        (_set_invalid_daily_cron, "daily cron"),
        (_set_overlapping_weekly_cron, "overlap"),
        (
            _set_leading_zero_overlap,
            "overlap",
        ),
    ],
)
def test_schedule_rejects_invalid_or_unsafe_configuration(
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], object],
    message: str,
) -> None:
    schedule = _schedule()
    mutate(schedule)

    with pytest.raises(ConfigError, match=message):
        load_configuration(RESUME, _configured(tmp_path, schedule))
