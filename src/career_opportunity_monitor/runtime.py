from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from .collection import CollectionResult, CollectionService
from .config import ConfigError, load_configuration
from .discord_delivery import DiscordDelivery
from .llm_adjustment import LlmConfiguration, assess_job, store_assessment
from .models import LoadedConfiguration
from .ranking import evaluate_job
from .reporting import (
    DailyReportService,
    Delivery,
    DeliveryError,
    FileDelivery,
    ReportJob,
    SourceHealth,
    WeeklyReportService,
    weekly_window,
)
from .repository import RepositoryError
from .schedule import render_supercronic_schedule
from .source import JobSource, SourceError
from .source_registry import build_sources
from .sqlite_repository import SQLiteRepository

_DEFAULT_RESUME_PATH = "/profile/resume_facts.yaml"
_DEFAULT_CONFIG_DIR = "/config"
_DEFAULT_DATA_DIR = "/data"
_PUBLIC_EXAMPLE_PROFILE_ID = "alex-chen-fictional"
_PUBLIC_EXAMPLE_STRATEGY_ID = "taiwan-software-fictional"


def _runtime_mode(environment: Mapping[str, str]) -> str:
    mode = environment.get("CAREER_MONITOR_MODE", "production")
    if mode not in ("demo", "production"):
        raise ConfigError("CAREER_MONITOR_MODE must be demo or production")
    return mode


def _reject_public_examples(configuration: LoadedConfiguration, mode: str) -> None:
    if mode == "demo":
        return
    if (
        configuration.profile.profile_id == _PUBLIC_EXAMPLE_PROFILE_ID
        or configuration.strategy.strategy_id == _PUBLIC_EXAMPLE_STRATEGY_ID
    ):
        raise ConfigError(
            "production mode cannot use the public fictional examples; "
            "mount private inputs or set CAREER_MONITOR_MODE=demo"
        )


def _configuration_summary(
    configuration: LoadedConfiguration,
    *,
    mode: str,
    resume_path: Path,
    config_directory: Path,
    data_directory: Path,
) -> dict[str, object]:
    return {
        "command": "validate",
        "destinations": [
            {
                "enabled": destination.enabled,
                "id": destination.id,
                "report_cadences": list(destination.report_cadences),
                "type": destination.type,
            }
            for destination in configuration.destinations
        ],
        "mode": mode,
        "paths": {
            "configuration_directory": str(config_directory),
            "data_directory": str(data_directory),
            "resume_file": str(resume_path),
        },
        "schedule": {
            "daily": {
                "cron": configuration.schedule.daily.cron,
                "enabled": configuration.schedule.daily.enabled,
            },
            "timezone": configuration.schedule.timezone,
            "weekly": {
                "cron": configuration.schedule.weekly.cron,
                "enabled": configuration.schedule.weekly.enabled,
            },
        },
        "schema_versions": {
            "destinations": 1,
            "resume_facts": 1,
            "schedule": 1,
            "sources": 1,
            "strategy": 1,
        },
        "sources": [
            {
                "adapter": source.adapter,
                "enabled": source.enabled,
                "id": source.id,
            }
            for source in configuration.sources
        ],
        "status": "ok",
    }


