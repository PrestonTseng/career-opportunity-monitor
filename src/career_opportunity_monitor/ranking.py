from __future__ import annotations

import unicodedata
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from .config import ConfigError, load_contract_schema
from .models import (
    CATEGORIES,
    CanonicalJob,
    Category,
    CategoryEvaluation,
    Compensation,
    Evaluation,
    JobLocation,
    LoadedConfiguration,
    ResumeFact,
    Strategy,
    WorkMode,
)

_TWO_PLACES = Decimal("0.01")
_SKILL_KINDS = frozenset(("skill", "language", "certification"))
_EXPERIENCE_KINDS = frozenset(("employment", "project", "education"))


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    separated = "".join(
        character if character.isalnum() else " " for character in normalized
    )
    return " ".join(separated.split())


def parse_job(value: dict[str, object]) -> CanonicalJob:
    """Validate and compile one canonical version-one job."""
    try:
        validator = Draft202012Validator(
            load_contract_schema("job.schema.yaml"), format_checker=FormatChecker()
        )
        validator.validate(value)  # pyright: ignore[reportUnknownMemberType]
    except ValidationError as exc:
        pointer = ".".join(str(part) for part in exc.absolute_path) or "root"
        raise ConfigError(f"invalid canonical job at {pointer}: {exc.message}") from exc

    raw_location = value.get("location")
    location: JobLocation | None = None
    if raw_location is not None:
        item = cast(dict[str, object], raw_location)
        location = JobLocation(
            country_code=cast(str | None, item.get("country_code")),
            region=cast(str | None, item.get("region")),
            work_mode=cast(WorkMode | None, item.get("work_mode")),
        )
    raw_compensation = value.get("compensation")
    compensation: Compensation | None = None
    if raw_compensation is not None:
        item = cast(dict[str, object], raw_compensation)
        compensation = Compensation(
            minimum=Decimal(str(item["minimum"])),
            maximum=Decimal(str(item["maximum"])),
            currency=cast(str, item["currency"]),
            period=cast(str, item["period"]),
        )
    return CanonicalJob(
        schema_version=cast(int, value["schema_version"]),
        source_name=cast(str, value["source_name"]),
        source_job_id=cast(str, value["source_job_id"]),
        source_url=cast(str, value["source_url"]),
        company=cast(str, value["company"]),
        title=cast(str, value["title"]),
        description_text=cast(str | None, value.get("description_text")),
        location=location,
        employment_type=cast(str | None, value.get("employment_type")),
        posted_at=cast(str | None, value.get("posted_at")),
        compensation=compensation,
    )


def _round(value: Decimal) -> Decimal:
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def _known_result(
    strategy: Strategy,
    category: Category,
    raw_score: Decimal,
    *,
    matched_phrases: tuple[str, ...] = (),
    fact_ids: tuple[str, ...] = (),
    reasons: tuple[str, ...],
) -> CategoryEvaluation:
    weight = strategy.weight(category)
    cap = strategy.cap(category)
    points = _round(min(raw_score * weight, cap))
    return CategoryEvaluation(
        category=category,
        raw_score=raw_score,
        weight=weight,
        cap=cap,
        points=points,
        matched_phrases=matched_phrases,
        resume_fact_ids=fact_ids,
        reasons=reasons,
        missing_reason=None,
    )


def _unknown_result(
    strategy: Strategy, category: Category, reason: str
) -> CategoryEvaluation:
    return CategoryEvaluation(
        category=category,
        raw_score=None,
        weight=strategy.weight(category),
        cap=strategy.cap(category),
        points=Decimal("0.00"),
        matched_phrases=(),
        resume_fact_ids=(),
        reasons=(reason,),
        missing_reason=reason,
    )


def _title_result(strategy: Strategy, job: CanonicalJob) -> CategoryEvaluation:
    title_tokens = frozenset(normalize_text(job.title).split())
    candidates: list[tuple[Decimal, str, tuple[str, ...]]] = []
    for target in strategy.role_targets:
        tokens = tuple(normalize_text(target).split())
        matched = tuple(token for token in tokens if token in title_tokens)
        with localcontext() as context:
            context.prec = 28
            ratio = Decimal(len(matched)) / Decimal(len(tokens))
        candidates.append((ratio, normalize_text(target), matched))
    raw, target, matched = max(candidates, key=lambda item: (item[0], item[1]))
    return _known_result(
        strategy,
        "title",
        raw,
        matched_phrases=matched,
        reasons=(
            f"matched {len(matched)} of {len(target.split())} "
            f"target tokens for {target}",
        ),
    )


