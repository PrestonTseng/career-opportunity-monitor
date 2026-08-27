import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_public_documentation_covers_the_operator_contract() -> None:
    required = {
        "README.md": (
            "docker compose --profile cli build cli",
            "docker compose --profile cli run --rm cli validate",
            "docker compose --profile cli run --rm cli daily --dry-run",
            "docs/operations.md",
        ),
        "docs/architecture.md": ("NVIDIA Workday", "SQLite", "LLM"),
        "docs/configuration.md": ("strategy.yaml", "resume_facts.yaml", "unknown"),
        "docs/llm-api.md": ("CAREER_MONITOR_LLM_MODE", "adjustment", "unavailable"),
        "docs/operations.md": (
            "Backup",
            "retry-delivery",
            "Update",
            "Rollback",
            "Source failures",
        ),
        "CONTRIBUTING.md": ("uv run pytest", "uv run ruff", "uv run pyright"),
        "SECURITY.md": ("read-only", "private", ".runtime"),
    }

    for relative_path, required_text in required.items():
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        for value in required_text:
            assert value in text, f"{relative_path} does not contain {value!r}"


def test_public_documentation_does_not_advertise_unimplemented_weekly_work() -> None:
    for relative_path in ("README.md", "docs/operations.md", "deploy/crontab"):
        text = (ROOT / relative_path).read_text(encoding="utf-8").lower()
        assert "weekly" not in text, f"{relative_path} advertises weekly work"


def test_architecture_identifies_compose_init_as_pid_one() -> None:
    text = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")

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
