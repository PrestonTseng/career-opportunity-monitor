from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from career_opportunity_monitor.collection import CollectionService
from career_opportunity_monitor.nvidia_workday import NvidiaWorkdaySource, SourceError
from career_opportunity_monitor.ranking import parse_job
from career_opportunity_monitor.repository import SourceObservation
from career_opportunity_monitor.source import SourceFetchResult
from career_opportunity_monitor.sqlite_repository import SQLiteRepository

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "nvidia"


class FixtureTransport:
    def __init__(self) -> None:
        self.requests: list[tuple[str, bytes | None]] = []

    def request(self, url: str, body: bytes | None) -> bytes:
        self.requests.append((url, body))
        if url.endswith("/jobs"):
            return (FIXTURES / "list-page-0.json").read_bytes()
        if url.endswith("CUDA-Engineer_JR100001"):
            return (FIXTURES / "detail-JR100001.json").read_bytes()
        raise SourceError(f"fixture has no response for {url}")


class ScriptedTransport:
    def __init__(self, responses: dict[tuple[str, int | None], bytes]) -> None:
        self._responses = responses

    def request(self, url: str, body: bytes | None) -> bytes:
        offset = None if body is None else int(json.loads(body)["offset"])
        return self._responses[(url, offset)]


class FailingTransport:
    def request(self, url: str, body: bytes | None) -> bytes:
        raise SourceError("temporary source failure")


class DefaultBoundaryTransport:
    def request(self, url: str, body: bytes | None) -> bytes:
        if body is not None:
            request = json.loads(body)
            offset = int(request["offset"])
            limit = int(request["limit"])
            return json.dumps(
                {
                    "total": 200,
                    "jobPostings": [
                        {"externalPath": f"/job/Taiwan/Role_JR{index:06d}"}
                        for index in range(offset, offset + limit)
                    ],
                }
            ).encode()
        requisition = url.rsplit("_", 1)[-1]
        return _detail(requisition)


class RetryEveryBoundaryRequestTransport:
    def __init__(self) -> None:
        self._attempts: dict[tuple[str, bytes | None], int] = {}
        self._successful = DefaultBoundaryTransport()

    def request(self, url: str, body: bytes | None) -> bytes:
        key = (url, body)
        attempts = self._attempts.get(key, 0)
        self._attempts[key] = attempts + 1
        if attempts == 0:
            raise SourceError("temporary source failure")
        return self._successful.request(url, body)


class EmptyPartialSource:
    name = "nvidia-workday"

    def fetch(self) -> SourceFetchResult:
        return SourceFetchResult(
            (),
            {"source": self.name, "failures": ["detail failed"], "partial": True},
        )


def _detail(requisition: str) -> bytes:
    return json.dumps(
        {
            "jobPostingInfo": {
                "jobReqId": requisition,
                "title": requisition,
                "externalUrl": (
                    "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/"
                    f"job/Taiwan-Taipei/{requisition}"
                ),
            }
        }
    ).encode()


def test_fetches_detail_and_normalizes_a_taiwan_job() -> None:
    transport = FixtureTransport()

    result = NvidiaWorkdaySource(transport=transport, page_size=2, retries=0).fetch()

    assert len(result.observations) == 1
    job = result.observations[0].job
    assert job.source_name == "nvidia-workday"
    assert job.source_job_id == "JR100001"
    assert job.source_url == (
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/"
        "Taiwan-Taipei/CUDA-Engineer_JR100001"
    )
    assert job.title == "CUDA Engineer"
    assert job.location is not None
    assert job.location.country_code == "TW"
    assert job.location.region == "Taipei"
    assert job.description_text == "Build CUDA systems & tools."
    assert job.posted_at == "2026-08-25T00:00:00Z"
    assert result.receipt["listed"] == 2
    assert result.receipt["accepted"] == 1
    assert result.receipt["request_count"] == 3
    payload = json.loads(result.observations[0].raw_payload)
    assert payload["jobPostingInfo"]["jobReqId"] == "JR100001"


