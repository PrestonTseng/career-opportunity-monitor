from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from .llm_adjustment import LlmAssessment
from .models import CanonicalJob, Evaluation
from .repository import StoredJob

ReportOutcome = Literal["new", "changed"]
SourceStatus = Literal["completed", "failed"]


class DeliveryError(RuntimeError):
    """Raised when a report cannot reach one requested delivery target."""


class Delivery(Protocol):
    def deliver(self, content: bytes) -> None: ...


class ReportRepository(Protocol):
    def store_report(self, key: str, content: bytes, created_at: str) -> int: ...

    def get_report(self, key: str) -> bytes: ...


class FeedbackRepository(Protocol):
    def get_job(self, source_name: str, source_job_id: str) -> StoredJob: ...

    def store_feedback(
        self, job_id: int, key: str, content: bytes, created_at: str
    ) -> int: ...


@dataclass(frozen=True)
class ReportJob:
    job: CanonicalJob
    outcome: ReportOutcome
    observed_at: str
    evaluation: Evaluation
    assessment: LlmAssessment


@dataclass(frozen=True)
class SourceHealth:
    source_name: str
    status: SourceStatus
    partial: bool
    failures: tuple[str, ...]


class FileDelivery:
    def __init__(self, path: Path) -> None:
        self._path = path

    def deliver(self, content: bytes) -> None:
        _prepare_report_directory(self._path.parent)
        if self._path.exists() or self._path.is_symlink():
            existing = self._path.lstat()
            if self._path.is_symlink() or not stat.S_ISREG(existing.st_mode):
                raise OSError(f"unsafe report path: {self._path}")
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self._path)
        finally:
            temporary.unlink(missing_ok=True)


class ConsoleDelivery:
    def deliver(self, content: bytes) -> None:
        sys.stdout.buffer.write(content)


def _prepare_report_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(existing.st_mode):
        raise OSError(f"unsafe report directory: {path}")
    os.chmod(path, 0o700)


class DailyReportService:
    """Render, persist, and deliver immutable daily report bytes."""

    def __init__(self, repository: ReportRepository) -> None:
        self._repository = repository

    def create_and_deliver(
        self,
        *,
        report_date: str,
        jobs: tuple[ReportJob, ...],
        source_health: tuple[SourceHealth, ...],
        display_limit: int,
        created_at: str,
        deliveries: tuple[Delivery, ...],
    ) -> bytes:
        content = render_daily_report(
            report_date=report_date,
            jobs=jobs,
            source_health=source_health,
            display_limit=display_limit,
        )
        self._repository.store_report(f"daily:{report_date}", content, created_at)
        for delivery in deliveries:
            _deliver(delivery, content)
        return content

    def retry_delivery(self, key: str, deliveries: tuple[Delivery, ...]) -> bytes:
        content = self._repository.get_report(key)
        for delivery in deliveries:
            _deliver(delivery, content)
        return content


class FeedbackService:
    """Store an advisory job decision under the canonical source identity."""

    def __init__(self, repository: FeedbackRepository) -> None:
        self._repository = repository

    def record(
        self,
        *,
        source_name: str,
        source_job_id: str,
        decision: str,
        created_at: str,
    ) -> int:
        if not source_name or not source_job_id or not decision or not created_at:
            raise ValueError("feedback identity, decision, and time are required")
        job = self._repository.get_job(source_name, source_job_id)
        content = _json_bytes(
            {
                "decision": decision,
                "source_job_id": source_job_id,
                "source_name": source_name,
            }
        )
        key = f"feedback:{source_name}:{source_job_id}:{decision}"
        return self._repository.store_feedback(job.id, key, content, created_at)


def render_daily_report(
    *,
    report_date: str,
    jobs: tuple[ReportJob, ...],
    source_health: tuple[SourceHealth, ...],
    display_limit: int,
) -> bytes:
    """Render one stable Markdown report from already committed report inputs."""
    report_day = _parse_date(report_date)
    if display_limit < 0:
        raise ValueError("display limit cannot be negative")
    ordered_jobs = tuple(sorted(jobs, key=_job_sort_key))
    displayed = ordered_jobs[:display_limit]
    lines = [f"# Daily career report: {report_date}", ""]
    lines.extend(_receipt_lines(len(ordered_jobs), len(displayed)))
    lines.extend(("", "## New and changed jobs", ""))
    if displayed:
        for index, report_job in enumerate(displayed, start=1):
            lines.extend(_job_lines(index, report_job, report_day))
    else:
        lines.append("The report displays no jobs.")
    lines.extend(("", "## Source health", ""))
    if source_health:
        for health in sorted(source_health, key=lambda value: value.source_name):
            lines.extend(_source_health_lines(health))
    else:
        lines.append("The report has no source health receipt.")
    lines.extend(("", "## Data gaps", ""))
    gaps = _data_gaps(displayed)
    if gaps:
        lines.extend(f"- {gap}" for gap in gaps)
    else:
        lines.append("Displayed jobs have no reported data gaps.")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _receipt_lines(total: int, displayed: int) -> tuple[str, str]:
    if total == 0:
        receipt = "Receipt: The run found no new or changed jobs."
    elif total == 1:
        receipt = "Receipt: The run found 1 new or changed job."
    else:
        receipt = f"Receipt: The run found {total} new or changed jobs."
    omitted = total - displayed
    return receipt, f"Displayed jobs: {displayed} of {total}. Omitted jobs: {omitted}."


