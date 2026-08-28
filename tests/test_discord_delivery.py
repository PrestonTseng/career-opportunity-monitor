from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

import career_opportunity_monitor.discord_delivery as discord_delivery_module
from career_opportunity_monitor.discord_delivery import (
    DiscordDelivery,
    DiscordResponse,
    DiscordTransportError,
    chunk_discord_content,
    post_discord_request,
    read_discord_webhook_url,
    request_discord,
)
from career_opportunity_monitor.reporting import DeliveryError


@dataclass
class EvidenceRepository:
    attempts: list[dict[str, object]] = field(default_factory=list[dict[str, object]])

    def delivery_chunk_acknowledged(
        self,
        report_key: str,
        report_hash: str,
        destination_id: str,
        chunk_index: int,
        chunk_hash: str,
    ) -> bool:
        return any(
            row["report_key"] == report_key
            and row["report_hash"] == report_hash
            and row["destination_id"] == destination_id
            and row["chunk_index"] == chunk_index
            and row["chunk_hash"] == chunk_hash
            and row["status"] == "acknowledged"
            for row in self.attempts
        )

    def record_delivery_attempt(self, **values: object) -> None:
        self.attempts.append(values)

    def delivery_chunk_indeterminate(
        self,
        report_key: str,
        report_hash: str,
        destination_id: str,
        chunk_index: int,
        chunk_hash: str,
    ) -> bool:
        return any(
            row["report_key"] == report_key
            and row["report_hash"] == report_hash
            and row["destination_id"] == destination_id
            and row["chunk_index"] == chunk_index
            and row["chunk_hash"] == chunk_hash
            and row["idempotency_state"] == "indeterminate"
            for row in self.attempts
        )


@dataclass
class ScriptedRequester:
    outcomes: list[DiscordResponse | Exception]
    payloads: list[bytes] = field(default_factory=list[bytes])

    def __call__(
        self, url: str, payload: bytes, timeout_seconds: float
    ) -> DiscordResponse:
        assert "TOKEN" not in repr(payload)
        assert timeout_seconds > 0
        self.payloads.append(payload)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _secret(
    tmp_path: Path, value: str = "https://discord.com/api/webhooks/123/TOKEN"
) -> Path:
    path = tmp_path / "webhook"
    path.write_text(value, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "value",
    [
        "",
        "https://discord.com/api/webhooks/123/TOKEN\nextra",
        "http://discord.com/api/webhooks/123/TOKEN",
        "https://user@discord.com/api/webhooks/123/TOKEN",
        "https://discord.com/api/webhooks/123/TOKEN#fragment",
        "https://discord.com.evil.invalid/api/webhooks/123/TOKEN",
        "https://localhost/api/webhooks/123/TOKEN",
        "https://127.0.0.1/api/webhooks/123/TOKEN",
        "https://discord.com/not-webhooks/TOKEN",
    ],
)
def test_webhook_secret_rejects_unsafe_values_without_disclosure(
    tmp_path: Path, value: str
) -> None:
    path = _secret(tmp_path, value)

    with pytest.raises(ValueError) as caught:
        read_discord_webhook_url(path)

    assert "TOKEN" not in str(caught.value)
    if value:
        assert value not in str(caught.value)


def test_webhook_secret_rejects_symlink_and_oversized_file(tmp_path: Path) -> None:
    target = _secret(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="regular file"):
        read_discord_webhook_url(link)

    target.write_bytes(b"x" * 4097)
    with pytest.raises(ValueError, match="size"):
        read_discord_webhook_url(target)


def test_chunking_preserves_text_and_boundaries_for_utf8_and_long_lines() -> None:
    text = "alpha\n" + ("界" * 2001) + "\nomega\n"

    chunks = chunk_discord_content(text.encode(), limit=2000)

    assert "".join(chunks) == text
    assert all(0 < len(chunk) <= 2000 for chunk in chunks)
    assert chunks == chunk_discord_content(text.encode(), limit=2000)


def test_delivery_records_acknowledged_chunks_without_secret_data(
    tmp_path: Path,
) -> None:
    repository = EvidenceRepository()
    requester = ScriptedRequester(
        [
            DiscordResponse(204, b""),
            DiscordResponse(204, b""),
            DiscordResponse(204, b""),
        ]
    )
    delivery = DiscordDelivery(
        destination_id="alerts",
        report_key="daily:2026-08-29",
        webhook_url_file=_secret(tmp_path),
        repository=repository,
        requester=requester,
        now=lambda: "2026-08-29T00:00:00Z",
        chunk_limit=5,
        retries=0,
    )

    delivery.deliver(b"hello world")

    assert len(requester.payloads) == 3
    assert [json.loads(value)["content"] for value in requester.payloads] == [
        "hello",
        " worl",
        "d",
    ]
    assert all(row["status"] == "acknowledged" for row in repository.attempts)
    evidence = repr(repository.attempts)
    assert "TOKEN" not in evidence
    assert all(row["chunk_count"] == 3 for row in repository.attempts)


@pytest.mark.parametrize("status", [400, 401, 404])
def test_non_retryable_4xx_is_explicit_and_sanitized(
    tmp_path: Path, status: int
) -> None:
    repository = EvidenceRepository()
    requester = ScriptedRequester([DiscordResponse(status, b'{"message":"TOKEN"}')])
    delivery = DiscordDelivery(
        destination_id="alerts",
        report_key="daily:x",
        webhook_url_file=_secret(tmp_path),
        repository=repository,
        requester=requester,
        retries=2,
    )

    with pytest.raises(DeliveryError) as caught:
        delivery.deliver(b"report")

    assert str(status) in str(caught.value)
    assert "TOKEN" not in str(caught.value)
    assert len(requester.payloads) == 1
    assert repository.attempts[-1]["http_class"] == "4xx"


