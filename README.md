# Career Opportunity Monitor

Career Opportunity Monitor is a self-hosted job monitor. It reads NVIDIA Taiwan jobs from the public NVIDIA Workday service.

The monitor keeps job history in SQLite. It scores jobs against a private resume profile and a private strategy.

## Requirements

Install Docker with the Compose plugin. You do not need a local Python installation for the quick start.

## Quick start

The files in `examples/` describe a fictional person. Replace them only after the fictional flow works.

1. Clone this repository.
2. Open a shell in the repository root.
3. Build the CLI image:

   ```text
   docker compose --profile cli build cli
   ```

4. Validate the fictional files:

   ```text
   docker compose --profile cli run --rm cli validate
   ```

5. Do a dry run of the runtime and storage paths:

   ```text
   docker compose --profile cli run --rm cli daily --dry-run
   ```

6. Create the first report from the current public source:

   ```text
   docker compose --profile cli run --rm cli daily
   ```

7. Show the report path from the JSON output. Then copy the report from the volume:

   ```text
   docker compose --profile cli run --rm --entrypoint sh cli -c 'cat /data/reports/daily-*.md'
   ```

The dry run does not contact the source. The live daily command creates a Markdown report but does not send applications or messages.

## Private setup

Do not edit the fictional examples with private values. Put private files outside Git, or put them under the ignored `.runtime/` directory.

Set `CAREER_MONITOR_RESUME_FILE` to the resume-facts file. Set `CAREER_MONITOR_CONFIG_DIR` to the strategy directory.

Compose mounts both paths as read-only files. The monitor writes history, receipts, errors, and reports only to `/data`.

## Scheduler

Start the UTC scheduler after the CLI flow works:

```text
docker compose --profile scheduler up --detach scheduler
```

The scheduler runs the daily command at 00:00 UTC.

## Documentation

- [Architecture](docs/architecture.md)
- [Configuration and resume facts](docs/configuration.md)
- [Optional LLM API](docs/llm-api.md)
- [Operations](docs/operations.md)
- [Security](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## License

Read `LICENSE.md` before you copy or distribute this project.
