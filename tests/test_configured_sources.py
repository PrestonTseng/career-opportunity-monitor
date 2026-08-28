from __future__ import annotations

import io
import socket
from http.client import HTTPMessage
from unittest.mock import patch
from urllib.request import Request

import pytest

from career_opportunity_monitor.models import SourceConfiguration, WorkdayOptions
from career_opportunity_monitor.source import SourceError
from career_opportunity_monitor.source_registry import build_sources
from career_opportunity_monitor.workday import (
    ApprovedOriginRedirectHandler,
    UrlLibTransport,
    WorkdaySource,
    validate_approved_url,
)


def _configuration(source_id: str, *, enabled: bool = True) -> SourceConfiguration:
    return SourceConfiguration(
        id=source_id,
        enabled=enabled,
        adapter="workday",
        origin=f"https://{source_id}.wd5.myworkdayjobs.com",
        tenant=source_id,
        site="ExternalCareerSite",
        company=f"{source_id} company",
        search_text="Taiwan",
        options=WorkdayOptions(
            page_size=20,
            max_pages=10,
            max_requests=420,
            timeout_seconds=10.0,
            response_limit_bytes=1_000_000,
            retries=1,
            rate_limit_seconds=0.0,
        ),
    )


def test_closed_registry_builds_enabled_sources_in_configuration_order() -> None:
    sources = build_sources(
        (
            _configuration("first-workday"),
            _configuration("disabled-workday", enabled=False),
            _configuration("second-workday"),
        )
    )

    assert [source.name for source in sources] == ["first-workday", "second-workday"]
    assert all(isinstance(source, WorkdaySource) for source in sources)


def _public_dns_answer() -> list[tuple[object, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]


def test_redirect_handler_allows_only_the_exact_approved_origin() -> None:
    handler = ApprovedOriginRedirectHandler("https://jobs.example.com")
    request = Request("https://jobs.example.com/old")
    with patch(
        "career_opportunity_monitor.workday.socket.getaddrinfo",
        return_value=_public_dns_answer(),
    ):
        redirected = handler.redirect_request(
            request,
            io.BytesIO(),
            302,
            "Found",
            HTTPMessage(),
            "https://jobs.example.com/new",
        )
        assert redirected is not None
        assert redirected.full_url == "https://jobs.example.com/new"
        with pytest.raises(SourceError, match="outside the configured approved origin"):
            handler.redirect_request(
                request,
                io.BytesIO(),
                302,
                "Found",
                HTTPMessage(),
                "https://evil.example/new",
            )


def test_request_url_rejects_private_dns_answers_and_fragments() -> None:
    with (
        patch(
            "career_opportunity_monitor.workday.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))
            ],
        ),
        pytest.raises(SourceError, match="non-public address"),
    ):
        validate_approved_url(
            "https://jobs.example.com/path", "https://jobs.example.com"
        )
    with pytest.raises(SourceError, match="outside the configured approved origin"):
        validate_approved_url(
            "https://jobs.example.com/path#fragment", "https://jobs.example.com"
        )


class _Response:
    status = 200

    def getheader(self, name: str) -> str | None:
        return None

    def read(self, amount: int) -> bytes:
        return b"{}"


class _Connection:
    def request(
        self, method: str, path: str, body: bytes | None, headers: dict[str, str]
    ) -> None:
        return None

    def getresponse(self) -> _Response:
        return _Response()

    def close(self) -> None:
        return None


def test_transport_pins_the_public_dns_answer_used_by_the_safety_check() -> None:
    connected: list[tuple[str, str]] = []

    def connection_factory(host: str, address: str, timeout: float) -> _Connection:
        connected.append((host, address))
        return _Connection()

    transport = UrlLibTransport(
        approved_origin="https://jobs.example.com",
        timeout_seconds=10,
        response_limit_bytes=100,
        connection_factory=connection_factory,
    )
    with patch(
        "career_opportunity_monitor.workday.socket.getaddrinfo",
        return_value=_public_dns_answer(),
    ):
        assert transport.request("https://jobs.example.com/path", None) == b"{}"

    assert connected == [("jobs.example.com", "8.8.8.8")]
