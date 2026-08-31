from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .repository import SourceObservation


class SourceError(RuntimeError):
    """A configured public source returned unavailable or invalid data."""


@dataclass(frozen=True)
class SourceFetchResult:
    observations: tuple[SourceObservation, ...]
    receipt: dict[str, object]


@runtime_checkable
class JobSource(Protocol):
    """A bounded, public job source that returns canonical observations."""

    @property
    def name(self) -> str: ...

    def fetch(self) -> SourceFetchResult: ...
