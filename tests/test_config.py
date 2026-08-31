from __future__ import annotations

import copy
import hashlib
import shutil
from pathlib import Path
from typing import cast

import pytest
import yaml

from career_opportunity_monitor.config import ConfigError, load_configuration

ROOT = Path(__file__).parents[1]
RESUME = ROOT / "examples" / "resume_facts.yaml"
STRATEGY_DIR = ROOT / "examples" / "strategy" / "v1"


def _strategy_dict() -> dict[str, object]:
    strategy = yaml.safe_load((STRATEGY_DIR / "strategy.yaml").read_text())
    assert isinstance(strategy, dict)
    return cast(dict[str, object], strategy)


def _sources_dict() -> dict[str, object]:
    sources = yaml.safe_load((STRATEGY_DIR / "sources.yaml").read_text())
    assert isinstance(sources, dict)
    return cast(dict[str, object], sources)


def _write_strategy(tmp_path: Path, strategy: dict[str, object]) -> Path:
    directory = tmp_path / "strategy"
    directory.mkdir()
    (directory / "strategy.yaml").write_text(yaml.safe_dump(strategy), encoding="utf-8")
    shutil.copyfile(STRATEGY_DIR / "sources.yaml", directory / "sources.yaml")
    shutil.copyfile(STRATEGY_DIR / "destinations.yaml", directory / "destinations.yaml")
    shutil.copyfile(STRATEGY_DIR / "schedule.yaml", directory / "schedule.yaml")
    return directory


def _write_configuration(tmp_path: Path, sources: dict[str, object]) -> Path:
    directory = tmp_path / "configuration"
    directory.mkdir()
    shutil.copyfile(STRATEGY_DIR / "strategy.yaml", directory / "strategy.yaml")
    (directory / "sources.yaml").write_text(yaml.safe_dump(sources), encoding="utf-8")
    shutil.copyfile(STRATEGY_DIR / "destinations.yaml", directory / "destinations.yaml")
    shutil.copyfile(STRATEGY_DIR / "schedule.yaml", directory / "schedule.yaml")
    return directory


def test_load_configuration_compiles_repeatable_snapshot() -> None:
    first = load_configuration(RESUME, STRATEGY_DIR)
    second = load_configuration(RESUME, STRATEGY_DIR)

    assert first == second
    assert len(first.profile_hash) == 64
    assert len(first.strategy_hash) == 64
    assert first.profile.profile_id == "alex-chen-fictional"
    assert first.strategy.strategy_id == "taiwan-software-fictional"
    assert first.strategy.referral_available == ("Example Semiconductor",)
    assert first.strategy.llm_adjustment_enabled is False
    assert first.strategy.llm_adjustment_minimum == -5
    assert first.strategy.llm_adjustment_maximum == 5


def test_load_configuration_compiles_versioned_sources_in_document_order() -> None:
    loaded = load_configuration(RESUME, STRATEGY_DIR)

    assert [source.id for source in loaded.sources] == [
        "example-workday",
        "second-example-workday",
    ]
    assert loaded.sources[0].adapter == "workday"
    assert loaded.sources[0].origin == "https://example.wd5.myworkdayjobs.com"
    assert loaded.sources[1].enabled is True
    assert len(loaded.sources_hash) == 64
    assert (STRATEGY_DIR / "sources.yaml").read_bytes() in loaded.sources_snapshot_bytes


def test_loaded_configuration_carries_hashed_validated_snapshot_bytes() -> None:
    loaded = load_configuration(RESUME, STRATEGY_DIR)

    assert hashlib.sha256(loaded.profile_snapshot_bytes).hexdigest() == (
        loaded.profile_hash
    )
    assert hashlib.sha256(loaded.strategy_snapshot_bytes).hexdigest() == (
        loaded.strategy_hash
    )
    assert RESUME.read_bytes() in loaded.profile_snapshot_bytes
    assert (STRATEGY_DIR / "strategy.yaml").read_bytes() in (
        loaded.strategy_snapshot_bytes
    )


def test_load_configuration_rejects_duplicate_source_ids(tmp_path: Path) -> None:
    sources = _sources_dict()
    entries = cast(list[object], sources["sources"])
    duplicate = copy.deepcopy(entries[0])
    entries.append(duplicate)

    with pytest.raises(ConfigError, match="duplicate source id"):
        load_configuration(RESUME, _write_configuration(tmp_path, sources))


