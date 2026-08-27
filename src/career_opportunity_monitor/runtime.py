from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .collection import CollectionService
from .config import ConfigError, load_configuration
from .llm_adjustment import LlmConfiguration, assess_job, store_assessment
from .models import LoadedConfiguration
from .nvidia_workday import NvidiaWorkdaySource, SourceError
from .ranking import evaluate_job
from .reporting import (
    DailyReportService,
    DeliveryError,
    FileDelivery,
    ReportJob,
    SourceHealth,
)
from .repository import RepositoryError
from .source import JobSource
from .sqlite_repository import SQLiteRepository

_DEFAULT_RESUME_PATH = "/profile/resume_facts.yaml"
_DEFAULT_CONFIG_DIR = "/config"
_DEFAULT_DATA_DIR = "/data"


def run(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    source: JobSource | None = None,
) -> int:
    """Run one profile-gated container role."""
    parser = argparse.ArgumentParser(prog="career-monitor")
    parser.add_argument(
        "command", choices=("validate", "daily", "retry-delivery", "weekly")
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-date")
    arguments = parser.parse_args(argv)
    values = os.environ if environment is None else environment
    data_directory = Path(values.get("CAREER_MONITOR_DATA_DIR", _DEFAULT_DATA_DIR))
    run_id = values.get("CAREER_MONITOR_RUN_ID", uuid.uuid4().hex)
    finished_at = values.get("CAREER_MONITOR_NOW", _now())

    try:
        _validate_data_directory(data_directory)
        configuration = load_configuration(
            Path(values.get("CAREER_MONITOR_RESUME_PATH", _DEFAULT_RESUME_PATH)),
            Path(values.get("CAREER_MONITOR_CONFIG_DIR", _DEFAULT_CONFIG_DIR)),
        )
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
                {
                    "command": arguments.command,
                    "profile_id": configuration.profile.profile_id,
                    "status": "ok",
                    "strategy_id": configuration.strategy.strategy_id,
                }
            )
        )
        return 0

    if arguments.command == "weekly":
        error = "weekly maintenance is not implemented"
        _write_json(
            data_directory / "errors" / f"weekly-{run_id}.json",
            {
                "command": "weekly",
                "error": error,
                "failed_at": finished_at,
                "run_id": run_id,
                "status": "failed",
            },
        )
        print(error, file=sys.stderr)
        return 2

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
                receipt.update(
                    _run_daily(
                        repository,
                        configuration=configuration,
                        source=source or NvidiaWorkdaySource(),
                        data_directory=data_directory,
                        environment=values,
                        run_id=run_id,
                        finished_at=finished_at,
                    )
                )
            elif arguments.command == "retry-delivery":
                report_date = _required_report_date(arguments.report_date)
                report_path = data_directory / "reports" / f"daily-{report_date}.md"
                DailyReportService(repository).retry_delivery(
                    f"daily:{report_date}", (FileDelivery(report_path),)
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
        output["report"] = str(
            data_directory / "reports" / f"daily-{finished_at[:10]}.md"
        )
    elif arguments.command == "retry-delivery":
        output["report"] = str(
            data_directory / "reports" / f"daily-{arguments.report_date}.md"
        )
    print(_json_text(output))
    return 0


def _run_daily(
    repository: SQLiteRepository,
    *,
    configuration: LoadedConfiguration,
    source: JobSource,
    data_directory: Path,
    environment: Mapping[str, str],
    run_id: str,
    finished_at: str,
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
    collection = CollectionService(repository).collect(
        source, run_id=f"{run_id}:source", now=finished_at
    )
    observations = {
        (item.job.source_name, item.job.source_job_id): item
        for item in collection.observations
    }
    report_jobs: list[ReportJob] = []
    llm_configuration = LlmConfiguration.from_environment(environment)
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
    raw_failures = collection.receipt.get("failures", [])
    if not isinstance(raw_failures, list):
        raise ValueError("source receipt failures must be a list of text values")
    raw_failure_items = cast(list[object], raw_failures)
    failures = tuple(
        failure for failure in raw_failure_items if isinstance(failure, str)
    )
    if len(failures) != len(raw_failure_items):
        raise ValueError("source receipt failures must be a list of text values")
    partial = collection.receipt.get("partial", False)
    if not isinstance(partial, bool):
        raise ValueError("source receipt partial value must be a boolean")
    report_path = data_directory / "reports" / f"daily-{finished_at[:10]}.md"
    DailyReportService(repository).create_and_deliver(
        report_date=finished_at[:10],
        jobs=tuple(report_jobs),
        source_health=(
            SourceHealth(
                source_name=source.name,
                status="completed",
                partial=partial,
                failures=failures,
            ),
        ),
        display_limit=int(environment.get("CAREER_MONITOR_DISPLAY_LIMIT", "25")),
        created_at=finished_at,
        deliveries=(FileDelivery(report_path),),
    )
    return {
        "accepted": len(collection.observation_results),
        "evaluated": len(collection.observation_results),
        "new_or_changed": len(report_jobs),
        "report": str(report_path),
        "source_partial": partial,
    }


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
