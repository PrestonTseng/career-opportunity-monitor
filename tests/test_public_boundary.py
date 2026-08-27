from pathlib import Path

ROOT = Path(__file__).parents[1]
PRIVATE_PATHS = {
    "/.runtime/",
    "/resume_facts.yaml",
    "/strategy/",
    "/data/",
    "/reports/",
    "/receipts/",
    "/logs/",
    ".env",
}


def lines(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_ignore_files_exclude_private_runtime_paths() -> None:
    for name in (".gitignore", ".dockerignore"):
        assert lines(ROOT / name) >= PRIVATE_PATHS


def test_environment_example_disables_optional_llm_and_has_no_key() -> None:
    environment = lines(ROOT / ".env.example")

    assert "CAREER_MONITOR_LLM_MODE=off" in environment
    assert any(
        value.startswith("CAREER_MONITOR_LLM_SECRET_FILE=") for value in environment
    )
    assert not any(
        value.startswith("CAREER_MONITOR_LLM_API_KEY=") for value in environment
    )


def test_license_file_records_the_unresolved_decision() -> None:
    decision = (ROOT / "LICENSE.md").read_text(encoding="utf-8")

    assert "No open-source license is selected" in decision
    assert "All rights are reserved" in decision
