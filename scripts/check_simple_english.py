from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
BANNED = re.compile(
    r"\b(?:should|shall|however|therefore|might|could|would)\b|;|\b(?:has|have) been\b",
    re.IGNORECASE,
)
SENTENCE = re.compile(r"[^.!?\n]+[.!?]")
WORD = re.compile(r"`[^`]+`|[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*")


def markdown_paths() -> tuple[Path, ...]:
    tracked = subprocess.run(
        ("git", "ls-files", "*.md"),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    local = [
        "README.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        *(str(path.relative_to(ROOT)) for path in (ROOT / "docs").glob("*.md")),
    ]
    return tuple(ROOT / name for name in sorted(set((*tracked, *local))))


def prose_lines(path: Path) -> tuple[tuple[int, str], ...]:
    result: list[tuple[int, str]] = []
    in_fence = False
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or stripped.startswith(("#", "|")):
            continue
        line = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", raw_line)
        line = re.sub(r"\[[^]]+\]\([^)]+\)", "reference", line)
        result.append((number, line))
    return tuple(result)


def violations(path: Path) -> tuple[str, ...]:
    found: list[str] = []
    for line_number, line in prose_lines(path):
        if match := BANNED.search(line):
            found.append(
                f"{path.relative_to(ROOT)}:{line_number}: banned text: {match.group(0)}"
            )
        for sentence in SENTENCE.findall(line):
            count = len(WORD.findall(sentence))
            if count > 25:
                found.append(
                    f"{path.relative_to(ROOT)}:{line_number}: {count}-word sentence"
                )
    return tuple(found)


def main() -> int:
    found = tuple(
        violation for path in markdown_paths() for violation in violations(path)
    )
    if found:
        print("\n".join(found))
        return 1
    print(f"Simple English scan passed for {len(markdown_paths())} Markdown files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
