from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

import career_opportunity_monitor

ROOT = Path(__file__).parents[1]
SCHEMA_DIR = ROOT / "schemas" / "v1"
EXAMPLE_DIR = ROOT / "examples"


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def test_source_package_exposes_version() -> None:
    assert career_opportunity_monitor.__version__ == "0.1.0"


@pytest.mark.parametrize(
    ("schema_name", "example_name"),
    [
        ("resume-facts.schema.yaml", "resume_facts.yaml"),
        ("strategy.schema.yaml", "strategy/v1/strategy.yaml"),
        ("job.schema.yaml", "job.yaml"),
    ],
)
def test_fictional_example_validates_against_versioned_schema(
    schema_name: str, example_name: str
) -> None:
    schema = load_yaml(SCHEMA_DIR / schema_name)
    instance = load_yaml(EXAMPLE_DIR / example_name)

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(instance)  # pyright: ignore[reportUnknownMemberType]


def test_resume_contract_keeps_confirmed_and_planned_facts_distinct() -> None:
    resume = load_yaml(EXAMPLE_DIR / "resume_facts.yaml")

    statuses = {fact["status"] for fact in resume["facts"]}

    assert statuses == {"confirmed", "planned"}


def test_strategy_contract_freezes_score_and_llm_adjustment_limits() -> None:
    strategy = load_yaml(EXAMPLE_DIR / "strategy/v1/strategy.yaml")
    schema = load_yaml(SCHEMA_DIR / "strategy.schema.yaml")
    adjustment = schema["$defs"]["llmAdjustment"]["properties"]

    assert strategy["ranking"]["base_score"] == {"minimum": 0, "maximum": 100}
    assert strategy["ranking"]["llm_adjustment"] == {
        "enabled": False,
        "minimum": -5,
        "maximum": 5,
    }
    assert adjustment["enabled"]["default"] is False
    assert adjustment["minimum"]["const"] == -5
    assert adjustment["maximum"]["const"] == 5
