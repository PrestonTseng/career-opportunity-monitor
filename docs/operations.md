# Operations

## Validate and do a dry run

Validate private inputs before each live command:

```text
docker compose --profile cli run --rm cli validate
```

Do a dry run before the first live command:

```text
docker compose --profile cli run --rm cli daily --dry-run
```

A dry run reads and validates all selected configuration. It does not contact a source or destination.

## Run reports manually

Run daily collection, scoring, storage, reporting, and delivery:

```text
docker compose --profile cli run --rm cli daily
```

Build a weekly report from stored daily report history:

```text
docker compose --profile cli run --rm cli weekly
```

The weekly command does not collect source data. A scheduled Monday run summarizes the prior completed local week.

## Start and stop the scheduler

Start the scheduler after validation and a dry run:

```text
docker compose --profile scheduler up --detach scheduler
```

Examine scheduler logs:

```text
docker compose --profile scheduler logs scheduler
```

Stop the scheduler without deleting runtime data:

```text
docker compose --profile scheduler down
```

## Retry delivery

If daily delivery fails, retry the immutable stored report:

```text
docker compose --profile cli run --rm cli retry-delivery --report-date 2026-08-27
```

Retry a weekly report with its local Monday start date:

```text
docker compose --profile cli run --rm cli retry-delivery --cadence weekly --report-date 2026-08-24
```

The monitor stores report bytes before delivery. A retry uses the same stored bytes.

Discord delivery stores chunk claims, acknowledgments, and hashes in SQLite. A retry resumes from durable delivery evidence.

## Backup

1. Stop the scheduler.
2. Find the Compose volume with `docker volume ls --filter name=career-opportunity-monitor_runtime-data`.
3. Copy the complete volume to protected storage.
4. Start the scheduler.

The volume contains private resume snapshots, job evidence, reports, receipts, and delivery evidence. Protect the backup as private data.

## Restore

1. Stop the scheduler.
2. Save the current volume for recovery.
3. Restore the complete matching backup.
4. Run an SQLite integrity test in a protected environment.
5. Run validation and a daily dry run.
6. Start the scheduler.

Do not print private database rows in shared logs.

## Update

1. Stop the scheduler.
2. Back up the runtime volume.
3. Read the migration notes for the target revision.
4. Build the approved image.
5. Run validation.
6. Run a daily dry run.
7. Start the scheduler.
8. Examine the logs.

## Rollback

1. Stop the scheduler.
2. Restore the prior repository revision.
3. Restore its matching runtime-volume backup.
4. Build the prior image.
5. Run validation.
6. Start the scheduler.

Do not use a newer database with an older image unless that revision documents compatibility.

## Troubleshooting

### Validation fails

Read the error without copying private configuration to a shared channel. Make sure that all five YAML documents use schema version 1.

Make sure that the configuration directory contains exactly four expected files. Production mode also rejects public fictional identities.

### Source failures

Each source receipt records listed, accepted, request, and failure counts. A partial source does not close unseen jobs.

If one source fails, other enabled sources continue. If all sources fail, the daily command exits with a nonzero status.

Examine `/data/errors` and the report source-health section. Do not increase limits until you understand the mismatch.

### Discord delivery fails

Make sure that the destination is enabled for the report cadence. Examine the secret-file permissions and the Discord webhook state.

Do not print the webhook URL. Use `retry-delivery` after you correct a temporary problem.

### Scheduler does not run

Examine the normalized Compose configuration and scheduler logs. Make sure that the IANA timezone and cron forms are valid.

A nonexistent spring DST time does not run. Select a stable local time when this behavior is not acceptable.

### Writer lock is busy

Wait for the active command to finish. Do not run daily, weekly, or retry commands in parallel against one runtime volume.
