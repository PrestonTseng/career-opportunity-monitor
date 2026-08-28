# Configuration and resume facts

The monitor reads one resume-facts file and one configuration directory. The
configuration directory must contain exactly `strategy.yaml`, `sources.yaml`,
`destinations.yaml`, and `schedule.yaml`.

Use the files in `examples/` as fictional templates. Do not put private values in public examples.

## Resume facts

The `resume_facts.yaml` file contains a profile ID, a location, and evidence facts. Each fact has a stable ID.

A fact status is `confirmed` or `planned`. Only confirmed facts can add score points or evidence IDs.

Use short keywords that can occur in job text. The monitor normalizes letter case and punctuation before it compares keywords.

The resume file must obey `schemas/v1/resume-facts.schema.yaml`. The monitor rejects duplicate fact IDs and extra fields.

## Strategy

The `strategy.yaml` file defines these values:

- Role targets.
- Markets and remote-work rules.
- Preferred, excluded, and referral-available company names.
- Category weights and caps.
- The optional LLM adjustment switch.

The five weights must total 100. A category cap cannot exceed its weight.

The missing-data policy is `unknown`. Missing job fields do not add points, and the monitor does not renormalize the score.

The strategy file must obey `schemas/v1/strategy.schema.yaml`.

## Approved sources

The `sources.yaml` file is an ordered list of source instances. Each entry has a
stable `id`, an `enabled` switch, the closed adapter name `workday`, an
approved HTTPS origin, Workday tenant and site names, a display company, search text, and
bounded request options. The runtime collects enabled entries in document order.
It does not load Python entry points from this file.

The origin must be one canonical public HTTPS origin. Requests and redirects
must stay on that exact origin. Local, private, credential-bearing, fragmented,
and cross-origin URLs are rejected. Copy the fictional and disabled sanitized
instances from `examples/strategy/v1/sources.yaml`. Do not add secrets.

The source file must obey `schemas/v1/sources.schema.yaml`. Existing private
configuration directories must add `sources.yaml` before upgrading.

## Delivery destinations

`destinations.yaml` is a versioned ordered list. Each Discord entry has a stable
ID, an enabled switch, report cadences, and an absolute `webhook_url_file` path.
The YAML file must never contain a webhook URL. Disabled entries are not opened.
The fictional example is disabled.

Create the webhook file outside Git as one private line. Set
`CAREER_MONITOR_DISCORD_SECRET_FILE` to that host path. Compose mounts it at
`/run/secrets/discord-webhook`. It is not passed in an environment value or CLI
argument. Only approved Discord HTTPS webhook hosts are accepted.

## Schedule

`schedule.yaml` contains one IANA timezone and explicit enabled switches and
cron expressions for daily and weekly runs. The supported forms are
`minute hour * * *` for daily and `minute hour * * weekday` for weekly. The
runtime rejects aliases, ranges, steps, malformed values, and daily and weekly
runs at the same wall-clock minute.

Compose remains the only scheduler owner. At startup it validates this file and
generates the Supercronic input. Supercronic interprets both cadences in the
configured local wall time. Across DST changes, a missing spring-forward wall
time does not run and a repeated fall-back wall time follows Supercronic's cron
behavior. Weekly report IDs and Monday-to-Monday data boundaries are still
derived from local calendar dates and do not depend on UTC offset length.
Daily report IDs also use the configured local calendar date, not the UTC date
at which the scheduled process happens to start.

## Compose paths

Set the private input paths before you run Compose:

```text
export CAREER_MONITOR_RESUME_FILE="$PWD/private/resume_facts.yaml"
export CAREER_MONITOR_CONFIG_DIR="$PWD/private/strategy/v1"
export CAREER_MONITOR_DISCORD_SECRET_FILE="$PWD/private/discord-webhook"
```

Compose mounts the resume file at `/profile/resume_facts.yaml`. It mounts the
configuration directory at `/config`.

## Runtime values

The runtime accepts these environment values:

- `CAREER_MONITOR_DISPLAY_LIMIT`: Maximum jobs in one report. The default is 25.
- `CAREER_MONITOR_LOCK_TIMEOUT_SECONDS`: Writer-lock wait time. The default is 0.25 seconds.
- `CAREER_MONITOR_RUN_ID`: Optional stable identity for one run.
- `CAREER_MONITOR_NOW`: Test-only UTC timestamp override.

The LLM values are in [Optional LLM API](llm-api.md).

## Validation

Run this command after each input change:

```text
docker compose --profile cli run --rm cli validate
```

The output gives the profile ID, strategy ID, configured source count, and
enabled source IDs. It does not print resume facts.
