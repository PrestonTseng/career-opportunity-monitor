from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

Category = Literal["title", "skills", "experience", "location", "company"]
FactKind = Literal[
    "certification", "education", "employment", "language", "project", "skill"
]
FactStatus = Literal["confirmed", "planned"]
SourceAdapter = Literal["workday"]
ReportCadence = Literal["daily", "weekly"]

CATEGORIES: tuple[Category, ...] = (
    "title",
    "skills",
    "experience",
    "location",
    "company",
)


@dataclass(frozen=True)
class ResumeFact:
    id: str
    kind: FactKind
    status: FactStatus
    statement: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class CompiledProfile:
    profile_id: str
    display_name: str
    country_code: str
    region: str | None
    facts: tuple[ResumeFact, ...]


@dataclass(frozen=True)
class Market:
    country_code: str
    regions: tuple[str, ...]
    remote: bool


@dataclass(frozen=True)
class Strategy:
    strategy_id: str
    role_targets: tuple[str, ...]
    markets: tuple[Market, ...]
    preferred_companies: tuple[str, ...]
    excluded_companies: tuple[str, ...]
    referral_available: tuple[str, ...]
    weights: tuple[tuple[Category, Decimal], ...]
    caps: tuple[tuple[Category, Decimal], ...]
    llm_adjustment_enabled: bool
    llm_adjustment_minimum: int
    llm_adjustment_maximum: int

    def weight(self, category: Category) -> Decimal:
        return dict(self.weights)[category]

    def cap(self, category: Category) -> Decimal:
        return dict(self.caps)[category]


@dataclass(frozen=True)
class WorkdayOptions:
    page_size: int
    max_pages: int
    max_requests: int
    timeout_seconds: float
    response_limit_bytes: int
    retries: int
    rate_limit_seconds: float


@dataclass(frozen=True)
class SourceConfiguration:
    id: str
    enabled: bool
    adapter: SourceAdapter
    origin: str
    tenant: str
    site: str
    company: str
    search_text: str
    options: WorkdayOptions


@dataclass(frozen=True)
class DestinationConfiguration:
    id: str
    enabled: bool
    type: Literal["discord"]
    report_cadences: tuple[ReportCadence, ...]
    webhook_url_file: Path


@dataclass(frozen=True)
class LoadedConfiguration:
    profile: CompiledProfile
    strategy: Strategy
    sources: tuple[SourceConfiguration, ...]
    destinations: tuple[DestinationConfiguration, ...]
    profile_hash: str
    strategy_hash: str
    sources_hash: str
    destinations_hash: str
    profile_snapshot_bytes: bytes
    strategy_snapshot_bytes: bytes
    sources_snapshot_bytes: bytes
    destinations_snapshot_bytes: bytes


WorkMode = Literal["office", "hybrid", "remote", "unknown"]


@dataclass(frozen=True)
class JobLocation:
    country_code: str | None
    region: str | None
    work_mode: WorkMode | None


@dataclass(frozen=True)
class Compensation:
    minimum: Decimal
    maximum: Decimal
    currency: str
    period: str


@dataclass(frozen=True)
class CanonicalJob:
    schema_version: int
    source_name: str
    source_job_id: str
    source_url: str
    company: str
    title: str
    description_text: str | None
    location: JobLocation | None
    employment_type: str | None
    posted_at: str | None
    compensation: Compensation | None


@dataclass(frozen=True)
class CategoryEvaluation:
    category: Category
    raw_score: Decimal | None
    weight: Decimal
    cap: Decimal
    points: Decimal
    matched_phrases: tuple[str, ...]
    resume_fact_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    missing_reason: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "cap": f"{self.cap:.2f}",
            "category": self.category,
            "matched_phrases": list(self.matched_phrases),
            "missing_reason": self.missing_reason,
            "points": f"{self.points:.2f}",
            "raw_score": (
                None if self.raw_score is None else format(self.raw_score, "f")
            ),
            "reasons": list(self.reasons),
            "resume_fact_ids": list(self.resume_fact_ids),
            "weight": f"{self.weight:.2f}",
        }


@dataclass(frozen=True)
class Evaluation:
    source_name: str
    source_job_id: str
    profile_hash: str
    strategy_hash: str
    base_score: Decimal
    confidence: Decimal
    categories: tuple[CategoryEvaluation, ...]

    def to_json_bytes(self) -> bytes:
        value: dict[str, object] = {
            "base_score": f"{self.base_score:.2f}",
            "categories": [category.as_dict() for category in self.categories],
            "confidence": f"{self.confidence:.2f}",
            "profile_hash": self.profile_hash,
            "source_job_id": self.source_job_id,
            "source_name": self.source_name,
            "strategy_hash": self.strategy_hash,
        }
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