def run(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    source: JobSource | None = None,
    sources: Sequence[JobSource] | None = None,
) -> int:
    """Run one profile-gated container role."""
    parser = argparse.ArgumentParser(
        prog="career-monitor",
        description="Validate configuration or create and deliver career reports.",
    )
    parser.add_argument(
        "command",
        choices=("validate", "daily", "retry-delivery", "render-schedule", "weekly"),
        help="Role to run. Validate prints a secret-safe configuration summary.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate and write a receipt only."
    )
    parser.add_argument(
        "--report-date", help="Stored report date for retry-delivery (YYYY-MM-DD)."
    )
    parser.add_argument(
        "--cadence",
        choices=("daily", "weekly"),
        default="daily",
        help="Stored report cadence for retry-delivery.",
    )
    arguments = parser.parse_args(argv)
    values = os.environ if environment is None else environment
    data_directory = Path(values.get("CAREER_MONITOR_DATA_DIR", _DEFAULT_DATA_DIR))
    resume_path = Path(values.get("CAREER_MONITOR_RESUME_PATH", _DEFAULT_RESUME_PATH))
    config_directory = Path(
        values.get("CAREER_MONITOR_CONFIG_DIR", _DEFAULT_CONFIG_DIR)
    )
    run_id = values.get("CAREER_MONITOR_RUN_ID", uuid.uuid4().hex)
    finished_at = values.get("CAREER_MONITOR_NOW", _now())
    if source is not None and sources is not None:
        raise ValueError("source and sources overrides are mutually exclusive")

    try:
        _validate_data_directory(data_directory)
        configuration = load_configuration(
            resume_path,
            config_directory,
        )
        mode = _runtime_mode(values)
        _reject_public_examples(configuration, mode)
    except (ConfigError, OSError, ValueError) as exc:
        _try_write_error(
            data_directory,
            command=arguments.command,
            run_id=run_id,
            failed_at=finished_at,
            error=str(exc),
        )
        print(f"runtime validation failed: {exc}", file=sys.stderr)
        return 2

    if arguments.command == "validate":
        print(
            _json_text(
                _configuration_summary(
                    configuration,
                    mode=mode,
                    resume_path=resume_path,
                    config_directory=config_directory,
                    data_directory=data_directory,
                )
            )
        )
        return 0

    if arguments.command == "render-schedule":
        sys.stdout.write(render_supercronic_schedule(configuration.schedule))
        return 0

    receipt_path = data_directory / "receipts" / f"{arguments.command}-{run_id}.json"
    error_path = data_directory / "errors" / f"{arguments.command}-{run_id}.json"
    repository = SQLiteRepository(data_directory / "history.sqlite3")
    try:
        timeout_seconds = float(
            values.get("CAREER_MONITOR_LOCK_TIMEOUT_SECONDS", "0.25")
        )
        with repository.writer_lock(arguments.command, timeout_seconds=timeout_seconds):
            receipt: dict[str, object] = {
                "command": arguments.command,
                "dry_run": arguments.dry_run,
                "finished_at": finished_at,
                "quiet": True,
                "run_id": run_id,
                "status": "completed",
            }
            if arguments.command == "daily" and not arguments.dry_run:
                report_date = _local_report_date(
                    finished_at, ZoneInfo(configuration.schedule.timezone)
                )
                configured_sources = (
                    tuple(sources)
                    if sources is not None
                    else (source,)
                    if source is not None
                    else build_sources(configuration.sources)
                )
                receipt.update(
                    _run_daily(
                        repository,
                        configuration=configuration,
                        sources=configured_sources,
                        data_directory=data_directory,
                        environment=values,
                        run_id=run_id,
                        finished_at=finished_at,
                        report_date=report_date,
                    )
                )
            elif arguments.command == "weekly" and not arguments.dry_run:
                week_start, week_end = _previous_week_window(
                    finished_at, ZoneInfo(configuration.schedule.timezone)
                )
                report_path = data_directory / "reports" / f"weekly-{week_start}.md"
                WeeklyReportService(repository).create_and_deliver(
                    week_start=week_start,
                    week_end=week_end,
                    timezone=configuration.schedule.timezone,
                    created_at=finished_at,
                    deliveries=build_deliveries(
                        configuration,
                        cadence="weekly",
                        report_key=f"weekly:{week_start}",
                        report_path=report_path,
                        repository=repository,
                        now=lambda: finished_at,
                    ),
                )
                receipt.update(
                    {
                        "report": str(report_path),
                        "week_end": week_end,
                        "week_start": week_start,
                    }
                )
            elif arguments.command == "retry-delivery" and not arguments.dry_run:
                report_date = _required_report_date(arguments.report_date)
                cadence = arguments.cadence
                report_path = data_directory / "reports" / f"{cadence}-{report_date}.md"
                service = (
                    WeeklyReportService(repository)
                    if cadence == "weekly"
                    else DailyReportService(repository)
                )
                service.retry_delivery(
                    f"{cadence}:{report_date}",
                    build_deliveries(
                        configuration,
                        cadence=cadence,
                        report_key=f"{cadence}:{report_date}",
                        report_path=report_path,
                        repository=repository,
                        now=lambda: finished_at,
                    ),
                )
                receipt["report"] = str(report_path)
            _write_json(receipt_path, receipt)
    except (DeliveryError, OSError, RepositoryError, SourceError, ValueError) as exc:
        _write_json(
            error_path,
            {
                "command": arguments.command,
                "error": str(exc),
                "failed_at": finished_at,
                "run_id": run_id,
                "sources_hash": configuration.sources_hash,
                "status": "failed",
            },
        )
        print(f"{arguments.command} failed: {exc}", file=sys.stderr)
        return 1
    finally:
        repository.close()
    output: dict[str, object] = {
        "command": arguments.command,
        "receipt": str(receipt_path),
        "status": "completed",
    }
    if arguments.command == "daily" and not arguments.dry_run:
        report_date = _local_report_date(
            finished_at, ZoneInfo(configuration.schedule.timezone)
        )
        output["report"] = str(data_directory / "reports" / f"daily-{report_date}.md")
    elif arguments.command == "retry-delivery":
        output["report"] = str(
            data_directory
            / "reports"
            / f"{arguments.cadence}-{arguments.report_date}.md"
        )
    elif arguments.command == "weekly" and not arguments.dry_run:
        week_start, _ = _previous_week_window(
            finished_at, ZoneInfo(configuration.schedule.timezone)
        )
        output["report"] = str(data_directory / "reports" / f"weekly-{week_start}.md")
    print(_json_text(output))
    return 0


