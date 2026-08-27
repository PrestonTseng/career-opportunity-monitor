from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from .nvidia_workday import SourceError
from .repository import ObservationResult, Repository, SourceObservation
from .source import JobSource


@dataclass(frozen=True)
class CollectionResult:
    source_run_id: int
    observations: tuple[SourceObservation, ...]
    observation_results: tuple[ObservationResult, ...]
    receipt: dict[str, object]


class CollectionService:
    """Persist a JobSource run with a durable, inspectable health receipt."""

    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    def collect(
        self, source: JobSource, *, run_id: str, now: str | None = None
    ) -> CollectionResult:
        started_at = now or _now()
        source_run_id = self._repository.start_source_run(
            source.name, started_at, run_id=run_id
        )
        try:
            fetched = source.fetch()
            observations = fetched.observations
            results = self._repository.record_observations(source_run_id, observations)
            receipt = _receipt_bytes(fetched.receipt)
            partial = fetched.receipt.get("partial", False)
            if not isinstance(partial, bool):
                raise SourceError("source receipt partial value must be a boolean")
            self._repository.complete_source_run(
                source_run_id,
                _now() if now is None else now,
                complete=not partial,
                receipt=receipt,
            )
        except SourceError as exc:
            receipt = _receipt_bytes({"source": source.name, "partial": False})
            self._repository.fail_source_run(
                source_run_id,
                _now() if now is None else now,
                error=str(exc),
                receipt=receipt,
            )
            raise
        return CollectionResult(source_run_id, observations, results, fetched.receipt)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _receipt_bytes(receipt: dict[str, object]) -> bytes:
    return json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
