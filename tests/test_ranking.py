from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
import yaml

from career_opportunity_monitor.config import ConfigError, load_configuration
from career_opportunity_monitor.ranking import evaluate_job, parse_job

ROOT = Path(__file__).parents[1]
CONFIG = load_configuration(
    ROOT / "examples" / "resume_facts.yaml", ROOT / "examples" / "strategy" / "v1"
)


def _example_job() -> dict[str, object]:
    value = yaml.safe_load((ROOT / "examples" / "job.yaml").read_text())
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_evaluation_is_deterministic_and_explains_each_category() -> None:
    job = parse_job(_example_job())

    first = evaluate_job(CONFIG, job)
    second = evaluate_job(CONFIG, job)

    assert first == second
    assert first.to_json_bytes() == second.to_json_bytes()
    assert first.profile_hash == CONFIG.profile_hash
    assert first.strategy_hash == CONFIG.strategy_hash
    assert first.base_score == Decimal("80.00")
    assert first.confidence == Decimal("0.80")

    categories = {result.category: result for result in first.categories}
    assert categories["title"].raw_score == Decimal("1.00")
    assert categories["title"].points == Decimal("30.00")
    assert categories["skills"].matched_phrases == (
        "data processing",
        "python",
        "testing",
    )
    assert categories["skills"].resume_fact_ids == ("fact-python-services",)
    assert categories["experience"].raw_score is None
    assert categories["experience"].missing_reason == (
        "no eligible confirmed experience keywords"
    )
    assert categories["experience"].points == Decimal("0.00")
    assert categories["location"].points == Decimal("15.00")
    assert categories["company"].points == Decimal("5.00")
    assert all(result.reasons for result in first.categories)

    encoded = json.loads(first.to_json_bytes())
    assert encoded["base_score"] == "80.00"
    assert encoded["profile_hash"] == CONFIG.profile_hash


def test_planned_facts_never_contribute_points_or_evidence() -> None:
    raw_job = _example_job()
    raw_job["description_text"] = "Cloud architecture"
    evaluation = evaluate_job(CONFIG, parse_job(raw_job))
    skills = next(item for item in evaluation.categories if item.category == "skills")

    assert "cloud architecture" not in skills.matched_phrases
    assert "fact-cloud-certification" not in skills.resume_fact_ids
    assert skills.raw_score == Decimal("0.00")
    assert skills.points == Decimal("0.00")


def test_missing_description_is_unknown_without_score_renormalization() -> None:
    raw_job = _example_job()
    raw_job["description_text"] = None
    evaluation = evaluate_job(CONFIG, parse_job(raw_job))
    categories = {result.category: result for result in evaluation.categories}

    assert categories["skills"].raw_score is None
    assert categories["skills"].points == Decimal("0.00")
    assert categories["experience"].raw_score is None
    assert evaluation.base_score == Decimal("50.00")
    assert evaluation.confidence == Decimal("0.50")


def test_parse_job_preserves_compensation_contract() -> None:
    raw_job = _example_job()
    raw_job["compensation"] = {
        "minimum": 1_500_000,
        "maximum": 2_000_000,
        "currency": "TWD",
        "period": "year",
    }

    job = parse_job(raw_job)

    assert job.compensation is not None
    assert job.compensation.minimum == Decimal("1500000")
    assert job.compensation.maximum == Decimal("2000000")
    assert job.compensation.currency == "TWD"
    assert job.compensation.period == "year"


def test_parse_job_keeps_omitted_location_details_unknown() -> None:
    raw_job = _example_job()
    raw_job["location"] = {"country_code": "TW"}

    job = parse_job(raw_job)

    assert job.location is not None
    assert job.location.country_code == "TW"
    assert job.location.region is None
    assert job.location.work_mode is None


@pytest.mark.parametrize("invalid_version", [0, 2])
def test_parse_job_rejects_invalid_schema_version(invalid_version: int) -> None:
    raw_job = _example_job()
    raw_job["schema_version"] = invalid_version

    with pytest.raises(ConfigError, match="invalid canonical job at schema_version"):
        parse_job(raw_job)


def test_parse_job_rejects_non_url_source_url() -> None:
    raw_job = _example_job()
    raw_job["source_url"] = "not a URL"

    with pytest.raises(ConfigError, match="invalid canonical job at source_url"):
        parse_job(raw_job)


@pytest.mark.parametrize(
    ("location", "expected_raw", "expected_reason"),
    [
        (
            {"country_code": "US", "region": None, "work_mode": "remote"},
            Decimal("1.0"),
            "remote work is permitted",
        ),
        (
            {"country_code": None, "region": None, "work_mode": "remote"},
            Decimal("1.0"),
            "remote work is permitted",
        ),
        (
            {"country_code": "TW", "region": "Kaohsiung", "work_mode": "office"},
            Decimal("0.7"),
            "country matches a configured market",
        ),
        (
            {"country_code": "US", "region": None, "work_mode": "office"},
            Decimal("0.0"),
            "country does not match a configured market",
        ),
        (
            {"country_code": None, "region": None, "work_mode": "unknown"},
            None,
            "source country is missing",
        ),
    ],
)
def test_location_rules_follow_approved_priority(
    location: dict[str, object],
    expected_raw: Decimal | None,
    expected_reason: str,
) -> None:
    raw_job = _example_job()
    raw_job["location"] = location
    evaluation = evaluate_job(CONFIG, parse_job(raw_job))
    result = next(item for item in evaluation.categories if item.category == "location")

    assert result.raw_score == expected_raw
    assert result.reasons == (expected_reason,)


def test_excluded_company_takes_priority_over_preference_and_referral() -> None:
    strategy = replace(
        CONFIG.strategy,
        excluded_companies=("Example Semiconductor",),
    )
    configuration = replace(CONFIG, strategy=strategy)

    evaluation = evaluate_job(configuration, parse_job(_example_job()))
    company = next(item for item in evaluation.categories if item.category == "company")

    assert company.raw_score == Decimal("0.0")
    assert company.points == Decimal("0.00")
    assert company.reasons == ("company is excluded",)


def test_title_points_use_nfkc_tokens_and_decimal_half_up_rounding() -> None:
    target = " ".join(f"token{number}" for number in range(16))
    strategy = replace(CONFIG.strategy, role_targets=(target,))
    configuration = replace(CONFIG, strategy=strategy)
    raw_job = _example_job()
    raw_job["title"] = "ＴＯＫＥＮ０"

    evaluation = evaluate_job(configuration, parse_job(raw_job))
    title = next(item for item in evaluation.categories if item.category == "title")

    assert title.raw_score == Decimal("0.0625")
    assert title.points == Decimal("1.88")