@pytest.mark.parametrize(
    "origin",
    [
        "http://jobs.example.com",
        "https://user@jobs.example.com",
        "https://jobs.example.com/#fragment",
        "https://localhost",
        "https://127.0.0.1",
        "https://169.254.169.254",
        "https://jobs.example.com.evil.invalid/path",
    ],
)
def test_load_configuration_rejects_unapproved_source_origins(
    tmp_path: Path, origin: str
) -> None:
    sources = _sources_dict()
    entries = cast(list[object], sources["sources"])
    first = cast(dict[str, object], entries[0])
    first["origin"] = origin

    with pytest.raises(ConfigError, match="source origin"):
        load_configuration(RESUME, _write_configuration(tmp_path, sources))


def test_load_configuration_rejects_unknown_adapter(tmp_path: Path) -> None:
    sources = _sources_dict()
    entries = cast(list[object], sources["sources"])
    first = cast(dict[str, object], entries[0])
    first["adapter"] = "python-entry-point"

    with pytest.raises(ConfigError, match="invalid sources at sources.0.adapter"):
        load_configuration(RESUME, _write_configuration(tmp_path, sources))


def test_load_configuration_rejects_invalid_ranking_invariants(
    tmp_path: Path,
) -> None:
    strategy = _strategy_dict()
    ranking = cast(dict[str, object], strategy["ranking"])
    weights = cast(dict[str, int], ranking["weights"])
    weights["title"] = 29
    strategy_dir = _write_strategy(tmp_path, strategy)

    with pytest.raises(ConfigError, match="weights must total exactly 100"):
        load_configuration(RESUME, strategy_dir)


def test_load_configuration_rejects_cap_above_weight(tmp_path: Path) -> None:
    strategy = _strategy_dict()
    ranking = cast(dict[str, object], strategy["ranking"])
    caps = cast(dict[str, int], ranking["caps"])
    caps["company"] = 6
    strategy_dir = _write_strategy(tmp_path, strategy)

    with pytest.raises(ConfigError, match="cap for company cannot exceed its weight"):
        load_configuration(RESUME, strategy_dir)


def test_load_configuration_rejects_duplicate_fact_ids(tmp_path: Path) -> None:
    resume = yaml.safe_load(RESUME.read_text())
    assert isinstance(resume, dict)
    typed_resume = cast(dict[str, object], resume)
    facts = cast(list[object], typed_resume["facts"])
    facts.append(copy.deepcopy(facts[0]))
    resume_path = tmp_path / "resume_facts.yaml"
    resume_path.write_text(yaml.safe_dump(resume), encoding="utf-8")

    with pytest.raises(ConfigError, match="duplicate resume fact id"):
        load_configuration(resume_path, STRATEGY_DIR)


def test_load_configuration_rejects_changed_input_snapshot(tmp_path: Path) -> None:
    resume_path = tmp_path / "resume_facts.yaml"
    shutil.copyfile(RESUME, resume_path)

    def change_resume_after_read() -> None:
        resume_path.write_bytes(resume_path.read_bytes() + b"\n")

    with pytest.raises(ConfigError, match="configuration changed after reading"):
        load_configuration(
            resume_path, STRATEGY_DIR, after_read=change_resume_after_read
        )


def test_load_configuration_rejects_symlinked_input(tmp_path: Path) -> None:
    resume_path = tmp_path / "resume_facts.yaml"
    resume_path.symlink_to(RESUME)

    with pytest.raises(ConfigError, match="unsafe configuration file shape"):
        load_configuration(resume_path, STRATEGY_DIR)


def test_referral_available_rejects_personal_contact_details(tmp_path: Path) -> None:
    strategy = _strategy_dict()
    companies = cast(dict[str, object], strategy["companies"])
    companies["referral_available"] = ["Example Semiconductor +886 912 345 678"]
    strategy_dir = _write_strategy(tmp_path, strategy)

    with pytest.raises(ConfigError, match="company names, not contact details"):
        load_configuration(RESUME, strategy_dir)


def test_load_configuration_rejects_role_target_without_tokens(tmp_path: Path) -> None:
    strategy = _strategy_dict()
    strategy["role_targets"] = ["---"]
    strategy_dir = _write_strategy(tmp_path, strategy)

    with pytest.raises(
        ConfigError, match="role target must contain alphanumeric tokens"
    ):
        load_configuration(RESUME, strategy_dir)


def test_load_configuration_rejects_unknown_resume_schema_version(
    tmp_path: Path,
) -> None:
    resume = yaml.safe_load(RESUME.read_text())
    assert isinstance(resume, dict)
    typed_resume = cast(dict[str, object], resume)
    typed_resume["schema_version"] = 2
    resume_path = tmp_path / "resume_facts.yaml"
    resume_path.write_text(yaml.safe_dump(typed_resume), encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid resume profile at schema_version"):
        load_configuration(resume_path, STRATEGY_DIR)
