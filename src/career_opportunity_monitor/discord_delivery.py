from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import ssl
import stat
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPException, HTTPSConnection
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit

from .reporting import DeliveryError

_DISCORD_HOSTS = frozenset(
    {"discord.com", "canary.discord.com", "ptb.discord.com", "discordapp.com"}
)
_MAX_SECRET_BYTES = 4096


@dataclass(frozen=True)
class DiscordResponse:
    status: int
    body: bytes


class DiscordTransportError(OSError):
    """A sanitized Discord transport failure."""


class DiscordRequester(Protocol):
    def __call__(
        self, url: str, payload: bytes, timeout_seconds: float
    ) -> DiscordResponse: ...


class DeliveryEvidenceRepository(Protocol):
    def delivery_chunk_acknowledged(
        self,
        report_key: str,
        report_hash: str,
        destination_id: str,
        chunk_index: int,
        chunk_hash: str,
    ) -> bool: ...

    def delivery_chunk_indeterminate(
        self,
        report_key: str,
        report_hash: str,
        destination_id: str,
        chunk_index: int,
        chunk_hash: str,
    ) -> bool: ...

    def record_delivery_attempt(self, **values: object) -> None: ...


def read_discord_webhook_url(path: Path) -> str:
    """Read and validate a Discord webhook without disclosing its value."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValueError("cannot inspect Discord webhook URL file") from exc
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise ValueError("Discord webhook URL must be a regular file")
    if before.st_size < 1 or before.st_size > _MAX_SECRET_BYTES:
        raise ValueError("Discord webhook URL file has an invalid size")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            raw = os.read(descriptor, _MAX_SECRET_BYTES + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ValueError("cannot read Discord webhook URL file") from exc

    def fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    if fingerprint(before) != fingerprint(opened) or fingerprint(before) != fingerprint(
        after
    ):
        raise ValueError("Discord webhook URL file changed while reading")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Discord webhook URL file is not UTF-8") from exc
    if not value or value != value.strip() or "\n" in value or "\r" in value:
        raise ValueError("Discord webhook URL file must contain one non-empty line")
    _validate_webhook_url(value)
    return value


def _validate_webhook_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Discord webhook URL is invalid") from exc
    hostname = parsed.hostname
    path_parts = parsed.path.split("/")
    if (
        parsed.scheme != "https"
        or hostname not in _DISCORD_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc != hostname
        or len(path_parts) != 5
        or path_parts[:3] != ["", "api", "webhooks"]
        or not path_parts[3]
        or not path_parts[4]
    ):
        raise ValueError("Discord webhook URL is not an approved HTTPS webhook")


def chunk_discord_content(content: bytes, *, limit: int = 2000) -> tuple[str, ...]:
    if limit < 1 or limit > 2000:
        raise ValueError("Discord chunk limit must be between 1 and 2000")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeliveryError("stored report is not valid UTF-8") from exc
    if not text:
        return ()
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        boundary = remaining.rfind("\n", 0, limit + 1)
        take = boundary + 1 if boundary >= 0 else limit
        chunks.append(remaining[:take])
        remaining = remaining[take:]
    return tuple(chunks)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _verified_discord_address(url: str) -> str:
    hostname = urlsplit(url).hostname
    if hostname not in _DISCORD_HOSTS:
        raise DiscordTransportError("destination host is not approved")
    try:
        answers = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DiscordTransportError("destination DNS lookup failed") from exc
    addresses = {str(answer[4][0]) for answer in answers}
    try:
        unsafe = not addresses or any(
            not ipaddress.ip_address(value).is_global for value in addresses
        )
    except ValueError as exc:
        raise DiscordTransportError("destination DNS answer is invalid") from exc
    if unsafe:
        raise DiscordTransportError("destination DNS answer is not public")
    return sorted(addresses)[0]


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


def _post_pinned_discord_request(
    url: str, payload: bytes, timeout_seconds: float, approved_address: str
) -> DiscordResponse:
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if hostname is None:
        raise DiscordTransportError("destination host is invalid")
    connection = _PinnedHTTPSConnection(hostname, approved_address, timeout_seconds)
    try:
        connection.request(
            "POST",
            parsed.path,
            body=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "career-monitor/0.1",
            },
        )
        response = connection.getresponse()
        return DiscordResponse(response.status, response.read(65537))
    except (OSError, HTTPException, TimeoutError) as exc:
        raise DiscordTransportError("Discord request failed") from exc
    finally:
        connection.close()


def post_discord_request(
    url: str,
    payload: bytes,
    timeout_seconds: float,
    *,
    approved_address: str | None = None,
) -> DiscordResponse:
    if approved_address is not None:
        return _post_pinned_discord_request(
            url, payload, timeout_seconds, approved_address
        )
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "career-monitor/0.1",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            body = response.read(65537)
            return DiscordResponse(int(response.status), body)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(65537)
        except OSError:
            body = b""
        return DiscordResponse(int(exc.code), body)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise DiscordTransportError("Discord request failed") from exc


def request_discord(
    url: str, payload: bytes, timeout_seconds: float
) -> DiscordResponse:
    approved_address = _verified_discord_address(url)
    return post_discord_request(
        url, payload, timeout_seconds, approved_address=approved_address
    )


class DiscordDelivery:
    """Deliver immutable report bytes with durable chunk-level idempotency."""

    def __init__(
        self,
        *,
        destination_id: str,
        report_key: str,
        webhook_url_file: Path,
        repository: DeliveryEvidenceRepository,
        requester: DiscordRequester = request_discord,
        now: Callable[[], str] = lambda: "",
        sleep: Callable[[float], None] = time.sleep,
        chunk_limit: int = 2000,
        timeout_seconds: float = 10.0,
        retries: int = 2,
    ) -> None:
        if not destination_id or not report_key:
            raise ValueError("delivery identity is required")
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("Discord timeout must be between 0 and 30 seconds")
        if retries < 0 or retries > 5:
            raise ValueError("Discord retries must be between 0 and 5")
        self._destination_id = destination_id
        self._report_key = report_key
        self._url = read_discord_webhook_url(webhook_url_file)
        self._repository = repository
        self._requester = requester
        self._now = now
        self._sleep = sleep
        self._chunk_limit = chunk_limit
        self._timeout_seconds = timeout_seconds
        self._retries = retries

    @property
    def destination_id(self) -> str:
        return self._destination_id

    def deliver(self, content: bytes) -> None:
        chunks = chunk_discord_content(content, limit=self._chunk_limit)
        report_hash = hashlib.sha256(content).hexdigest()
        for index, chunk in enumerate(chunks):
            chunk_bytes = chunk.encode("utf-8")
            chunk_hash = hashlib.sha256(chunk_bytes).hexdigest()
            if self._repository.delivery_chunk_acknowledged(
                self._report_key,
                report_hash,
                self._destination_id,
                index,
                chunk_hash,
            ):
                continue
            if self._repository.delivery_chunk_indeterminate(
                self._report_key,
                report_hash,
                self._destination_id,
                index,
                chunk_hash,
            ):
                raise DeliveryError(
                    "Discord delivery state is indeterminate; refusing to "
                    "duplicate chunk"
                )
            self._send_chunk(
                report_hash=report_hash,
                chunk_index=index,
                chunk_count=len(chunks),
                chunk_hash=chunk_hash,
                chunk=chunk,
            )

    def _send_chunk(
        self,
        *,
        report_hash: str,
        chunk_index: int,
        chunk_count: int,
        chunk_hash: str,
        chunk: str,
    ) -> None:
        payload = json.dumps(
            {"content": chunk}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        for attempt in range(self._retries + 1):
            status: int | None = None
            try:
                response = self._requester(self._url, payload, self._timeout_seconds)
                status = response.status
            except (DiscordTransportError, OSError, TimeoutError):
                self._record(
                    report_hash,
                    chunk_index,
                    chunk_count,
                    chunk_hash,
                    status="failed",
                    http_class="transport",
                    idempotency_state="indeterminate",
                )
                raise DeliveryError(
                    "Discord delivery state is indeterminate; refusing to retry chunk"
                ) from None

            http_class = f"{status // 100}xx" if 100 <= status <= 599 else "invalid"
            if 200 <= status < 300:
                if response.body:
                    self._record(
                        report_hash,
                        chunk_index,
                        chunk_count,
                        chunk_hash,
                        status="failed",
                        http_class=http_class,
                        idempotency_state="pending",
                    )
                    raise DeliveryError("Discord returned a malformed success response")
                self._record(
                    report_hash,
                    chunk_index,
                    chunk_count,
                    chunk_hash,
                    status="acknowledged",
                    http_class=http_class,
                    idempotency_state="acknowledged",
                )
                return
            if status == 429:
                retry_after = _retry_after(response.body)
                if retry_after is None:
                    self._record(
                        report_hash,
                        chunk_index,
                        chunk_count,
                        chunk_hash,
                        status="failed",
                        http_class=http_class,
                        idempotency_state="pending",
                    )
                    raise DeliveryError(
                        "Discord returned a malformed rate limit response"
                    )
                self._record(
                    report_hash,
                    chunk_index,
                    chunk_count,
                    chunk_hash,
                    status="rate_limited",
                    http_class=http_class,
                    idempotency_state="pending",
                )
                if attempt < self._retries:
                    self._sleep(retry_after)
                    continue
                raise DeliveryError("Discord delivery exhausted rate limit retries")
            retryable = 500 <= status < 600
            self._record(
                report_hash,
                chunk_index,
                chunk_count,
                chunk_hash,
                status="retryable_failure" if retryable else "failed",
                http_class=http_class,
                idempotency_state="pending",
            )
            if retryable and attempt < self._retries:
                continue
            raise DeliveryError(f"Discord delivery returned HTTP {status}")

    def _record(
        self,
        report_hash: str,
        chunk_index: int,
        chunk_count: int,
        chunk_hash: str,
        *,
        status: str,
        http_class: str,
        idempotency_state: str,
    ) -> None:
        self._repository.record_delivery_attempt(
            report_key=self._report_key,
            report_hash=report_hash,
            destination_id=self._destination_id,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            chunk_hash=chunk_hash,
            status=status,
            attempted_at=self._now(),
            http_class=http_class,
            idempotency_state=idempotency_state,
        )


def _retry_after(body: bytes) -> float | None:
    if len(body) > 65536:
        return None
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    typed_value = cast(dict[str, object], value)
    raw = typed_value.get("retry_after")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    result = float(raw)
    return result if 0 <= result <= 60 else None