def _job_sort_key(report_job: ReportJob) -> tuple[Decimal, str, str]:
    return (
        -report_job.assessment.final_score,
        report_job.job.source_name,
        report_job.job.source_job_id,
    )


def _job_lines(index: int, report_job: ReportJob, report_day: date) -> list[str]:
    job = report_job.job
    evaluation = report_job.evaluation
    categories = evaluation.categories
    positive = tuple(
        reason
        for category in categories
        if category.points > 0
        for reason in category.reasons
    )
    weak = tuple(
        reason
        for category in categories
        if category.raw_score is not None and category.points == 0
        for reason in category.reasons
    )
    unknown = tuple(
        f"{category.category}: {category.missing_reason}"
        for category in categories
        if category.missing_reason is not None
    )
    evidence_ids = tuple(
        fact_id for category in categories for fact_id in category.resume_fact_ids
    )
    location = _location_text(job)
    adjustment = report_job.assessment.adjustment
    adjustment_text = f"+{adjustment}" if adjustment >= 0 else str(adjustment)
    referral = (
        "A referral path is listed."
        if "referral path"
        in " ".join(
            reason for category in categories for reason in category.reasons
        ).lower()
        else "No referral path is listed."
    )
    return [
        f"### {index}. {job.title} at {job.company}",
        "",
        f"State: {report_job.outcome}.",
        f"Requisition ID: {job.source_job_id}.",
        f"Official link: {job.source_url}",
        f"Location: {location}.",
        f"Posting age: {_posting_age(job.posted_at, report_day)}.",
        f"Base score: {evaluation.base_score:.2f}.",
        f"LLM adjustment: {adjustment_text} ({report_job.assessment.status}).",
        f"Final score: {report_job.assessment.final_score:.2f}.",
        _field_line("Evidence IDs", evidence_ids),
        _field_line("Positive signals", positive),
        _field_line("Weak signals", weak),
        _field_line("Unknown fields", unknown),
        f"Referral signal: {referral}",
    ]


def _location_text(job: CanonicalJob) -> str:
    if job.location is None:
        return "Unknown"
    parts = [
        value for value in (job.location.region, job.location.country_code) if value
    ]
    location = ", ".join(parts) or "Unknown"
    if job.location.work_mode:
        return f"{location} ({job.location.work_mode})"
    return location


def _posting_age(posted_at: str | None, report_day: date) -> str:
    if posted_at is None:
        return "Unknown"
    try:
        posted_day = date.fromisoformat(posted_at[:10])
    except ValueError:
        return "Unknown"
    days = max((report_day - posted_day).days, 0)
    return "1 day" if days == 1 else f"{days} days"


def _list_or_none(values: Iterable[str]) -> str:
    ordered = tuple(sorted(set(values)))
    return ", ".join(ordered) if ordered else "None"


def _field_line(label: str, values: Iterable[str]) -> str:
    value = _list_or_none(values)
    punctuation = "" if value.endswith((".", "!", "?")) else "."
    return f"{label}: {value}{punctuation}"


def _source_health_lines(health: SourceHealth) -> list[str]:
    status = (
        "failed"
        if health.status == "failed"
        else "partial"
        if health.partial
        else "complete"
    )
    lines = [f"- {health.source_name}. Status: {status}."]
    lines.extend(f"  Evidence: {failure}" for failure in sorted(set(health.failures)))
    return lines


def _data_gaps(jobs: tuple[ReportJob, ...]) -> tuple[str, ...]:
    values = tuple(
        f"{report_job.job.source_name}/{report_job.job.source_job_id}: "
        f"{category.category}: {category.missing_reason}"
        for report_job in jobs
        for category in report_job.evaluation.categories
        if category.missing_reason is not None
    )
    return tuple(sorted(values))


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("report date must use YYYY-MM-DD") from exc


def _deliver(delivery: Delivery, content: bytes) -> None:
    try:
        delivery.deliver(content)
    except OSError as exc:
        raise DeliveryError(f"delivery failed: {exc}") from exc


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
