import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
TEXT_SUFFIXES = {".example", ".json", ".md", ".py", ".toml", ".yaml", ".yml"}
SKIP_PARTS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".runtime",
    ".venv",
    "__pycache__",
}

PRIVATE_MARKERS = (
    "Private" + " Person",
    "private.person" + "@example.invalid",
)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
)


def leakage_reasons(text: str) -> list[str]:
    reasons = [
        marker for marker in PRIVATE_MARKERS if marker.casefold() in text.casefold()
    ]
    reasons.extend(
        pattern.pattern for pattern in SECRET_PATTERNS if pattern.search(text)
    )
    return reasons


def public_text_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and path.suffix in TEXT_SUFFIXES
        and not SKIP_PARTS.intersection(path.relative_to(ROOT).parts)
    ]


@pytest.mark.parametrize("marker", PRIVATE_MARKERS)
def test_leakage_guard_rejects_private_identity(marker: str) -> None:
    assert leakage_reasons(f"private value: {marker}")


@pytest.mark.parametrize(
    "secret",
    [
        "-----BEGIN PRIVATE KEY-----",
        "AKIA" + "A1B2C3D4E5F6G7H8",
        "ghp_" + "a" * 24,
        "sk-proj-" + "b" * 24,
        "xoxb-" + "1234567890-abcdefghij",
    ],
)
def test_leakage_guard_rejects_secret_like_values(secret: str) -> None:
    assert leakage_reasons(secret)


def test_public_tree_has_no_private_or_secret_like_values() -> None:
    leaks = {
        str(path.relative_to(ROOT)): leakage_reasons(path.read_text(encoding="utf-8"))
        for path in public_text_files()
        if path != Path(__file__) and leakage_reasons(path.read_text(encoding="utf-8"))
    }

    assert leaks == {}
