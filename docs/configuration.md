# Configuration and resume facts

The monitor reads one resume-facts file and one strategy directory. The strategy directory must contain only `strategy.yaml`.

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

The strategy file must obey `schemas/v1/strategy.schema.yaml`. The current validation source is NVIDIA Workday for Taiwan.

## Compose paths

Set the private input paths before you run Compose:

```text
export CAREER_MONITOR_RESUME_FILE="$PWD/private/resume_facts.yaml"
export CAREER_MONITOR_CONFIG_DIR="$PWD/private/strategy/v1"
```

Compose mounts the resume file at `/profile/resume_facts.yaml`. It mounts the strategy directory at `/config`.

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

The output gives the profile ID and strategy ID. It does not print resume facts.