def test_5xx_and_429_retry_then_acknowledge(tmp_path: Path) -> None:
    repository = EvidenceRepository()
    sleeps: list[float] = []
    requester = ScriptedRequester(
        [
            DiscordResponse(500, b""),
            DiscordResponse(429, b'{"retry_after":0.25}'),
            DiscordResponse(204, b""),
        ]
    )
    delivery = DiscordDelivery(
        destination_id="alerts",
        report_key="daily:x",
        webhook_url_file=_secret(tmp_path),
        repository=repository,
        requester=requester,
        retries=2,
        sleep=sleeps.append,
    )

    delivery.deliver(b"report")

    assert [row["status"] for row in repository.attempts] == [
        "retryable_failure",
        "rate_limited",
        "acknowledged",
    ]
    assert sleeps == [0.25]
    assert "TOKEN" not in repr(repository.attempts)


def test_timeout_is_indeterminate_and_never_silently_retried(tmp_path: Path) -> None:
    repository = EvidenceRepository()
    requester = ScriptedRequester(
        [DiscordTransportError("timeout TOKEN"), DiscordResponse(204, b"")]
    )
    delivery = DiscordDelivery(
        destination_id="alerts",
        report_key="daily:x",
        webhook_url_file=_secret(tmp_path),
        repository=repository,
        requester=requester,
        retries=3,
    )

    with pytest.raises(DeliveryError, match="indeterminate"):
        delivery.deliver(b"report")
    with pytest.raises(DeliveryError, match="indeterminate"):
        delivery.deliver(b"report")

    assert len(requester.payloads) == 1
    assert repository.attempts[-1]["idempotency_state"] == "indeterminate"
    assert "TOKEN" not in repr(repository.attempts)


@pytest.mark.parametrize("body", [b"", b"not-json", b'{"retry_after":"soon"}'])
def test_malformed_429_fails_closed(tmp_path: Path, body: bytes) -> None:
    repository = EvidenceRepository()
    delivery = DiscordDelivery(
        destination_id="alerts",
        report_key="daily:x",
        webhook_url_file=_secret(tmp_path),
        repository=repository,
        requester=ScriptedRequester([DiscordResponse(429, body)]),
        retries=2,
    )

    with pytest.raises(DeliveryError, match="malformed rate limit response"):
        delivery.deliver(b"report")

    assert repository.attempts[-1]["status"] == "failed"


def test_partial_delivery_resumes_and_repeated_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    repository = EvidenceRepository()
    first = DiscordDelivery(
        destination_id="alerts",
        report_key="daily:x",
        webhook_url_file=_secret(tmp_path),
        repository=repository,
        requester=ScriptedRequester(
            [DiscordResponse(204, b""), DiscordResponse(500, b"")]
        ),
        chunk_limit=5,
        retries=0,
    )
    with pytest.raises(DeliveryError):
        first.deliver(b"hello world")

    second_requester = ScriptedRequester(
        [DiscordResponse(204, b""), DiscordResponse(204, b"")]
    )
    second = DiscordDelivery(
        destination_id="alerts",
        report_key="daily:x",
        webhook_url_file=_secret(tmp_path),
        repository=repository,
        requester=second_requester,
        chunk_limit=5,
        retries=0,
    )
    second.deliver(b"hello world")
    second.deliver(b"hello world")

    assert [json.loads(value)["content"] for value in second_requester.payloads] == [
        " worl",
        "d",
    ]
    assert sum(row["status"] == "acknowledged" for row in repository.attempts) == 3


def test_invalid_utf8_is_rejected_before_network(tmp_path: Path) -> None:
    requester = ScriptedRequester([])
    delivery = DiscordDelivery(
        destination_id="alerts",
        report_key="daily:x",
        webhook_url_file=_secret(tmp_path),
        repository=EvidenceRepository(),
        requester=requester,
    )

    with pytest.raises(DeliveryError, match="UTF-8"):
        delivery.deliver(b"\xff")
    assert requester.payloads == []


def test_local_http_fixture_observes_post_and_redirect_is_not_followed() -> None:
    requests: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            requests.append(self.rfile.read(length))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/unexpected")
            else:
                self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        success = post_discord_request(origin + "/ok", b"{}", 1)
        redirect = post_discord_request(origin + "/redirect", b"{}", 1)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert success == DiscordResponse(204, b"")
    assert redirect.status == 302
    assert requests == [b"{}", b"{}"]


def test_request_pins_a_verified_dns_address() -> None:
    captured: list[str | None] = []

    def fake_getaddrinfo(*args: object, **kwargs: object) -> list[object]:
        return [
            (2, 1, 6, "", ("8.8.8.8", 443)),
            (2, 1, 6, "", ("1.1.1.1", 443)),
        ]

    def fake_post(
        url: str,
        payload: bytes,
        timeout_seconds: float,
        *,
        approved_address: str | None = None,
    ) -> DiscordResponse:
        captured.append(approved_address)
        return DiscordResponse(204, b"")

    with (
        patch.object(discord_delivery_module.socket, "getaddrinfo", fake_getaddrinfo),
        patch.object(discord_delivery_module, "post_discord_request", fake_post),
    ):
        response = request_discord(
            "https://discord.com/api/webhooks/123/TOKEN", b"{}", 1
        )

    assert response == DiscordResponse(204, b"")
    assert captured == ["1.1.1.1"]
