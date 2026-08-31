from __future__ import annotations

import stat
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from career_opportunity_monitor.llm_adjustment import LlmAssessment
from career_opportunity_monitor.models import (
    CanonicalJob,
    CategoryEvaluation,
    Evaluation,
    JobLocation,
)
from career_opportunity_monitor.reporting import (
    ConsoleDelivery,
    DailyReportService,
    DeliveryError,
    FeedbackService,
    FileDelivery,
    ReportJob,
    SourceHealth,
    render_daily_report,
)
from career_opportunity_monitor.repository import RepositoryError, StoredJob


@dataclass
class FakeRepository:
    reports: list[tuple[str, bytes, str]]
    feedback: list[tuple[int, str, bytes, str]]
    job_id: int = 71

    def store_report(self, key: str, content: bytes, created_at: str) -> int:
        self.reports.append((key, content, created_at))
        return len(self.reports)

    def get_report(self, key: str) -> bytes:
        for stored_key, content, _ in self.reports:
            if stored_key == key:
                return content
        raise RepositoryError("report does not exist")

    def get_job(self, source_name: str, source_job_id: str) -> StoredJob:
        if (source_name, source_job_id) != ("source-a", "REQ-7"):
            raise RepositoryError("job does not exist")
        return StoredJob(
            id=self.job_id,
            source_name=source_name,
            source_job_id=source_job_id,
            source_url="https://jobs.example/REQ-7",
            state="new",
            current_version_id=1,
            first_seen_at="2026-08-27T00:00:00Z",
            last_seen_at="2026-08-27T00:00:00Z",
        )

    def store_feedback(
        self, job_id: int, key: str, content: bytes, created_at: str
    ) -> int:
        self.feedback.append((job_id, key, content, created_at))
        return len(self.feedback)


def _job(*, posted_at: str | None = "2026-08-25T00:00:00Z") -> CanonicalJob:
    return CanonicalJob(
        schema_version=1,
        source_name="source-a",
        source_job_id="REQ-7",
        source_url="https://jobs.example/REQ-7",
        company="Example",
        title="Platform Engineer",
        description_text="Python systems work",
        location=JobLocation(country_code="TW", region="Taipei", work_mode="hybrid"),
        employment_type="full-time",
        posted_at=posted_at,
        compensation=None,
    )


def _evaluation() -> Evaluation:
    known = CategoryEvaluation(
        category="skills",
        raw_score=Decimal("1"),
        weight=Decimal("30"),
        cap=Decimal("30"),
        points=Decimal("30"),
        matched_phrases=("python",),
        resume_fact_ids=("skill-python",),
        reasons=("Python matches the job description.",),
        missing_reason=None,
    )
    weak = CategoryEvaluation(
        category="title",
        raw_score=Decimal("0"),
        weight=Decimal("20"),
        cap=Decimal("20"),
        points=Decimal("0"),
        matched_phrases=(),
        resume_fact_ids=(),
        reasons=("The title has no target tokens.",),
        missing_reason=None,
    )
    unknown = CategoryEvaluation(
        category="experience",
        raw_score=None,
        weight=Decimal("20"),
        cap=Decimal("20"),
        points=Decimal("0"),
        matched_phrases=(),
        resume_fact_ids=(),
        reasons=("The job description is missing.",),
        missing_reason="The job description is missing.",
    )
    return Evaluation(
        source_name="source-a",
        source_job_id="REQ-7",
        profile_hash="profile-hash",
        strategy_hash="strategy-hash",
        base_score=Decimal("55"),
        confidence=Decimal("0.80"),
        categories=(known, weak, unknown),
    )


def _assessment(status: str = "applied", adjustment: int = 3) -> LlmAssessment:
    return LlmAssessment(
        status=status,  # type: ignore[arg-type]
        base_score=Decimal("55"),
        adjustment=adjustment,
        final_score=Decimal("55") + adjustment,
        model="example-model",
        prompt_version="v1",
        request_hash="request",
        response_hash="response",
        reasons=("The listed work has strong evidence.",),
    )


def _report_job(
    *,
    outcome: str = "new",
    assessment: LlmAssessment | None = None,
    posted_at: str | None = "2026-08-25T00:00:00Z",
) -> ReportJob:
    return ReportJob(
        job=_job(posted_at=posted_at),
        outcome=outcome,  # type: ignore[arg-type]
        observed_at="2026-08-27T00:00:00Z",
        evaluation=_evaluation(),
        assessment=assessment or _assessment(),
    )


def test_normal_report_matches_golden_file() -> None:
    rendered = render_daily_report(
        report_date="2026-08-27",
        jobs=(_report_job(),),
        source_health=(
            SourceHealth(
                source_name="source-a", status="completed", partial=False, failures=()
            ),
        ),
        display_limit=1,
    )

    assert rendered == _golden("normal-report.md")


def test_quiet_report_matches_golden_file() -> None:
    rendered = render_daily_report(
        report_date="2026-08-27",
        jobs=(),
        source_health=(
            SourceHealth(
                source_name="source-a", status="completed", partial=False, failures=()
            ),
        ),
        display_limit=5,
    )

    assert rendered == _golden("quiet-report.md")


