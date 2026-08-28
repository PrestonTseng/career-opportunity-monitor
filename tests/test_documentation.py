import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def _text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_readme_local_document_links_resolve() -> None:
    links = re.findall(r"\[[^]]+\]\(([^)]+\.md)\)", _text("README.md"))

    assert links
    assert all((ROOT / link).is_file() for link in links)


def test_onboarding_defines_one_private_layout_and_safe_mode_boundary() -> None:
    readme = _text("README.md")
    environment = _text(".env.example")

    required_private_paths = {
        "private/resume_facts.yaml",
        "private/config",
        "private/secrets/discord-webhook",
    }
    selected_paths = set(re.findall(r"private/[\w./-]+", readme + environment))
    assert required_private_paths <= selected_paths
    assert "CAREER_MONITOR_MODE=production" in environment
    assert "CAREER_MONITOR_MODE=demo" in readme
    assert "discord.com/api/webhooks" not in environment


def test_docs_cover_each_operator_role_and_recovery_path() -> None:
    operations = _text("docs/operations.md")
    commands = set(
        re.findall(r"cli (validate|daily|weekly|retry-delivery)", operations)
    )

    assert commands == {"validate", "daily", "weekly", "retry-delivery"}
    assert all(
        heading in operations
        for heading in ("## Backup", "## Restore", "## Troubleshooting")
    )
    assert "--cadence weekly" in operations
    assert "--dry-run" in operations


def test_configuration_docs_match_compose_private_mounts() -> None:
    compose = yaml.safe_load(_text("compose.yaml"))
    runtime = compose["x-runtime"]
    environment = runtime["environment"]
    volume_sources = {item["source"] for item in runtime["volumes"]}
    configuration = _text("docs/configuration.md")

    assert environment["CAREER_MONITOR_MODE"] == "${CAREER_MONITOR_MODE:-production}"
    assert (
        "${CAREER_MONITOR_RESUME_FILE:-./private/resume_facts.yaml}" in volume_sources
    )
    assert "${CAREER_MONITOR_CONFIG_DIR:-./private/config}" in volume_sources
    assert all(
        name in configuration
        for name in (
            "resume_facts.yaml",
            "strategy.yaml",
            "sources.yaml",
            "destinations.yaml",
            "schedule.yaml",
        )
    )


def test_schedule_docs_define_timezone_and_dst_behavior() -> None:
    configuration = _text("docs/configuration.md")
    operations = _text("docs/operations.md")

    assert "IANA timezone" in configuration
    assert "local wall time" in configuration
    assert "DST spring" in configuration
    assert "DST fall" in configuration
    assert "prior completed local week" in operations


def test_provider_example_is_not_product_framing() -> None:
    adapter = _text("docs/workday-adapter.md")
    generic_docs = (
        _text("README.md"),
        _text("docs/architecture.md"),
        _text("docs/configuration.md"),
        _text("docs/operations.md"),
        _text("SECURITY.md"),
    )

    assert "NVIDIA adapter example" in adapter
    assert all("NVIDIA" not in text for text in generic_docs)


def test_migration_preserves_v1_contracts_and_defines_new_documents() -> None:
    migration = _text("docs/migration-v1.md")

    assert (
        "keeps the version 1 resume, strategy, job, score, and storage contracts"
        in migration
    )
    assert all(
        name in migration
        for name in ("sources.yaml", "destinations.yaml", "schedule.yaml")
    )
    assert "matching backup" in migration


def test_cli_help_explains_secret_safe_validation_and_retry_options() -> None:
    result = subprocess.run(
        (sys.executable, "-m", "career_opportunity_monitor.runtime", "--help"),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    help_text = " ".join(result.stdout.split())
    assert "secret-safe configuration summary" in help_text
    assert "--report-date" in result.stdout
    assert "--cadence" in result.stdout
    assert "--dry-run" in result.stdout


def test_architecture_identifies_compose_init_as_pid_one() -> None:
    text = _text("docs/architecture.md")

    assert "Compose init process is PID 1" in text
    assert "Supercronic as PID 1" not in text


def test_public_examples_do_not_use_the_private_runtime_directory() -> None:
    public_markdown = (
        ROOT / "README.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "SECURITY.md",
        *(ROOT / "docs").glob("*.md"),
    )

    for path in public_markdown:
        text = path.read_text(encoding="utf-8")
        if path.name != "SECURITY.md":
            assert "/opt/data/" not in text


def test_public_markdown_passes_the_simple_english_scan() -> None:
    result = subprocess.run(
        (sys.executable, "scripts/check_simple_english.py"),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