def test_collection_persists_partial_observations_and_health_receipt(
    tmp_path: Path,
) -> None:
    source = NvidiaWorkdaySource(transport=FixtureTransport(), page_size=2, retries=0)
    repository = SQLiteRepository(tmp_path / "history.sqlite3")

    result = CollectionService(repository).collect(
        source, run_id="nvidia-fixture-run", now="2026-08-27T00:00:00Z"
    )

    stored_run = repository.get_source_run(result.source_run_id)
    assert result.observation_results[0].source_job_id == "JR100001"
    assert stored_run.status == "completed"
    assert stored_run.receipt is not None
    assert json.loads(stored_run.receipt)["partial"] is True
    assert repository.get_job("nvidia-workday", "JR100001").source_url.endswith(
        "CUDA-Engineer_JR100001"
    )
    repository.close()


def test_partial_collection_does_not_age_unseen_jobs(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    initial = repository.start_source_run(
        "nvidia-workday", "2026-08-26T00:00:00Z", run_id="initial"
    )
    job = parse_job(
        {
            "schema_version": 1,
            "source_name": "nvidia-workday",
            "source_job_id": "JR-FICTIONAL",
            "source_url": "https://jobs.example/JR-FICTIONAL",
            "company": "Example",
            "title": "Engineer",
            "description_text": None,
            "location": None,
            "employment_type": None,
            "posted_at": None,
            "compensation": None,
        }
    )
    repository.record_observations(
        initial,
        (
            SourceObservation(
                job=job,
                observed_at="2026-08-26T00:01:00Z",
                raw_payload=b"{}",
            ),
        ),
    )
    repository.complete_source_run(initial, "2026-08-26T00:02:00Z")

    CollectionService(repository).collect(
        EmptyPartialSource(), run_id="partial", now="2026-08-27T00:00:00Z"
    )

    assert repository.get_job("nvidia-workday", "JR-FICTIONAL").state == "new"
    repository.close()


def test_stops_at_maximum_page_count_and_marks_receipt_partial() -> None:
    first_path = "/job/Taiwan-Taipei/First_JR100010"
    responses: dict[tuple[str, int | None], bytes] = {
        (
            "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/"
            "NVIDIAExternalCareerSite/jobs",
            0,
        ): json.dumps(
            {"total": 2, "jobPostings": [{"externalPath": first_path}]}
        ).encode(),
        (
            "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/"
            f"NVIDIAExternalCareerSite{first_path}",
            None,
        ): _detail("JR100010"),
    }

    result = NvidiaWorkdaySource(
        transport=ScriptedTransport(responses), page_size=1, max_pages=1
    ).fetch()

    assert [item.job.source_job_id for item in result.observations] == ["JR100010"]
    assert result.receipt["partial"] is True
    failures = cast(list[str], result.receipt["failures"])
    assert "page limit reached" in failures


def test_pagination_keeps_the_first_page_total_when_later_pages_report_zero() -> None:
    first_path = "/job/Taiwan-Taipei/First_JR100040"
    second_path = "/job/Taiwan-Taipei/Second_JR100041"
    list_url = (
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/"
        "NVIDIAExternalCareerSite/jobs"
    )
    responses: dict[tuple[str, int | None], bytes] = {
        (list_url, 0): json.dumps(
            {"total": 2, "jobPostings": [{"externalPath": first_path}]}
        ).encode(),
        (list_url, 1): json.dumps(
            {"total": 0, "jobPostings": [{"externalPath": second_path}]}
        ).encode(),
        (
            "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/"
            f"NVIDIAExternalCareerSite{first_path}",
            None,
        ): _detail("JR100040"),
        (
            "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/"
            f"NVIDIAExternalCareerSite{second_path}",
            None,
        ): _detail("JR100041"),
    }

    result = NvidiaWorkdaySource(
        transport=ScriptedTransport(responses), page_size=1
    ).fetch()

    assert result.receipt["total"] == 2
    assert result.receipt["partial"] is False
    assert [item.job.source_job_id for item in result.observations] == [
        "JR100040",
        "JR100041",
    ]


def test_malformed_first_list_payload_is_a_terminal_source_failure() -> None:
    with pytest.raises(SourceError, match="total must be a non-negative integer"):
        NvidiaWorkdaySource(
            transport=ScriptedTransport(
                {
                    (
                        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/"
                        "NVIDIAExternalCareerSite/jobs",
                        0,
                    ): b"{}"
                }
            )
        ).fetch()


def test_total_transport_outage_raises_source_error() -> None:
    with pytest.raises(SourceError, match="source collection failed"):
        NvidiaWorkdaySource(transport=FailingTransport(), retries=0).fetch()


def test_taiwan_country_descriptor_is_normalized_when_alpha_code_is_absent() -> None:
    path = "/job/Taiwan-Taipei/Descriptor_JR100050"
    list_url = (
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/"
        "NVIDIAExternalCareerSite/jobs"
    )
    detail = json.dumps(
        {
            "jobPostingInfo": {
                "jobReqId": "JR100050",
                "title": "Descriptor",
                "location": "Taiwan, Taipei",
                "country": {"descriptor": "Taiwan"},
                "externalUrl": (
                    "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/"
                    "job/Taiwan-Taipei/Descriptor_JR100050"
                ),
            }
        }
    ).encode()
    result = NvidiaWorkdaySource(
        transport=ScriptedTransport(
            {
                (list_url, 0): json.dumps(
                    {"total": 1, "jobPostings": [{"externalPath": path}]}
                ).encode(),
                (
                    "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/"
                    f"NVIDIAExternalCareerSite{path}",
                    None,
                ): detail,
            }
        )
    ).fetch()

    assert result.observations[0].job.location is not None
    assert result.observations[0].job.location.country_code == "TW"


def test_duplicate_requisition_is_not_stored_twice() -> None:
    first_path = "/job/Taiwan-Taipei/First_JR100020"
    second_path = "/job/Taiwan-Taipei/Second_JR100020"
    list_url = (
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/"
        "NVIDIAExternalCareerSite/jobs"
    )
    responses: dict[tuple[str, int | None], bytes] = {
        (list_url, 0): json.dumps(
            {
                "total": 2,
                "jobPostings": [
                    {"externalPath": first_path},
                    {"externalPath": second_path},
                ],
            }
        ).encode(),
        (
            "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/"
            f"NVIDIAExternalCareerSite{first_path}",
            None,
        ): _detail("JR100020"),
        (
            "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/"
            f"NVIDIAExternalCareerSite{second_path}",
            None,
        ): _detail("JR100020"),
    }

    result = NvidiaWorkdaySource(
        transport=ScriptedTransport(responses), page_size=2
    ).fetch()

    assert [item.job.source_job_id for item in result.observations] == ["JR100020"]
    assert result.receipt["partial"] is True
    failures = cast(list[str], result.receipt["failures"])
    assert "duplicate requisition JR100020" in failures


def test_request_limit_preserves_a_partial_failure_receipt() -> None:
    path = "/job/Taiwan-Taipei/One_JR100030"
    list_url = (
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/"
        "NVIDIAExternalCareerSite/jobs"
    )
    result = NvidiaWorkdaySource(
        transport=ScriptedTransport(
            {
                (list_url, 0): json.dumps(
                    {"total": 1, "jobPostings": [{"externalPath": path}]}
                ).encode()
            }
        ),
        max_requests=1,
    ).fetch()

    assert result.observations == ()
    assert result.receipt["request_count"] == 1
    assert result.receipt["partial"] is True
    failures = cast(list[str], result.receipt["failures"])
    assert "request limit reached" in failures[0]


def test_request_limit_includes_retries() -> None:
    with pytest.raises(SourceError, match="configured request limit reached"):
        NvidiaWorkdaySource(
            transport=FailingTransport(),
            max_requests=1,
            retries=1,
        ).fetch()


def test_default_request_budget_covers_the_full_default_page_boundary() -> None:
    result = NvidiaWorkdaySource(
        transport=DefaultBoundaryTransport(), retries=0
    ).fetch()

    assert len(result.observations) == 200
    assert result.receipt["listed"] == 200
    assert result.receipt["request_count"] == 210
    assert result.receipt["partial"] is False


def test_default_request_budget_covers_one_retry_at_the_full_boundary() -> None:
    result = NvidiaWorkdaySource(
        transport=RetryEveryBoundaryRequestTransport(), rate_limit_seconds=0
    ).fetch()

    assert len(result.observations) == 200
    assert result.receipt["listed"] == 200
    assert result.receipt["request_count"] == 420
    assert result.receipt["partial"] is False
