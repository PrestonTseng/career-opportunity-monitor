from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _compose_environment(tmp_path: Path, project_name: str) -> dict[str, str]:
    data_directory = tmp_path / "data"
    data_directory.mkdir()
    return {
        **os.environ,
        "CAREER_MONITOR_CONFIG_DIR": str(ROOT / "examples" / "strategy" / "v1"),
        "CAREER_MONITOR_DATA_DIR": str(data_directory),
        "CAREER_MONITOR_MODE": "demo",
        "CAREER_MONITOR_RESUME_FILE": str(ROOT / "examples" / "resume_facts.yaml"),
        "COMPOSE_PROJECT_NAME": project_name,
    }


def test_compose_config_accepts_fictional_mounts_and_publishes_no_ports(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        ("docker", "compose", "--profile", "cli", "config"),
        cwd=ROOT,
        env=_compose_environment(tmp_path, "career-monitor-config-test"),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "published:" not in result.stdout
    assert "/profile/resume_facts.yaml" in result.stdout
    assert "/config" in result.stdout
    assert "/data" in result.stdout
