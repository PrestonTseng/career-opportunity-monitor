from __future__ import annotations

import time
from collections.abc import Callable

from .source import SourceError
from .workday import Transport, UrlLibTransport, WorkdaySource


class NvidiaWorkdaySource(WorkdaySource):
    """Compatibility wrapper for the former NVIDIA-specific adapter API."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        page_size: int = 20,
        max_pages: int = 10,
        max_requests: int = 420,
        timeout_seconds: float = 10.0,
        response_limit_bytes: int = 1_000_000,
        retries: int = 1,
        rate_limit_seconds: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(
            name="nvidia-workday",
            origin="https://nvidia.wd5.myworkdayjobs.com",
            tenant="nvidia",
            site="NVIDIAExternalCareerSite",
            company="NVIDIA",
            search_text="Taiwan",
            transport=transport,
            page_size=page_size,
            max_pages=max_pages,
            max_requests=max_requests,
            timeout_seconds=timeout_seconds,
            response_limit_bytes=response_limit_bytes,
            retries=retries,
            rate_limit_seconds=rate_limit_seconds,
            sleep=sleep,
        )


__all__ = [
    "NvidiaWorkdaySource",
    "SourceError",
    "Transport",
    "UrlLibTransport",
]