def _keyword_evidence(
    facts: tuple[ResumeFact, ...], eligible_kinds: frozenset[str]
) -> dict[str, set[str]]:
    evidence: dict[str, set[str]] = {}
    for fact in facts:
        if fact.status != "confirmed" or fact.kind not in eligible_kinds:
            continue
        for keyword in fact.keywords:
            normalized = normalize_text(keyword)
            if normalized:
                evidence.setdefault(normalized, set()).add(fact.id)
    return evidence


def _evidence_result(
    configuration: LoadedConfiguration,
    job: CanonicalJob,
    category: Category,
    eligible_kinds: frozenset[str],
) -> CategoryEvaluation:
    evidence = _keyword_evidence(configuration.profile.facts, eligible_kinds)
    if not evidence:
        return _unknown_result(
            configuration.strategy,
            category,
            f"no eligible confirmed {category} keywords",
        )
    if job.description_text is None:
        return _unknown_result(
            configuration.strategy, category, "job description is missing"
        )

    text = f" {normalize_text(job.title + ' ' + job.description_text)} "
    matched = tuple(sorted(keyword for keyword in evidence if f" {keyword} " in text))
    fact_ids = tuple(
        sorted({fact_id for keyword in matched for fact_id in evidence[keyword]})
    )
    with localcontext() as context:
        context.prec = 28
        raw = Decimal(len(matched)) / Decimal(len(evidence))
    return _known_result(
        configuration.strategy,
        category,
        raw,
        matched_phrases=matched,
        fact_ids=fact_ids,
        reasons=(
            f"matched {len(matched)} of {len(evidence)} eligible confirmed keywords",
        ),
    )


def _location_result(strategy: Strategy, job: CanonicalJob) -> CategoryEvaluation:
    location = job.location
    if location is None:
        return _unknown_result(strategy, "location", "source country is missing")
    if location.work_mode == "remote" and any(
        market.remote for market in strategy.markets
    ):
        return _known_result(
            strategy, "location", Decimal("1.0"), reasons=("remote work is permitted",)
        )
    if location.country_code is None:
        return _unknown_result(strategy, "location", "source country is missing")

    country_markets = tuple(
        market
        for market in strategy.markets
        if market.country_code == location.country_code
    )
    if not country_markets:
        return _known_result(
            strategy,
            "location",
            Decimal("0.0"),
            reasons=("country does not match a configured market",),
        )
    normalized_region = normalize_text(location.region) if location.region else None
    if normalized_region is not None and any(
        normalized_region == normalize_text(region)
        for market in country_markets
        for region in market.regions
    ):
        return _known_result(
            strategy,
            "location",
            Decimal("1.0"),
            matched_phrases=(normalized_region,),
            reasons=("country and region match a configured market",),
        )
    return _known_result(
        strategy,
        "location",
        Decimal("0.7"),
        reasons=("country matches a configured market",),
    )


def _company_result(strategy: Strategy, job: CanonicalJob) -> CategoryEvaluation:
    company = normalize_text(job.company)
    preferred = {normalize_text(value) for value in strategy.preferred_companies}
    excluded = {normalize_text(value) for value in strategy.excluded_companies}
    referral = {normalize_text(value) for value in strategy.referral_available}
    if company in excluded:
        raw, reason = Decimal("0.0"), "company is excluded"
    elif company in preferred and company in referral:
        raw, reason = Decimal("1.0"), "company is preferred and has a referral path"
    elif company in preferred or company in referral:
        raw, reason = Decimal("0.8"), "company is preferred or has a referral path"
    else:
        raw, reason = Decimal("0.5"), "company is known and neutral"
    return _known_result(
        strategy,
        "company",
        raw,
        matched_phrases=(company,),
        reasons=(reason,),
    )


def evaluate_job(configuration: LoadedConfiguration, job: CanonicalJob) -> Evaluation:
    """Produce one pure, deterministic, explainable evaluation."""
    results = (
        _title_result(configuration.strategy, job),
        _evidence_result(configuration, job, "skills", _SKILL_KINDS),
        _evidence_result(configuration, job, "experience", _EXPERIENCE_KINDS),
        _location_result(configuration.strategy, job),
        _company_result(configuration.strategy, job),
    )
    assert tuple(result.category for result in results) == CATEGORIES
    base_score = _round(sum((result.points for result in results), Decimal()))
    known_weight = sum(
        (result.weight for result in results if result.raw_score is not None), Decimal()
    )
    confidence = _round(known_weight / Decimal(100))
    return Evaluation(
        source_name=job.source_name,
        source_job_id=job.source_job_id,
        profile_hash=configuration.profile_hash,
        strategy_hash=configuration.strategy_hash,
        base_score=base_score,
        confidence=confidence,
        categories=results,
    )
