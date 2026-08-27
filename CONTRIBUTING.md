# Contributing

Keep each change small and limited to one contract. Add a failing test before you change runtime behavior.

Use fictional values in source files, tests, fixtures, logs, and examples. Do not add private resume facts or contact details.

## Required gates

Run these commands from the repository root:

```text
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
python scripts/check_simple_english.py

docker build --no-cache --tag career-opportunity-monitor:gate .
docker compose --profile cli config
```

Then run the fictional Compose smoke flow from `README.md`. Stop and remove each test project after the smoke flow.

Run `git diff --check` before review. Examine `git status --short` for private or generated files.

## Test rules

Use one behavior in each test. Make sure that each new test fails for the expected reason before you add code.

Use fixed fixtures for source tests. Do not make the unit test suite depend on a live public source.

Record the exact command, exit code, and material result in the review handoff.

## Documentation rules

Use Simple English in all public Markdown. Keep procedures separate from descriptions.

Use `config` as the short technical noun in commands only. Use `configuration` in prose.

Keep commands, paths, identifiers, and quoted errors exact.
