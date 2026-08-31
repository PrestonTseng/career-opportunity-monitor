# Architecture

## Runtime roles

Docker Compose builds one immutable image. The `cli` profile runs one command and stops.

The Compose init process is PID 1 for the scheduler profile. It starts Supercronic and forwards termination signals.

The image runs as an unprivileged user. Compose removes Linux capabilities and uses a read-only root file system.

## Configuration flow

The loader reads `resume_facts.yaml` and four configuration documents. It validates one stable snapshot before a command uses the values.

Production mode rejects the public fictional profile and strategy identities. Demo mode permits only an explicit fictional flow.

The secret-safe validation summary reports paths, schema versions, source IDs, destination IDs, cadence settings, and enabled states.

## Daily data flow

1. The closed registry builds enabled configured Workday sources in document order.
2. Each adapter reads only its approved public HTTPS origin.
3. The collection service stores observations and canonical job versions in SQLite.
4. The ranking service scores every accepted job with the preserved score contract.
5. The report service stores immutable Markdown bytes before delivery.
6. Enabled destinations receive the stored report bytes.

One source failure does not remove successful results from other sources. A complete source run can mark its unseen old jobs as stale.

## Weekly data flow

The weekly role reads stored daily report history for the prior completed local week. It does not collect sources again.

The report service stores one deterministic weekly report before delivery. Retry uses the stored report bytes.

## Trust boundaries

The resume profile, strategy, SQLite history, receipts, errors, reports, and secret files are private inputs or outputs.

Configured Workday services are untrusted public sources. The adapter enforces request, page, response-size, URL, DNS, and exact redirect-origin limits.

Discord and the optional LLM endpoint are untrusted destinations. Their secrets enter only through mounted secret files.

SQLite is the durable local record. One operating-system lock prevents concurrent writers from changing one runtime volume.

## Delivery recovery

A delivery stores an indeterminate claim before network I/O. It stores acknowledgment evidence after Discord accepts a chunk.

A retry uses report bytes and delivery evidence from SQLite. It does not build a changed report.

## Failure model

A partial source run records accepted jobs and failures. It does not age unseen jobs.

A validation, source, storage, report, or delivery failure creates error evidence. The command exits with a nonzero status.
