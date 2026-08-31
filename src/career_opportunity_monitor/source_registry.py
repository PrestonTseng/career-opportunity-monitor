from __future__ import annotations

from collections.abc import Callable

from .models import SourceConfiguration
from .source import JobSource
from .workday import Transport, WorkdaySource

TransportFactory = Callable[[SourceConfiguration], Transport | None]


def _workday_source(
    configuration: SourceConfiguration,
    transport_factory: TransportFactory | None,
) -> WorkdaySource:
    options = configuration.options
    return WorkdaySource(
        name=configuration.id,
        origin=configuration.origin,
        tenant=configuration.tenant,
        site=configuration.site,
        company=configuration.company,
        search_text=configuration.search_text,
        transport=(
            None if transport_factory is None else transport_factory(configuration)
        ),
        page_size=options.page_size,
        max_pages=options.max_pages,
        max_requests=options.max_requests,
        timeout_seconds=options.timeout_seconds,
        response_limit_bytes=options.response_limit_bytes,
        retries=options.retries,
        rate_limit_seconds=options.rate_limit_seconds,
    )


_ADAPTERS: dict[
    str, Callable[[SourceConfiguration, TransportFactory | None], JobSource]
] = {"workday": _workday_source}


def build_sources(
    configurations: tuple[SourceConfiguration, ...],
    *,
    transport_factory: TransportFactory | None = None,
) -> tuple[JobSource, ...]:
    """Build enabled sources from the closed adapter registry in document order."""
    return tuple(
        _ADAPTERS[configuration.adapter](configuration, transport_factory)
        for configuration in configurations
        if configuration.enabled
    )
