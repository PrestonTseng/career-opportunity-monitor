from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import stat
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .models import (
    CATEGORIES,
    Category,
    CompiledProfile,
    FactKind,
    FactStatus,
    LoadedConfiguration,
    Market,
    ResumeFact,
    SourceAdapter,
    SourceConfiguration,
    Strategy,
    WorkdayOptions,
)


class ConfigError(ValueError):
    """Raised when configuration cannot form one valid, stable snapshot."""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = cast(
            object,
            loader.construct_object(  # pyright: ignore[reportUnknownMemberType]
                key_node, deep=deep
            ),
        )
        if key in result:
            raise ConfigError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(  # pyright: ignore[reportUnknownMemberType]
            value_node, deep=deep
        )
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


@dataclass(frozen=True)
class _Fingerprint:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int


def _fingerprint(value: os.stat_result) -> _Fingerprint:
    return _Fingerprint(
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_stable_regular(path: Path) -> tuple[bytes, _Fingerprint]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ConfigError(f"cannot inspect configuration file {path}: {exc}") from exc
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise ConfigError(f"unsafe configuration file shape: {path}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ConfigError(f"cannot open configuration file {path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        after_read = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    expected = _fingerprint(before)
    if _fingerprint(opened) != expected or _fingerprint(after_read) != expected:
        raise ConfigError(f"configuration changed while reading: {path}")
    return b"".join(chunks), expected


def _load_yaml(raw: bytes, label: str) -> dict[str, object]:
    try:
        text = raw.decode("utf-8")
        value = yaml.load(text, Loader=_UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError, ConfigError) as exc:
        raise ConfigError(f"invalid YAML in {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must contain one YAML mapping")
    return cast(dict[str, object], value)


def load_contract_schema(name: str) -> dict[str, object]:
    source_path = Path(__file__).resolve().parents[2] / "schemas" / "v1" / name
    if source_path.is_file():
        return _load_yaml(source_path.read_bytes(), str(source_path))
    packaged_path = files("career_opportunity_monitor").joinpath("schemas", "v1", name)
    try:
        raw = packaged_path.read_bytes()
    except (FileNotFoundError, OSError) as exc:
        raise ConfigError(f"packaged schema is missing: {name}") from exc
    return _load_yaml(raw, f"packaged schema {name}")


def _validate(instance: object, schema_name: str, label: str) -> None:
    try:
        validator = Draft202012Validator(load_contract_schema(schema_name))
        validator.validate(instance)  # pyright: ignore[reportUnknownMemberType]
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "root"
        raise ConfigError(f"invalid {label} at {location}: {exc.message}") from exc


def _snapshot_bytes(files: tuple[tuple[str, bytes], ...]) -> bytes:
    parts: list[bytes] = []
    for name, content in files:
        encoded_name = name.encode("utf-8")
        parts.extend(
            (
                len(encoded_name).to_bytes(8, "big"),
                encoded_name,
                len(content).to_bytes(8, "big"),
                content,
            )
        )
    return b"".join(parts)


def _compile_profile(value: object) -> CompiledProfile:
    root = cast(dict[str, object], value)
    fact_values = cast(list[object], root["facts"])
    facts = tuple(
        ResumeFact(
            id=cast(str, fact["id"]),
            kind=cast(FactKind, fact["kind"]),
            status=cast(FactStatus, fact["status"]),
            statement=cast(str, fact["statement"]),
            keywords=tuple(cast(list[str], fact["keywords"])),
        )
        for raw_fact in fact_values
        for fact in (cast(dict[str, object], raw_fact),)
    )
    ids = [fact.id for fact in facts]
    if len(ids) != len(set(ids)):
        raise ConfigError("duplicate resume fact id")

    person = cast(dict[str, object], root["person"])
    location = cast(dict[str, object], person["location"])
    return CompiledProfile(
        profile_id=cast(str, root["profile_id"]),
        display_name=cast(str, person["display_name"]),
        country_code=cast(str, location["country_code"]),
        region=cast(str | None, location.get("region")),
        facts=facts,
    )


def _company_names(value: object, key: str) -> tuple[str, ...]:
    companies = cast(dict[str, object], value)
    return tuple(cast(list[str], companies[key]))


def _compile_strategy(value: object) -> Strategy:
    root = cast(dict[str, object], value)
    role_targets = tuple(cast(list[str], root["role_targets"]))
    if any(
        not any(
            character.isalnum() for character in unicodedata.normalize("NFKC", target)
        )
        for target in role_targets
    ):
        raise ConfigError("role target must contain alphanumeric tokens")
    ranking = cast(dict[str, object], root["ranking"])
    raw_weights = cast(dict[str, int], ranking["weights"])
    raw_caps = cast(dict[str, int], ranking["caps"])
    raw_llm_adjustment = cast(dict[str, object], ranking["llm_adjustment"])
    weights: tuple[tuple[Category, Decimal], ...] = tuple(
        (category, Decimal(raw_weights[category])) for category in CATEGORIES
    )
    caps: tuple[tuple[Category, Decimal], ...] = tuple(
        (category, Decimal(raw_caps[category])) for category in CATEGORIES
    )
    if sum((weight for _, weight in weights), Decimal()) != Decimal(100):
        raise ConfigError("weights must total exactly 100")
    for category in CATEGORIES:
        if Decimal(raw_caps[category]) > Decimal(raw_weights[category]):
            raise ConfigError(f"cap for {category} cannot exceed its weight")

    raw_markets = cast(list[object], root["markets"])
    markets = tuple(
        Market(
            country_code=cast(str, market["country_code"]),
            regions=tuple(cast(list[str], market["regions"])),
            remote=cast(bool, market["remote"]),
        )
        for raw_market in raw_markets
        for market in (cast(dict[str, object], raw_market),)
    )
    companies = root["companies"]
    referrals = _company_names(companies, "referral_available")
    if any(
        "@" in name or "://" in name or len(re.sub(r"\D", "", name)) >= 7
        for name in referrals
    ):
        raise ConfigError(
            "referral_available accepts company names, not contact details"
        )

    return Strategy(
        strategy_id=cast(str, root["strategy_id"]),
        role_targets=role_targets,
        markets=markets,
        preferred_companies=_company_names(companies, "preferred"),
        excluded_companies=_company_names(companies, "excluded"),
        referral_available=referrals,
        weights=weights,
        caps=caps,
        llm_adjustment_enabled=cast(bool, raw_llm_adjustment["enabled"]),
        llm_adjustment_minimum=cast(int, raw_llm_adjustment["minimum"]),
        llm_adjustment_maximum=cast(int, raw_llm_adjustment["maximum"]),
    )


def _approved_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("source origin is ambiguous") from exc
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.netloc != hostname
        or hostname != hostname.lower()
        or hostname.endswith(".")
        or "%" in hostname
    ):
        raise ConfigError("source origin must be an unambiguous HTTPS origin")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ConfigError(
            "source origin must not target localhost or a private address"
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if len(labels) < 2 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        ):
            raise ConfigError("source origin has an invalid host") from None
    else:
        if not address.is_global:
            raise ConfigError(
                "source origin must not target localhost or a private address"
            )
    return f"https://{hostname}"


def _compile_sources(value: object) -> tuple[SourceConfiguration, ...]:
    root = cast(dict[str, object], value)
    raw_sources = cast(list[object], root["sources"])
    sources: list[SourceConfiguration] = []
    for raw_source in raw_sources:
        source = cast(dict[str, object], raw_source)
        raw_options = cast(dict[str, object], source["options"])
        sources.append(
            SourceConfiguration(
                id=cast(str, source["id"]),
                enabled=cast(bool, source["enabled"]),
                adapter=cast(SourceAdapter, source["adapter"]),
                origin=_approved_origin(cast(str, source["origin"])),
                tenant=cast(str, source["tenant"]),
                site=cast(str, source["site"]),
                company=cast(str, source["company"]),
                search_text=cast(str, source["search_text"]),
                options=WorkdayOptions(
                    page_size=cast(int, raw_options["page_size"]),
                    max_pages=cast(int, raw_options["max_pages"]),
                    max_requests=cast(int, raw_options["max_requests"]),
                    timeout_seconds=float(
                        cast(int | float, raw_options["timeout_seconds"])
                    ),
                    response_limit_bytes=cast(int, raw_options["response_limit_bytes"]),
                    retries=cast(int, raw_options["retries"]),
                    rate_limit_seconds=float(
                        cast(int | float, raw_options["rate_limit_seconds"])
                    ),
                ),
            )
        )
    ids = [source.id for source in sources]
    if len(ids) != len(set(ids)):
        raise ConfigError("duplicate source id")
    return tuple(sources)


def _assert_unchanged(path: Path, expected: _Fingerprint) -> None:
    try:
        current = _fingerprint(path.lstat())
    except OSError as exc:
        raise ConfigError(f"configuration changed after reading: {path}") from exc
    if current != expected:
        raise ConfigError(f"configuration changed after reading: {path}")


def load_configuration(
    resume_path: Path,
    strategy_directory: Path,
    *,
    after_read: Callable[[], None] | None = None,
) -> LoadedConfiguration:
    """Read, validate, and compile one immutable configuration snapshot."""
    try:
        directory_before = strategy_directory.lstat()
    except OSError as exc:
        raise ConfigError(f"cannot inspect strategy directory: {exc}") from exc
    if not stat.S_ISDIR(directory_before.st_mode) or strategy_directory.is_symlink():
        raise ConfigError("unsafe strategy directory shape")

    entries_before = tuple(sorted(path.name for path in strategy_directory.iterdir()))
    if entries_before != ("sources.yaml", "strategy.yaml"):
        raise ConfigError(
            "configuration directory must contain exactly sources.yaml and "
            "strategy.yaml"
        )

    resume_raw, resume_fingerprint = _read_stable_regular(resume_path)
    strategy_path = strategy_directory / "strategy.yaml"
    strategy_raw, strategy_fingerprint = _read_stable_regular(strategy_path)
    sources_path = strategy_directory / "sources.yaml"
    sources_raw, sources_fingerprint = _read_stable_regular(sources_path)
    if after_read is not None:
        after_read()

    _assert_unchanged(resume_path, resume_fingerprint)
    _assert_unchanged(strategy_path, strategy_fingerprint)
    _assert_unchanged(sources_path, sources_fingerprint)
    if _fingerprint(strategy_directory.lstat()) != _fingerprint(directory_before):
        raise ConfigError("strategy directory changed while reading")
    entries_after = tuple(sorted(path.name for path in strategy_directory.iterdir()))
    if entries_after != entries_before:
        raise ConfigError("strategy directory changed while reading")

    resume_value = _load_yaml(resume_raw, str(resume_path))
    strategy_value = _load_yaml(strategy_raw, str(strategy_path))
    sources_value = _load_yaml(sources_raw, str(sources_path))
    _validate(resume_value, "resume-facts.schema.yaml", "resume profile")
    _validate(strategy_value, "strategy.schema.yaml", "strategy")
    _validate(sources_value, "sources.schema.yaml", "sources")

    profile_snapshot = _snapshot_bytes(((resume_path.name, resume_raw),))
    strategy_snapshot = _snapshot_bytes((("strategy.yaml", strategy_raw),))
    sources_snapshot = _snapshot_bytes((("sources.yaml", sources_raw),))
    return LoadedConfiguration(
        profile=_compile_profile(resume_value),
        strategy=_compile_strategy(strategy_value),
        sources=_compile_sources(sources_value),
        profile_hash=hashlib.sha256(profile_snapshot).hexdigest(),
        strategy_hash=hashlib.sha256(strategy_snapshot).hexdigest(),
        sources_hash=hashlib.sha256(sources_snapshot).hexdigest(),
        profile_snapshot_bytes=profile_snapshot,
        strategy_snapshot_bytes=strategy_snapshot,
        sources_snapshot_bytes=sources_snapshot,
    )
