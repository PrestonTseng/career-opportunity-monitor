from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).parents[1]
TEST_COMPOSE_FILES = ("-f", "compose.yaml", "-f", "compose.test.yaml")


def _compose_command(
    project: str,
    *arguments: str,
    environment: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("docker", "compose", "--project-name", project, *arguments),
        cwd=ROOT,
        env=environment,
        check=check,
        capture_output=True,
        text=True,
    )


def _test_compose_command(
    project: str,
    *arguments: str,
    environment: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _compose_command(
        project,
        *TEST_COMPOSE_FILES,
        *arguments,
        environment=environment,
        check=check,
    )


def _seed_test_volumes(
    project: str, environment: dict[str, str], image_id: str
) -> None:
    _test_compose_command(
        project,
        "--profile",
        "cli",
        "create",
        "cli",
        environment=environment,
    )
    seed_container = f"{project}-seed"
    subprocess.run(
        (
            "docker",
            "create",
            "--name",
            seed_container,
            "--user",
            "0:0",
            "--entrypoint",
            "/bin/true",
            "--volume",
            f"{project}_profile-data:/profile",
            "--volume",
            f"{project}_config-data:/config",
            "--volume",
            f"{project}_crontab-data:/etc/career-monitor",
            image_id,
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        for source, destination in (
            (ROOT / "examples" / "resume_facts.yaml", "/profile/resume_facts.yaml"),
            (
                ROOT / "examples" / "strategy" / "v1" / "strategy.yaml",
                "/config/strategy.yaml",
            ),
            (
                ROOT / "examples" / "strategy" / "v1" / "sources.yaml",
                "/config/sources.yaml",
            ),
            (
                ROOT / "examples" / "strategy" / "v1" / "destinations.yaml",
                "/config/destinations.yaml",
            ),
            (ROOT / "deploy" / "crontab", "/etc/career-monitor/crontab"),
        ):
            subprocess.run(
                ("docker", "cp", str(source), f"{seed_container}:{destination}"),
                check=True,
                capture_output=True,
                text=True,
            )
    finally:
        subprocess.run(
            ("docker", "rm", seed_container),
            check=True,
            capture_output=True,
            text=True,
        )


def _prepare_test_project(project: str, environment: dict[str, str]) -> str:
    _test_compose_command(
        project,
        "--profile",
        "cli",
        "build",
        "cli",
        environment=environment,
    )
    image_id = _test_compose_command(
        project,
        "--profile",
        "cli",
        "config",
        "--images",
        environment=environment,
    ).stdout.strip()
    _seed_test_volumes(project, environment, image_id)
    return image_id


def _read_runtime_file(project: str, environment: dict[str, str], path: str) -> str:
    return _test_compose_command(
        project,
        "--profile",
        "cli",
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "python",
        "cli",
        "-c",
        f"from pathlib import Path; print(Path({path!r}).read_text())",
        environment=environment,
    ).stdout


def _container_state(container_id: str) -> dict[str, object]:
    completed = subprocess.run(
        ("docker", "inspect", container_id, "--format", "{{json .State}}"),
        check=True,
        capture_output=True,
        text=True,
    )
    return cast(dict[str, object], json.loads(completed.stdout))


def _wait_for_state(
    container_id: str, expected: str, *, timeout_seconds: float = 45
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = _container_state(container_id)
        if state["Status"] == expected:
            return state
        time.sleep(0.2)
    raise AssertionError(
        f"container {container_id} did not reach {expected}: "
        f"{_container_state(container_id)}"
    )


@pytest.fixture
def compose_project(tmp_path: Path) -> Iterator[tuple[str, dict[str, str]]]:
    project = f"career-monitor-test-{uuid.uuid4().hex[:12]}"
    data_directory = tmp_path / "data"
    data_directory.mkdir()
    environment = dict(os.environ)
    environment["CAREER_MONITOR_DATA_PATH"] = str(data_directory)
    try:
        yield project, environment
    finally:
        logs = _test_compose_command(
            project,
            "--profile",
            "*",
            "logs",
            "--no-color",
            environment=environment,
            check=False,
        )
        if logs.stdout or logs.stderr:
            print(logs.stdout + logs.stderr)
        cleanup = _test_compose_command(
            project,
            "--profile",
            "*",
            "down",
            "-v",
            "--remove-orphans",
            environment=environment,
        )
        assert cleanup.returncode == 0, cleanup.stderr
        remaining = subprocess.run(
            (
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert remaining == "", f"project containers remain after cleanup: {remaining}"


def _compose_config(environment: dict[str, str]) -> dict[str, object]:
    completed = subprocess.run(
        (
            "docker",
            "compose",
            "--profile",
            "*",
            "config",
            "--format",
            "json",
        ),
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return cast(dict[str, object], json.loads(completed.stdout))


def test_compose_config_preserves_the_runtime_boundary() -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "CAREER_MONITOR_DISPLAY_LIMIT": "17",
            "CAREER_MONITOR_LLM_BASE_URL": "https://llm.invalid/v1",
            "CAREER_MONITOR_LLM_MODE": "remote",
            "CAREER_MONITOR_LLM_MODEL": "controlled-model",
            "CAREER_MONITOR_LLM_PROMPT_VERSION": "controlled-v2",
            "CAREER_MONITOR_LLM_RETRIES": "3",
            "CAREER_MONITOR_LLM_TIMEOUT_SECONDS": "12.5",
            "CAREER_MONITOR_LOCK_TIMEOUT_SECONDS": "0.75",
        }
    )
    config = _compose_config(environment)
    services = cast(dict[str, dict[str, object]], config["services"])

    assert set(services) == {"cli", "scheduler"}
    assert services["scheduler"]["restart"] == "unless-stopped"
    assert "ports" not in services["scheduler"]
    assert "ports" not in services["cli"]

    for service in services.values():
        environment = cast(dict[str, str], service["environment"])
        assert environment["CAREER_MONITOR_DISPLAY_LIMIT"] == "17"
        assert environment["CAREER_MONITOR_LOCK_TIMEOUT_SECONDS"] == "0.75"
        assert environment["CAREER_MONITOR_LLM_BASE_URL"] == "https://llm.invalid/v1"
        assert environment["CAREER_MONITOR_LLM_MODE"] == "remote"
        assert environment["CAREER_MONITOR_LLM_MODEL"] == "controlled-model"
        assert environment["CAREER_MONITOR_LLM_PROMPT_VERSION"] == "controlled-v2"
        assert environment["CAREER_MONITOR_LLM_RETRIES"] == "3"
        assert environment["CAREER_MONITOR_LLM_TIMEOUT_SECONDS"] == "12.5"
        assert environment["CAREER_MONITOR_LLM_API_KEY_FILE"] == (
            "/run/secrets/llm-api-key"
        )
        secrets = cast(list[dict[str, object]], service["secrets"])
        assert any(secret["target"] == "llm-api-key" for secret in secrets)
        mounts = {
            cast(str, mount["target"]): mount
            for mount in cast(list[dict[str, object]], service["volumes"])
        }
        assert mounts["/profile/resume_facts.yaml"]["read_only"] is True
        assert mounts["/config"]["read_only"] is True
        assert mounts["/data"].get("read_only", False) is False


def test_fresh_image_runs_all_cli_roles_as_non_root(
    compose_project: tuple[str, dict[str, str]],
) -> None:
    project, environment = compose_project
    _test_compose_command(
        project,
        "--profile",
        "cli",
        "build",
        "--no-cache",
        "cli",
        environment=environment,
    )
    image_id = _test_compose_command(
        project,
        "--profile",
        "cli",
        "config",
        "--images",
        environment=environment,
    ).stdout.strip()
    _seed_test_volumes(project, environment, image_id)
    image_user = subprocess.run(
        ("docker", "image", "inspect", image_id, "--format", "{{.Config.User}}"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert image_user not in {"", "0", "root"}
    for command in (("validate",), ("daily", "--dry-run")):
        completed = _test_compose_command(
            project,
            "--profile",
            "cli",
            "run",
            "--rm",
            "cli",
            *command,
            environment=environment,
        )
        assert json.loads(completed.stdout)["status"] in {"ok", "completed"}

    weekly = _test_compose_command(
        project,
        "--profile",
        "cli",
        "run",
        "--rm",
        "cli",
        "weekly",
        "--dry-run",
        environment=environment,
        check=False,
    )
    assert weekly.returncode == 2
    assert "weekly maintenance is not implemented" in weekly.stderr


def test_daily_commands_share_one_durable_lock(
    compose_project: tuple[str, dict[str, str]],
) -> None:
    project, environment = compose_project
    _prepare_test_project(project, environment)
    holder_script = """\
import time
from pathlib import Path
from career_opportunity_monitor.sqlite_repository import SQLiteRepository

repository = SQLiteRepository(Path('/data/history.sqlite3'))
with repository.writer_lock('daily'):
    print('lock-ready', flush=True)
    time.sleep(5)
repository.close()
"""
    holder = subprocess.Popen(
        (
            "docker",
            "compose",
            "--project-name",
            project,
            *TEST_COMPOSE_FILES,
            "--profile",
            "cli",
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python",
            "cli",
            "-c",
            holder_script,
        ),
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "lock-ready"

    try:
        collision = _test_compose_command(
            project,
            "--profile",
            "cli",
            "run",
            "--rm",
            "--no-deps",
            "-e",
            "CAREER_MONITOR_RUN_ID=compose-lock",
            "-e",
            "CAREER_MONITOR_LOCK_TIMEOUT_SECONDS=0.01",
            "cli",
            "daily",
            "--dry-run",
            environment=environment,
            check=False,
        )
    finally:
        holder.communicate(timeout=10)

    assert collision.returncode == 1
    assert "writer lock was not available" in collision.stderr
    error = json.loads(
        _read_runtime_file(project, environment, "/data/errors/daily-compose-lock.json")
    )
    assert error["command"] == "daily"
    assert error["status"] == "failed"
    assert "writer lock was not available" in error["error"]


def test_invalid_profile_mount_exits_nonzero_and_keeps_error_evidence(
    compose_project: tuple[str, dict[str, str]],
) -> None:
    project, environment = compose_project
    image_id = _prepare_test_project(project, environment)
    subprocess.run(
        (
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "--volume",
            f"{project}_profile-data:/profile",
            "--entrypoint",
            "rm",
            image_id,
            "/profile/resume_facts.yaml",
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    invalid = _test_compose_command(
        project,
        "--profile",
        "cli",
        "run",
        "--rm",
        "--no-deps",
        "-e",
        "CAREER_MONITOR_RUN_ID=invalid-profile",
        "cli",
        "daily",
        "--dry-run",
        environment=environment,
        check=False,
    )

    assert invalid.returncode == 2
    assert "runtime validation failed" in invalid.stderr
    error = json.loads(
        _read_runtime_file(
            project, environment, "/data/errors/daily-invalid-profile.json"
        )
    )
    assert error["command"] == "daily"
    assert error["status"] == "failed"
    assert "cannot inspect configuration file" in error["error"]


def test_scheduler_stops_gracefully(
    compose_project: tuple[str, dict[str, str]],
) -> None:
    project, environment = compose_project
    _prepare_test_project(project, environment)
    _test_compose_command(
        project,
        "--profile",
        "scheduler",
        "up",
        "--detach",
        "--no-build",
        "scheduler",
        environment=environment,
    )
    container_id = _test_compose_command(
        project,
        "--profile",
        "scheduler",
        "ps",
        "--quiet",
        "scheduler",
        environment=environment,
    ).stdout.strip()
    assert container_id
    _wait_for_state(container_id, "running")

    _test_compose_command(
        project,
        "--profile",
        "scheduler",
        "stop",
        "--timeout",
        "5",
        "scheduler",
        environment=environment,
    )

    state = _wait_for_state(container_id, "exited")
    assert state["ExitCode"] == 0


def test_scheduler_restarts_after_an_unexpected_exit(
    compose_project: tuple[str, dict[str, str]],
    tmp_path: Path,
) -> None:
    project, environment = compose_project
    _prepare_test_project(project, environment)
    restart_override = tmp_path / "restart.override.yaml"
    restart_override.write_text(
        """\
services:
  scheduler:
    entrypoint: !override
      - /bin/sh
      - -c
    command: !override
      - sleep 11; exit 42
""",
        encoding="utf-8",
    )
    restart_compose_files = (*TEST_COMPOSE_FILES, "-f", str(restart_override))
    _compose_command(
        project,
        *restart_compose_files,
        "--profile",
        "scheduler",
        "up",
        "--detach",
        "--no-build",
        "scheduler",
        environment=environment,
    )
    container_id = _compose_command(
        project,
        *restart_compose_files,
        "--profile",
        "scheduler",
        "ps",
        "--quiet",
        "scheduler",
        environment=environment,
    ).stdout.strip()
    assert container_id
    _wait_for_state(container_id, "running")

    deadline = time.monotonic() + 30
    inspection = "not inspected"
    while time.monotonic() < deadline:
        inspection = subprocess.run(
            (
                "docker",
                "inspect",
                container_id,
                "--format",
                "{{.RestartCount}} {{.State.Status}}",
            ),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        restart_count, status = inspection.split()
        if int(restart_count) >= 1 and status == "running":
            break
        time.sleep(0.2)
    else:
        raise AssertionError(f"scheduler did not restart: {inspection}")
