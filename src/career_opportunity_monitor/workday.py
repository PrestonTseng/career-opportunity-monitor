from __future__ import annotations

import ipaddress
import json
import re
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html import unescape
from http.client import HTTPException, HTTPMessage, HTTPSConnection
from typing import IO, Protocol, cast
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request

from .models import CanonicalJob, JobLocation
from .repository import SourceObservation
from .source import SourceError, SourceFetchResult

_TAGS = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")


class Transport(Protocol):
    def request(self, url: str, body: bytes | None) -> bytes: ...


class _Response(Protocol):
    status: int

    def getheader(self, name: str) -> str | None: ...

    def read(self, amount: int) -> bytes: ...


class _Connection(Protocol):
    def request(
        self, method: str, path: str, body: bytes | None, headers: dict[str, str]
    ) -> None: ...

    def getresponse(self) -> _Response: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[str, str, float], _Connection]


class ApprovedOriginRedirectHandler(HTTPRedirectHandler):
    def __init__(self, approved_origin: str) -> None:
        self._approved_origin = approved_origin

    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        validate_approved_url(newurl, self._approved_origin)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrlLibTransport:
    def __init__(
        self,
        *,
        approved_origin: str,
        timeout_seconds: float,
        response_limit_bytes: int,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._approved_origin = approved_origin
        self._timeout_seconds = timeout_seconds
        self._response_limit_bytes = response_limit_bytes
        self._connection_factory = connection_factory or _pinned_https_connection

    def request(self, url: str, body: bytes | None) -> bytes:
        current_url = url
        current_body = body
        for redirect_count in range(6):
            addresses = validate_approved_url(current_url, self._approved_origin)
            parsed = urlsplit(current_url)
            assert parsed.hostname is not None
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            connection = self._connection_factory(
                parsed.hostname, addresses[0], self._timeout_seconds
            )
            try:
                connection.request(
                    "POST" if current_body is not None else "GET",
                    path,
                    current_body,
                    {"Accept": "application/json", "Content-Type": "application/json"},
                )
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if location is None or redirect_count == 5:
                        raise SourceError("source returned an invalid redirect")
                    try:
                        current_url = urljoin(current_url, location)
                        _assert_approved_origin(current_url, self._approved_origin)
                    except ValueError as exc:
                        raise SourceError(
                            "source returned an invalid redirect"
                        ) from exc
                    except SourceError as exc:
                        raise SourceError(
                            "source returned an invalid redirect"
                        ) from exc
                    if response.status in (301, 302, 303):
                        current_body = None
                    continue
                if response.status >= 400:
                    raise SourceError(f"request failed with HTTP {response.status}")
                payload = response.read(self._response_limit_bytes + 1)
            except HTTPException as exc:
                raise SourceError(
                    "request failed due to invalid HTTP response"
                ) from exc
            except (OSError, TimeoutError) as exc:
                raise SourceError(f"request failed: {exc}") from exc
            finally:
                connection.close()
            if len(payload) > self._response_limit_bytes:
                raise SourceError("response exceeded configured byte limit")
            return payload
        raise SourceError("source returned too many redirects")


class _PinnedHTTPSConnection(HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float) -> None:
        super().__init__(host, 443, timeout=timeout)
        self._approved_address = address
        self._ssl_context = ssl.create_default_context()

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._approved_address, 443), self.timeout
        )
        self.sock = self._ssl_context.wrap_socket(raw_socket, server_hostname=self.host)


def _pinned_https_connection(
    host: str, address: str, timeout: float
) -> _PinnedHTTPSConnection:
    return _PinnedHTTPSConnection(host, address, timeout)


def _origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.netloc != parsed.hostname
    ):
        return ""
    return f"https://{parsed.hostname}"


