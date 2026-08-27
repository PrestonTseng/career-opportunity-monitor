# Architecture

## Runtime boundary

Docker Compose starts one immutable image. The `cli` profile runs one command and stops. The Compose init process is PID 1 for the `scheduler` profile.

The init process starts Supercronic and forwards termination signals.

The image runs as an unprivileged user. Compose removes Linux capabilities and uses a read-only root file system.

## Data flow

The configuration loader reads `resume_facts.yaml` and `strategy.yaml`. It validates each file before a run uses the values.

The NVIDIA Workday adapter reads public NVIDIA Taiwan job pages. It accepts only official NVIDIA Workday links.

The collection service stores each raw observation and each canonical job version in SQLite. A source receipt records counts, limits, and failures.

The ranking service creates a deterministic base score for every accepted job. The score includes title, skills, experience, location, and company categories.

The optional LLM service can add a bounded adjustment. The deterministic evidence and base score do not change.

The report service stores immutable Markdown bytes in SQLite. It also writes the same bytes to the configured report path.

## Trust boundaries

The resume profile and strategy are private inputs. Compose mounts them as read-only paths.

The NVIDIA Workday service is an untrusted public source. The adapter applies request, page, response-size, and URL limits.

An LLM endpoint is optional and untrusted. The client accepts one strict JSON object and caps the adjustment from -5 through 5.

SQLite is the durable local record. One operating-system lock prevents two writers from changing history at the same time.

## Failure model

A complete source run can mark missing old jobs as stale. A failed source run does not close jobs.

A partial source run records each accepted job and each source failure. The report shows the partial state.

The runtime writes an error receipt after a validation, source, storage, or report failure. It exits with a nonzero status.
