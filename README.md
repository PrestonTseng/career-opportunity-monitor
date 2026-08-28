# Career Opportunity Monitor

Career Opportunity Monitor is a self-hosted job monitor. It reads approved public Workday sources and stores job history in SQLite.

The monitor scores all accepted jobs against a private resume profile and a private strategy. It never sends job applications.

## Requirements

Install Docker with the Compose plugin. The quick start does not require a local Python installation.

## Try the fictional demo

The public examples describe fictional people and companies. Demo mode permits these files and disables Discord delivery.

```text
CAREER_MONITOR_MODE=demo \
CAREER_MONITOR_RESUME_FILE=./examples/resume_facts.yaml \
CAREER_MONITOR_CONFIG_DIR=./examples/strategy/v1 \
docker compose --profile cli run --rm cli validate

CAREER_MONITOR_MODE=demo \
CAREER_MONITOR_RESUME_FILE=./examples/resume_facts.yaml \
CAREER_MONITOR_CONFIG_DIR=./examples/strategy/v1 \
docker compose --profile cli run --rm cli daily --dry-run
```

Production mode is the default. Production mode rejects the public fictional profile and strategy identities.

## Create a private deployment

Use this private layout outside Git, or use the ignored `private/` directory:

```text
private/
  resume_facts.yaml
  config/
    strategy.yaml
    sources.yaml
    destinations.yaml
    schedule.yaml
  secrets/
    discord-webhook
```

Copy `.env.example` to `.env`. Set the three private host paths and keep `CAREER_MONITOR_MODE=production`.

Copy the fictional YAML files as templates. Replace all fictional identity, resume, strategy, source, and destination values.

Create a Discord webhook in the server integration settings. Put its URL as one line in `private/secrets/discord-webhook`.

Do not put the webhook URL in YAML, `.env`, a command, or a log. Restrict the secret file to its owner.

```text
chmod 600 private/secrets/discord-webhook
```

Build the image and validate the selected configuration:

```text
docker compose --profile cli build cli
docker compose --profile cli run --rm cli validate
docker compose --profile cli run --rm cli daily --dry-run
```

The validation output shows selected paths, schema versions, source and destination IDs, schedules, the timezone, and enabled states.

The validation output does not show resume facts, strategy contents, webhook URLs, or secret bytes.

## Run reports

Create and deliver a daily report:

```text
docker compose --profile cli run --rm cli daily
```

Create a weekly report from stored daily history:

```text
docker compose --profile cli run --rm cli weekly
```

Start the daily and weekly scheduler:

```text
docker compose --profile scheduler up --detach scheduler
```

The scheduler uses the IANA timezone and cron values in `schedule.yaml`.

## Documentation

- [Configuration and private setup](docs/configuration.md)
- [Operations, retry, backup, and restore](docs/operations.md)
- [Architecture](docs/architecture.md)
- [Workday adapter examples](docs/workday-adapter.md)
- [Migration from v1](docs/migration-v1.md)
- [Optional LLM API](docs/llm-api.md)
- [Security and privacy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## Limits

The monitor supports only the closed `workday` adapter registry. It does not bypass authentication, robots controls, rate limits, or source terms.

A source can change its public API without notice. Review source failures and reports before you act on the results.

## License

Read `LICENSE.md` before you copy or distribute this project.