def _assert_approved_origin(url: str, approved_origin: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise SourceError(
            "request URL is outside the configured approved origin"
        ) from exc
    if (
        _origin(approved_origin) != approved_origin
        or parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or f"{parsed.scheme}://{parsed.netloc}" != approved_origin
    ):
        raise SourceError("request URL is outside the configured approved origin")
    assert parsed.hostname is not None
    return parsed.hostname


def validate_approved_url(url: str, approved_origin: str) -> tuple[str, ...]:
    hostname = _assert_approved_origin(url, approved_origin)
    try:
        answers = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceError("approved source host could not be resolved safely") from exc
    addresses = {str(answer[4][0]) for answer in answers}
    if not addresses or any(
        not ipaddress.ip_address(address).is_global for address in addresses
    ):
        raise SourceError("approved source host resolved to a non-public address")
    return tuple(sorted(addresses))


def _assert_official_url(url: str, approved_origin: str, site: str) -> None:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise SourceError(
            "detail official URL is outside the configured Workday origin"
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or f"{parsed.scheme}://{parsed.netloc}" != approved_origin
        or not parsed.path.startswith(f"/{site}/job/")
    ):
        raise SourceError(
            "detail official URL is outside the configured Workday origin"
        )


@dataclass(frozen=True)
class WorkdaySource:
    """Read-only adapter for one explicitly approved Workday CXS instance."""

    name: str
    origin: str
    tenant: str
    site: str
    company: str
    search_text: str

    transport: Transport | None = None
    page_size: int = 20
    max_pages: int = 10
    max_requests: int = 420
    timeout_seconds: float = 10.0
    response_limit_bytes: int = 1_000_000
    retries: int = 1
    rate_limit_seconds: float = 0.0
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if (
            self.page_size < 1
            or self.max_pages < 1
            or self.max_requests < 1
            or self.timeout_seconds <= 0
            or self.response_limit_bytes < 1
            or self.retries < 0
            or self.rate_limit_seconds < 0
        ):
            raise ValueError("Workday source limits must be positive and bounded")
        if _origin(self.origin) != self.origin:
            raise ValueError("Workday source origin must be canonical HTTPS")

    def fetch(self) -> SourceFetchResult:
        transport = self.transport or UrlLibTransport(
            approved_origin=self.origin,
            timeout_seconds=self.timeout_seconds,
            response_limit_bytes=self.response_limit_bytes,
        )
        cxs_base = f"{self.origin}/wday/cxs/{self.tenant}/{self.site}"
        list_url = f"{cxs_base}/jobs"
        observations: list[SourceObservation] = []
        failures: list[str] = []
        seen_ids: set[str] = set()
        request_count = 0
        listed = 0
        total: int | None = None

        def request(url: str, body: bytes | None) -> bytes:
            nonlocal request_count
            if request_count >= self.max_requests:
                raise SourceError("configured request limit reached")
            last_error: SourceError | None = None
            for attempt in range(self.retries + 1):
                if request_count >= self.max_requests:
                    raise SourceError("configured request limit reached")
                request_count += 1
                try:
                    return transport.request(url, body)
                except SourceError as exc:
                    last_error = exc
                    if attempt < self.retries:
                        self.sleep(self.rate_limit_seconds)
            assert last_error is not None
            raise last_error

        for page in range(self.max_pages):
            offset = page * self.page_size
            body = json.dumps(
                {
                    "limit": self.page_size,
                    "offset": offset,
                    "searchText": self.search_text,
                },
                separators=(",", ":"),
            ).encode()
            try:
                page_data = _json_object(request(list_url, body), "list response")
                raw_total = page_data.get("total")
                raw_postings_value = page_data.get("jobPostings")
                if not isinstance(raw_total, int) or raw_total < 0:
                    raise SourceError(
                        "list response total must be a non-negative integer"
                    )
                if not isinstance(raw_postings_value, list):
                    raise SourceError("list response jobPostings must be a list")
                raw_postings = cast(list[object], raw_postings_value)
            except SourceError as exc:
                failures.append(f"list page {page}: {exc}")
                break
            if total is None:
                total = raw_total
            if len(raw_postings) > self.page_size:
                failures.append(f"list page {page}: response exceeds page size")
                break
            listed += len(raw_postings)
            for item in raw_postings:
                try:
                    external_path = _external_path(item)
                    detail_payload = request(f"{cxs_base}{external_path}", None)
                    detail = _json_object(detail_payload, "detail")
                    observation = _observation(
                        detail,
                        source_name=self.name,
                        company=self.company,
                        origin=self.origin,
                        site=self.site,
                    )
                    source_id = observation.job.source_job_id
                    if source_id in seen_ids:
                        failures.append(f"duplicate requisition {source_id}")
                        continue
                    seen_ids.add(source_id)
                    observations.append(observation)
                except SourceError as exc:
                    failures.append(f"detail: {exc}")
                finally:
                    if self.rate_limit_seconds:
                        self.sleep(self.rate_limit_seconds)
            if listed >= total or len(raw_postings) < self.page_size:
                break

        if total is not None and listed < total and not failures:
            failures.append("page limit reached")

        if total is None and not observations:
            detail = failures[0] if failures else "no valid list response"
            raise SourceError(f"source collection failed: {detail}")

        receipt: dict[str, object] = {
            "source": self.name,
            "listed": listed,
            "accepted": len(observations),
            "request_count": request_count,
            "max_pages": self.max_pages,
            "max_requests": self.max_requests,
            "failures": failures,
            "partial": bool(failures),
            "total": total,
        }
        return SourceFetchResult(tuple(observations), receipt)


def _json_object(payload: bytes, label: str) -> dict[str, object]:
    try:
        value: object = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError(f"malformed {label} JSON") from exc
    if not isinstance(value, dict):
        raise SourceError(f"{label} must be an object")
    return _object_dict(cast(object, value))


def _object_dict(value: object) -> dict[str, object]:
    raw = cast(dict[object, object], value)
    return {str(key): item for key, item in raw.items()}


def _external_path(item: object) -> str:
    if not isinstance(item, dict):
        raise SourceError("list posting must be an object")
    value = _object_dict(cast(object, item)).get("externalPath")
    if not isinstance(value, str):
        raise SourceError("list posting has unsafe externalPath")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise SourceError("list posting has unsafe externalPath") from exc
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/job/")
        or "\\" in value
        or "%" in value
        or any(part in ("", ".", "..") for part in parsed.path.split("/")[2:])
    ):
        raise SourceError("list posting has unsafe externalPath")
    return value


