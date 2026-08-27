# Optional LLM API

The LLM step is optional. The deterministic base score is always authoritative.

## Off mode

Keep this value for the default mode:

```text
CAREER_MONITOR_LLM_MODE=off
```

Off mode makes no API request. Each job gets an adjustment of 0 and the report shows `off`.

## OpenAI-compatible mode

Set these values only for an endpoint that you trust:

```text
CAREER_MONITOR_LLM_MODE=openai
CAREER_MONITOR_LLM_BASE_URL=https://llm.example/v1
CAREER_MONITOR_LLM_MODEL=example-model
CAREER_MONITOR_LLM_API_KEY_FILE=/run/secrets/llm-api-key
```

The client sends a request to `/chat/completions`. The endpoint must support strict JSON-schema responses.

The response must contain only an integer `adjustment` and a nonempty `reasons` list. The permitted adjustment is -5 through 5.

The client records request and response hashes. It does not store the API key in an assessment.

## Failure behavior

A timeout gives an adjustment of 0 and a `timeout` status. An unavailable endpoint gives an adjustment of 0 and an `unavailable` status.

An invalid or oversized response gives an adjustment of 0 and an `invalid` status. The daily run continues with the base score.

## Secret boundary

Prefer `CAREER_MONITOR_LLM_API_KEY_FILE` to `CAREER_MONITOR_LLM_API_KEY`. Mount the secret file outside the Git tree.

Do not put a key in `.env.example`, a strategy file, a command, an image layer, or a report.