def _run_daily(
    repository: SQLiteRepository,
    *,
    configuration: LoadedConfiguration,
    sources: Sequence[JobSource],
    data_directory: Path,
    environment: Mapping[str, str],
    run_id: str,
    finished_at: str,
    report_date: str,
) -> dict[str, object]:
    loaded = configuration
    repository.store_profile_snapshot(
        loaded.profile_hash, loaded.profile_snapshot_bytes, finished_at
    )
    repository.store_strategy_snapshot(
        loaded.strategy_hash,
        loaded.strategy_snapshot_bytes,
        finished_at,
    )
    repository.store_sources_snapshot(
        loaded.sources_hash,
        loaded.sources_snapshot_bytes,
        finished_at,
    )
    if not sources:
        raise SourceError("no enabled sources configured")
    collections: list[CollectionResult] = []
    source_health: list[SourceHealth] = []
    source_failures = 0
    failed_messages: list[str] = []
    service = CollectionService(repository)
    for configured_source in sources:
        try:
            collection = service.collect(
                configured_source,
                run_id=f"{run_id}:source:{configured_source.name}",
                sources_hash=loaded.sources_hash,
                now=finished_at,
            )
        except SourceError as exc:
            source_failures += 1
            failed_messages.append(f"{configured_source.name}: {exc}")
            source_health.append(
                SourceHealth(
                    source_name=configured_source.name,
                    status="failed",
                    partial=False,
                    failures=(str(exc),),
                )
            )
            continue
        collections.append(collection)
        raw_failures_value = collection.receipt.get("failures", [])
        if not isinstance(raw_failures_value, list):
            raise ValueError("source receipt failures must be a list of text values")
        raw_failures = cast(list[object], raw_failures_value)
        failures = tuple(
            failure for failure in raw_failures if isinstance(failure, str)
        )
        if len(failures) != len(raw_failures):
            raise ValueError("source receipt failures must be a list of text values")
        partial = collection.receipt.get("partial", False)
        if not isinstance(partial, bool):
            raise ValueError("source receipt partial value must be a boolean")
        source_health.append(
            SourceHealth(
                source_name=configured_source.name,
                status="completed",
                partial=partial,
                failures=failures,
            )
        )
    if not collections:
        raise SourceError(
            "all configured sources failed: " + "; ".join(failed_messages)
        )
    report_jobs: list[ReportJob] = []
    llm_configuration = LlmConfiguration.from_environment(environment)
    accepted = 0
    for collection in collections:
        observations = {
            (item.job.source_name, item.job.source_job_id): item
            for item in collection.observations
        }
        accepted += len(collection.observation_results)
        for result in collection.observation_results:
            observation = observations[(result.source_name, result.source_job_id)]
            evaluation = evaluate_job(loaded, observation.job)
            evaluation_id = repository.store_evaluation(
                result.job_version_id,
                loaded.profile_hash,
                loaded.strategy_hash,
                evaluation.to_json_bytes(),
                finished_at,
            )
            assessment = assess_job(
                llm_configuration, loaded.strategy, observation.job, evaluation
            )
            store_assessment(repository, evaluation_id, assessment, finished_at)
            if result.outcome in ("new", "changed"):
                report_jobs.append(
                    ReportJob(
                        job=observation.job,
                        outcome=result.outcome,
                        observed_at=observation.observed_at,
                        evaluation=evaluation,
                        assessment=assessment,
                    )
                )
    report_path = data_directory / "reports" / f"daily-{report_date}.md"
    DailyReportService(repository).create_and_deliver(
        report_date=report_date,
        jobs=tuple(report_jobs),
        source_health=tuple(source_health),
        display_limit=int(environment.get("CAREER_MONITOR_DISPLAY_LIMIT", "25")),
        created_at=finished_at,
        deliveries=build_deliveries(
            loaded,
            cadence="daily",
            report_key=f"daily:{report_date}",
            report_path=report_path,
            repository=repository,
            now=lambda: finished_at,
        ),
    )
    return {
        "accepted": accepted,
        "evaluated": accepted,
        "new_or_changed": len(report_jobs),
        "report": str(report_path),
        "sources_hash": loaded.sources_hash,
        "source_failures": source_failures,
        "source_partial": any(health.partial for health in source_health),
    }


