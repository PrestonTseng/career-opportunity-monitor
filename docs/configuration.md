# Configuration and private setup

The monitor reads one resume file and one configuration directory. The directory must contain exactly four YAML files.

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

Mount the resume file and the configuration directory as read-only inputs. Store the SQLite database, reports, receipts, and errors in the runtime volume.

## Runtime mode

`CAREER_MONITOR_MODE` accepts `production` or `demo`. Production is the default.

Production mode rejects the public fictional profile and strategy identities. Demo mode permits them for the fictional acceptance flow.

Do not use demo mode with private data or a real webhook.

## Resume facts

`resume_facts.yaml` contains a profile ID, a location, and evidence facts. Each fact has a stable ID.

A fact status is `confirmed` or `planned`. Only confirmed facts can add score points or evidence IDs.

The file must obey `schemas/v1/resume-facts.schema.yaml`. The monitor rejects duplicate fact IDs and extra fields.

## Strategy

`strategy.yaml` defines role targets, markets, company preferences, ranking weights, caps, and the optional LLM adjustment.

The five weights must total 100. A category cap cannot exceed its weight.

The missing-data policy is `unknown`. Missing job fields do not add points, and the monitor does not change the score scale.

The file must obey `schemas/v1/strategy.schema.yaml`.

## Approved sources

`sources.yaml` is an ordered allowlist of source instances. Each entry has a stable ID and an enabled state.

The closed registry accepts only the `workday` adapter. It does not load Python entry points from configuration.

Each source defines an approved HTTPS origin, Workday tenant, site, company label, search text, and bounded request options.

Requests and redirects must stay on the exact approved origin. The monitor rejects local, private, credential-bearing, fragmented, and cross-origin URLs.

Read [Workday adapter examples](workday-adapter.md) before you add a source. The file must obey `schemas/v1/sources.schema.yaml`.

## Discord destinations

`destinations.yaml` is an ordered allowlist. Each Discord destination has an ID, an enabled state, report cadences, and `webhook_url_file`.

Set `webhook_url_file` to `/run/secrets/discord-webhook` for Compose. Never put a webhook URL in YAML.

Create the webhook in the Discord server integration settings. Copy the URL into one private file as one line.

Set `CAREER_MONITOR_DISCORD_SECRET_FILE` to the host path. Compose mounts the file as a read-only secret.

The monitor opens secret files only for enabled destinations. It accepts only approved Discord HTTPS webhook hosts.

The file must obey `schemas/v1/destinations.schema.yaml`.

## Daily and weekly schedules

`schedule.yaml` contains an IANA timezone and daily and weekly cadence settings. Each cadence has an enabled state and a cron value.

Daily cron uses `minute hour * * *`. Weekly cron uses `minute hour * * weekday`.

The scheduler interprets these values as local wall time. It derives report IDs and weekly boundaries from local calendar dates.

A missing DST spring time does not run. A repeated DST fall time follows the Supercronic cron behavior.

The file must obey `schemas/v1/schedule.schema.yaml`.

## Environment file

Copy `.env.example` to `.env`. Set these host paths:

```text
CAREER_MONITOR_MODE=production
CAREER_MONITOR_RESUME_FILE=./private/resume_facts.yaml
CAREER_MONITOR_CONFIG_DIR=./private/config
CAREER_MONITOR_DISCORD_SECRET_FILE=./private/secrets/discord-webhook
```

`.env.example` contains path and configuration choices only. It contains no secret value.

## Validation summary

Run validation after each input change:

```text
docker compose --profile cli run --rm cli validate
```

The JSON summary identifies the selected resume, configuration, and data paths. It reports schema versions, IDs, enabled states, cadences, cron values, and timezone.

The summary never prints resume statements, strategy contents, webhook file paths, webhook URLs, or secret bytes.

## Other runtime values

- `CAREER_MONITOR_DISPLAY_LIMIT` sets the maximum jobs in one report.
- `CAREER_MONITOR_LOCK_TIMEOUT_SECONDS` sets the writer-lock wait time.
- `CAREER_MONITOR_RUN_ID` gives one run a stable identity.
- `CAREER_MONITOR_NOW` is a test-only UTC timestamp override.

Read [Optional LLM API](llm-api.md) for the optional LLM values.
