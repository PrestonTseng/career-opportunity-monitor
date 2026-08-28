from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

import career_opportunity_monitor.sqlite_repository as sqlite_repository_module
from career_opportunity_monitor.config import ConfigError, load_configuration
from career_opportunity_monitor.discord_delivery import DiscordDelivery
from career_opportunity_monitor.reporting import FileDelivery
from career_opportunity_monitor.runtime import build_deliveries
from career_opportunity_monitor.sqlite_repository import SQLiteRepository

ROOT = Path(__file__).parents[1]
RESUME = ROOT / "examples" / "resume_facts.yaml"
STRATEGY = ROOT / "examples" / "strategy" / "v1"


def _configuration(tmp_path: Path, destinations: object) -> Path:
    directory = tmp_path / "config"
    directory.mkdir(parents=True)
    shutil.copyfile(STRATEGY / "strategy.yaml", directory / "strategy.yaml")
    shutil.copyfile(STRATEGY / "sources.yaml", directory / "sources.yaml")
    (directory / "destinations.yaml").write_text(
        yaml.safe_dump(destinations), encoding="utf-8"
    )
    return directory


def _destination(
    identifier: str = "discord-alerts", *, enabled: bool = True
) -> dict[str, object]:
    return {
        "id": identifier,
        "enabled": enabled,
        "type": "discord",
        "report_cadences": ["daily"],
        "webhook_url_file": "/run/secrets/discord-webhook",
    }


def test_load_configuration_compiles_versioned_destinations_in_order(
    tmp_path: Path,
) -> None:
    directory = _configuration(
        tmp_path,
        {
            "schema_version": 1,
            "destinations": [_destination(), _destination("off", enabled=False)],
        },
    )

    loaded = load_configuration(RESUME, directory)

    assert [item.id for item in loaded.destinations] == ["discord-alerts", "off"]
    assert loaded.destinations[0].report_cadences == ("daily",)
    assert loaded.destinations[0].webhook_url_file == Path(
        "/run/secrets/discord-webhook"
    )
    assert len(loaded.destinations_hash) == 64
    assert b"TOKEN" not in loaded.destinations_snapshot_bytes


def test_load_configuration_rejects_duplicate_or_relative_destination(
    tmp_path: Path,
) -> None:
    duplicate = _destination()
    directory = _configuration(
        tmp_path,
        {"schema_version": 1, "destinations": [duplicate, duplicate]},
    )
    with pytest.raises(ConfigError, match="duplicate destination id"):
        load_configuration(RESUME, directory)

    relative = _destination()
    relative["webhook_url_file"] = "secret.txt"
    directory = _configuration(
        tmp_path / "other",
        {"schema_version": 1, "destinations": [relative]},
    )
    with pytest.raises(ConfigError, match="absolute"):
        load_configuration(RESUME, directory)


def test_schema_v5_migrates_and_records_secret_free_delivery_evidence(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    repository.store_report("daily:2026-08-29", b"report", "2026-08-29T00:00:00Z")

    repository.record_delivery_attempt(
        report_key="daily:2026-08-29",
        report_hash="a" * 64,
        destination_id="discord-alerts",
        chunk_index=0,
        chunk_count=2,
        chunk_hash="b" * 64,
        status="acknowledged",
        attempted_at="2026-08-29T00:00:00Z",
        http_class="2xx",
        idempotency_state="acknowledged",
    )

    assert repository.delivery_chunk_acknowledged(
        "daily:2026-08-29", "a" * 64, "discord-alerts", 0, "b" * 64
    )
    assert "delivery_attempts" in repository.table_names()
    repository.close()

    connection = sqlite3.connect(tmp_path / "history.sqlite3")
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
    stored = repr(connection.execute("SELECT * FROM delivery_attempts").fetchall())
    assert "webhook" not in stored.lower()
    assert "TOKEN" not in stored
    connection.close()


def test_delivery_evidence_persists_indeterminate_transport_state(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    repository.store_report("daily:2026-08-29", b"report", "2026-08-29T00:00:00Z")
    repository.record_delivery_attempt(
        report_key="daily:2026-08-29",
        report_hash="a" * 64,
        destination_id="discord-alerts",
        chunk_index=0,
        chunk_count=1,
        chunk_hash="b" * 64,
        status="failed",
        attempted_at="2026-08-29T00:00:00Z",
        http_class="transport",
        idempotency_state="indeterminate",
    )

    assert repository.delivery_chunk_indeterminate(
        "daily:2026-08-29", "a" * 64, "discord-alerts", 0, "b" * 64
    )
    repository.close()


def test_existing_schema_v4_delivery_evidence_migrates_without_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    connection = sqlite3.connect(path)
    for version in range(1, 5):
        migration = getattr(sqlite_repository_module, f"_MIGRATION_{version}")
        connection.executescript(migration)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, f"2026-08-29T00:00:0{version}Z"),
        )
    connection.execute("PRAGMA user_version = 4")
    connection.execute(
        "INSERT INTO reports(report_key, report_bytes, created_at) VALUES (?, ?, ?)",
        ("daily:x", b"report", "2026-08-29T00:00:00Z"),
    )
    connection.execute(
        "INSERT INTO delivery_attempts("
        "report_id, report_key, report_hash, destination_id, chunk_index, "
        "chunk_count, chunk_hash, status, attempted_at, http_class, "
        "idempotency_state) VALUES (1, ?, ?, ?, 0, 1, ?, ?, ?, ?, ?)",
        (
            "daily:x",
            "a" * 64,
            "discord-alerts",
            "b" * 64,
            "acknowledged",
            "2026-08-29T00:00:00Z",
            "2xx",
            "acknowledged",
        ),
    )
    connection.commit()
    connection.close()

    repository = SQLiteRepository(path)

    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone() == (5,)
    assert connection.execute("SELECT count(*) FROM delivery_attempts").fetchone() == (
        1,
    )
    connection.close()
    assert repository.delivery_chunk_acknowledged(
        "daily:x", "a" * 64, "discord-alerts", 0, "b" * 64
    )
    repository.close()


def test_delivery_composition_keeps_file_and_filters_disabled_and_cadence(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "discord"
    secret.write_text("https://discord.com/api/webhooks/123/TOKEN", encoding="utf-8")
    values = {
        "schema_version": 1,
        "destinations": [
            {**_destination("daily-one"), "webhook_url_file": str(secret)},
            {
                **_destination("disabled", enabled=False),
                "webhook_url_file": str(secret),
            },
            {
                **_destination("weekly-only"),
                "report_cadences": ["weekly"],
                "webhook_url_file": str(secret),
            },
            {**_destination("daily-two"), "webhook_url_file": str(secret)},
        ],
    }
    loaded = load_configuration(RESUME, _configuration(tmp_path / "loaded", values))
    repository = SQLiteRepository(tmp_path / "history.sqlite3")
    repository.store_report("daily:x", b"report", "now")

    deliveries = build_deliveries(
        loaded,
        cadence="daily",
        report_key="daily:x",
        report_path=tmp_path / "report.md",
        repository=repository,
        now=lambda: "now",
    )

    assert isinstance(deliveries[0], FileDelivery)
    assert [
        item.destination_id
        for item in deliveries[1:]
        if isinstance(item, DiscordDelivery)
    ] == [
        "daily-one",
        "daily-two",
    ]
    repository.close()
