# Operations

## Start the scheduler

Validate the private inputs first. Then start the scheduler:

```text
docker compose --profile scheduler up --detach scheduler
```

Show scheduler logs:

```text
docker compose --profile scheduler logs scheduler
```

Stop the scheduler without deleting data:

```text
docker compose --profile scheduler down
```

## Run commands manually

Run a daily collection and report:

```text
docker compose --profile cli run --rm cli daily
```

Run a weekly report from already stored daily report history. This command does
not recollect jobs. A scheduled Monday run summarizes the prior completed local
calendar week:

```text
docker compose --profile cli run --rm cli weekly
```

If a report delivery fails, retry the stored report bytes:

```text
docker compose --profile cli run --rm cli retry-delivery --report-date 2026-08-27
```

Retry the immutable weekly report whose ID is its local Monday start date:

```text
docker compose --profile cli run --rm cli retry-delivery --cadence weekly --report-date 2026-08-24
```

Discord delivery is chunked at the destination limit. Acknowledged chunk hashes
are stored in SQLite, so a retry resumes at the first unacknowledged chunk. The
database evidence contains hashes and HTTP classes, never the webhook URL. A
dry run makes no delivery request and records no successful delivery.

A successful command prints one JSON receipt. A failed command prints an error and exits with a nonzero status.

## Backup

Stop the scheduler before you copy the SQLite database. This action prevents a writer from changing the backup.

Find the Compose volume:

```text
docker volume ls --filter name=career-opportunity-monitor_runtime-data
```

Copy the complete volume to protected storage. Keep the backup private because it contains resume snapshots and job evidence.

Start the scheduler after the copy completes. Do an SQLite integrity test after each restore.

## Update

1. Stop the scheduler.
2. Back up the runtime volume.
3. Fetch the approved repository revision.
4. Build the image with `docker compose --profile cli build cli`.
5. Run the validation command.
6. Run a daily dry run.
7. Start the scheduler.
8. Examine the scheduler logs.

## Rollback

1. Stop the scheduler.
2. Restore the prior repository revision.
3. Restore its matching runtime-volume backup.
4. Build the prior image.
5. Run the validation command.
6. Start the scheduler.

Do not use a newer database with an older image unless that revision documents compatibility.

## Source failures

Each configured source receipt records listed, accepted, request, and failure counts. The report shows `partial` when some source items fail.

If the source is partial, examine each failure in the report. Do not treat the omitted source jobs as closed.

If the source fails, the command exits with a nonzero status. Read the JSON file under `/data/errors`.

Retry only after you identify a temporary network or source problem. Do not increase request limits to bypass an unexplained count mismatch.

## Storage checks

Run SQLite integrity and foreign-key checks from a protected maintenance environment. Do not print private snapshot or report tables to shared logs.

## Cleanup

Remove stopped CLI containers:

```text
docker compose --profile cli rm --force --stop cli
```

Delete the runtime volume only when you have an approved backup. The delete action removes job history and reports.