def _observation(
    detail: dict[str, object],
    *,
    source_name: str,
    company: str,
    origin: str,
    site: str,
) -> SourceObservation:
    info_value = detail.get("jobPostingInfo")
    if not isinstance(info_value, dict):
        raise SourceError("detail has no jobPostingInfo object")
    info = _object_dict(cast(object, info_value))
    requisition = info.get("jobReqId")
    title = info.get("title")
    url = info.get("externalUrl")
    if not all(
        isinstance(value, str) and value.strip() for value in (requisition, title, url)
    ):
        raise SourceError("detail requires requisition, title, and official URL")
    _assert_official_url(str(url), origin, site)
    location = _location(info)
    description = info.get("jobDescription")
    if description is not None and not isinstance(description, str):
        raise SourceError("detail jobDescription must be text or null")
    raw_date = info.get("startDate")
    posted_at = _date_timestamp(raw_date) if raw_date is not None else None
    raw = json.dumps(
        detail, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return SourceObservation(
        job=CanonicalJob(
            schema_version=1,
            source_name=source_name,
            source_job_id=str(requisition),
            source_url=str(url),
            company=company,
            title=str(title),
            description_text=(
                None if description is None else _html_to_text(description)
            ),
            location=location,
            employment_type=_optional_text(info.get("timeType")),
            posted_at=posted_at,
            compensation=None,
        ),
        observed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        raw_payload=raw,
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SourceError("detail text field is malformed")
    return value.strip()


def _date_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise SourceError("detail startDate must be an ISO date")
    try:
        timestamp = datetime.combine(
            date.fromisoformat(value), datetime.min.time(), UTC
        )
        return timestamp.isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise SourceError("detail startDate must be an ISO date") from exc


def _location(info: dict[str, object]) -> JobLocation | None:
    country: str | None = None
    requisition_location = info.get("jobRequisitionLocation")
    country_value: dict[str, object] | None = None
    if isinstance(requisition_location, dict):
        location_value = _object_dict(cast(object, requisition_location))
        raw_country = location_value.get("country")
        if isinstance(raw_country, dict):
            country_value = _object_dict(cast(object, raw_country))
    if country_value is None and isinstance(info.get("country"), dict):
        country_value = _object_dict(info["country"])
    if country_value is not None:
        alpha2 = country_value.get("alpha2Code")
        descriptor = country_value.get("descriptor")
        if isinstance(alpha2, str) and len(alpha2) == 2 and alpha2.isupper():
            country = alpha2
        elif descriptor == "Taiwan":
            country = "TW"
    raw_location = info.get("location")
    if raw_location is None:
        return None if country is None else JobLocation(country, None, "unknown")
    if not isinstance(raw_location, str) or not raw_location.strip():
        raise SourceError("detail location must be text or null")
    parts = [part.strip() for part in raw_location.split(",")]
    region = parts[-1] if len(parts) > 1 and parts[-1] else None
    return JobLocation(country, region, "unknown")


def _html_to_text(value: str) -> str:
    text = _SPACE.sub(" ", _TAGS.sub(" ", unescape(value))).strip()
    if not text:
        raise SourceError("detail jobDescription contains no text")
    return text
