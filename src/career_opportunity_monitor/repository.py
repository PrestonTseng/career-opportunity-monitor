from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from .models import CanonicalJob

JobState = Literal["new", "changed", "unchanged", "stale", "closed"]
SourceRunStatus = Literal["running", "completed", "failed"]


class RepositoryError(RuntimeError):
    """Base error raised when durable history cannot accept an operation."""


class RepositoryLockedError(RepositoryError):
    """Raised when the durable single-writer lock cannot be acquired in time."""


@dataclass(frozen=True)
class SourceObservation:
    job: CanonicalJob
    observed_at: str
    raw_payload: bytes


@dataclass(frozen=True)
class ObservationResult:
    source_name: str
    source_job_id: str
    job_id: int
    job_version_id: int
    outcome: Literal["new", "changed", "unchanged"]


@dataclass(frozen=True)
class StoredJob:
    id: int
    source_name: str
    source_job_id: str
    source_url: str
    state: JobState
    current_version_id: int
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True)
class StoredSourceRun:
    id: int
    run_id: str
    source_name: str
    started_at: str
    finished_at: str | None
    status: SourceRunStatus
    receipt: bytes | None
    error: str | None


@runtime_checkable
class Repository(Protocol):
    """Storage port used by collection, evaluation, and reporting services."""

    def writer_lock(
        self,
        owner: str,
        *,
        timeout_seconds: float = 0.25,
        lease_seconds: float = 30.0,
        poll_seconds: float = 0.01,
    ) -> AbstractContextManager[None]:
        """Acquire exclusivity; lease time controls evidence, not ownership."""
        ...

    def start_source_run(
        self, source_name: str, started_at: str, *, run_id: str
    ) -> int: ...

    def record_observations(
        self, source_run_id: int, observations: tuple[SourceObservation, ...]
    ) -> tuple[ObservationResult, ...]: ...

    def complete_source_run(
        self,
        source_run_id: int,
        finished_at: str,
        *,
        complete: bool = True,
        receipt: bytes | None = None,
    ) -> None: ...

    def fail_source_run(
        self,
        source_run_id: int,
        finished_at: str,
        *,
        error: str,
        receipt: bytes | None = None,
    ) -> None: ...

    def get_source_run(self, source_run_id: int) -> StoredSourceRun: ...

    def get_job(self, source_name: str, source_job_id: str) -> StoredJob: ...

    def store_profile_snapshot(
        self, profile_hash: str, snapshot_bytes: bytes, created_at: str
    ) -> None: ...

    def store_strategy_snapshot(
        self, strategy_hash: str, snapshot_bytes: bytes, created_at: str
    ) -> None: ...

    def store_evaluation(
        self,
        job_version_id: int,
        profile_hash: str,
        strategy_hash: str,
        evaluation_bytes: bytes,
        created_at: str,
    ) -> int: ...

    def store_report(self, key: str, content: bytes, created_at: str) -> int: ...

    def get_report(self, key: str) -> bytes: ...

    def store_feedback(
        self, job_id: int, key: str, content: bytes, created_at: str
    ) -> int: ...

    def store_llm_assessment(
        self, evaluation_id: int, key: str, content: bytes, created_at: str
    ) -> int: ...