def test_partial_source_report_matches_golden_file() -> None:
    rendered = render_daily_report(
        report_date="2026-08-27",
        jobs=(_report_job(),),
        source_health=(
            SourceHealth(
                source_name="source-a",
                status="completed",
                partial=True,
                failures=("One detail request failed.",),
            ),
        ),
        display_limit=5,
    )

    assert rendered == _golden("partial-source-report.md")


def test_unavailable_llm_report_matches_golden_file() -> None:
    rendered = render_daily_report(
        report_date="2026-08-27",
        jobs=(_report_job(assessment=_assessment(status="unavailable", adjustment=0)),),
        source_health=(),
        display_limit=5,
    )

    assert rendered == _golden("unavailable-llm-report.md")


def test_capped_adjustment_report_matches_golden_file() -> None:
    lower = _report_job()
    higher = _report_job(outcome="changed", assessment=_assessment(adjustment=5))
    rendered = render_daily_report(
        report_date="2026-08-27",
        jobs=(lower, higher),
        source_health=(),
        display_limit=1,
    )

    assert rendered == _golden("capped-adjustment-report.md")


def test_delivery_failure_persists_and_retries_golden_report(tmp_path: Path) -> None:
    repository = FakeRepository(reports=[], feedback=[])
    service = DailyReportService(repository)

    with pytest.raises(DeliveryError, match="delivery failed"):
        service.create_and_deliver(
            report_date="2026-08-27",
            jobs=(_report_job(),),
            source_health=(),
            display_limit=5,
            created_at="2026-08-27T00:00:00Z",
            deliveries=(FailingDelivery(),),
        )

    golden = _golden("delivery-failure-report.md")
    assert repository.reports == [("daily:2026-08-27", golden, "2026-08-27T00:00:00Z")]

    output = tmp_path / "retry.md"
    service.retry_delivery("daily:2026-08-27", (FileDelivery(output),))

    assert output.read_bytes() == golden


def test_initial_delivery_attempts_later_destination_after_first_failure() -> None:
    repository = FakeRepository(reports=[], feedback=[])
    later = RecordingDelivery()

    with pytest.raises(DeliveryError, match="delivery failed"):
        DailyReportService(repository).create_and_deliver(
            report_date="2026-08-27",
            jobs=(_report_job(),),
            source_health=(),
            display_limit=5,
            created_at="2026-08-27T00:00:00Z",
            deliveries=(FailingDelivery(), later),
        )

    assert later.contents == [_golden("delivery-failure-report.md")]


def test_retry_delivery_attempts_later_destination_after_first_failure() -> None:
    golden = _golden("delivery-failure-report.md")
    repository = FakeRepository(
        reports=[("daily:2026-08-27", golden, "2026-08-27T00:00:00Z")],
        feedback=[],
    )
    later = RecordingDelivery()

    with pytest.raises(DeliveryError, match="delivery failed"):
        DailyReportService(repository).retry_delivery(
            "daily:2026-08-27", (FailingDelivery(), later)
        )

    assert later.contents == [golden]


def test_file_and_console_delivery_write_the_same_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    content = b"# Report\n"
    path = tmp_path / "daily.md"

    FileDelivery(path).deliver(content)
    ConsoleDelivery().deliver(content)

    assert path.read_bytes() == content
    assert capsys.readouterr().out.encode() == content


def test_file_delivery_tightens_existing_private_modes(tmp_path: Path) -> None:
    report_directory = tmp_path / "reports"
    report_directory.mkdir(mode=0o755)
    report_path = report_directory / "daily.md"
    report_path.write_bytes(b"old")
    report_path.chmod(0o644)

    FileDelivery(report_path).deliver(b"new")

    assert stat.S_IMODE(report_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600


def test_file_delivery_rejects_a_symlinked_private_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    report_directory = tmp_path / "reports"
    report_directory.symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError, match="unsafe report directory"):
        FileDelivery(report_directory / "daily.md").deliver(b"private")


def test_feedback_uses_the_canonical_job_identity() -> None:
    repository = FakeRepository(reports=[], feedback=[])

    FeedbackService(repository).record(
        source_name="source-a",
        source_job_id="REQ-7",
        decision="interested",
        created_at="2026-08-27T00:00:00Z",
    )

    assert repository.feedback[0][0] == 71
    assert b'"source_job_id":"REQ-7"' in repository.feedback[0][2]
    assert b'"source_name":"source-a"' in repository.feedback[0][2]


class FailingDelivery:
    def deliver(self, content: bytes) -> None:
        raise OSError("disk is full")


@dataclass
class RecordingDelivery:
    contents: list[bytes] = field(default_factory=list[bytes])

    def deliver(self, content: bytes) -> None:
        self.contents.append(content)


def _golden(name: str) -> bytes:
    return (Path(__file__).parent / "golden" / name).read_bytes()