def build_deliveries(
    configuration: LoadedConfiguration,
    *,
    cadence: str,
    report_key: str,
    report_path: Path,
    repository: SQLiteRepository,
    now: Callable[[], str],
) -> tuple[Delivery, ...]:
    deliveries: list[Delivery] = [FileDelivery(report_path)]
    deliveries.extend(
        DiscordDelivery(
            destination_id=destination.id,
            report_key=report_key,
            webhook_url_file=destination.webhook_url_file,
            repository=repository,
            now=now,
        )
        for destination in configuration.destinations
        if destination.enabled and cadence in destination.report_cadences
    )
    return tuple(deliveries)


def _validate_data_directory(path: Path) -> None:
    if not path.is_dir():
        raise ValueError(f"data directory is not a directory: {path}")
    probe = path / f".write-probe-{os.getpid()}"
    descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    probe.unlink()


def _required_report_date(value: str | None) -> str:
    if value is None:
        raise ValueError("retry-delivery requires --report-date")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("report date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("report date must use YYYY-MM-DD")
    return value


def _parse_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("runtime time must use an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("runtime time must include a timezone")
    return parsed


def _local_report_date(value: str, timezone: ZoneInfo) -> str:
    return _parse_instant(value).astimezone(timezone).date().isoformat()


def _previous_week_window(value: str, timezone: ZoneInfo) -> tuple[str, str]:
    local_instant = _parse_instant(value).astimezone(timezone)
    return weekly_window(local_instant - timedelta(days=7), timezone)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(_json_text(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _try_write_error(
    data_directory: Path,
    *,
    command: str,
    run_id: str,
    failed_at: str,
    error: str,
) -> None:
    with contextlib.suppress(OSError):
        _write_json(
            data_directory / "errors" / f"{command}-{run_id}.json",
            {
                "command": command,
                "error": error,
                "failed_at": failed_at,
                "run_id": run_id,
                "status": "failed",
            },
        )


def _json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
