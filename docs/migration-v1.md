# Migration from published v1

This candidate keeps the version 1 resume, strategy, job, score, and storage contracts. It adds required configuration documents and runtime roles.

## Before the migration

1. Stop the old scheduler.
2. Back up the complete runtime volume.
3. Save the old private resume and strategy files outside Git.
4. Record the old image revision.

## Build the new private layout

Keep the existing version 1 `resume_facts.yaml` and `strategy.yaml`. Put the strategy file in the new `config/` directory.

Add these version 1 documents:

- `sources.yaml` with explicit approved Workday instances.
- `destinations.yaml` with disabled destinations first.
- `schedule.yaml` with an IANA timezone and explicit cadences.

Move each Discord webhook URL into a private secret file. Put only `/run/secrets/discord-webhook` in `destinations.yaml`.

Set `CAREER_MONITOR_MODE=production`. Replace the public fictional profile and strategy identities before validation.

## Database migration

The application applies forward SQLite migrations when it opens the runtime database. The new schema keeps prior job and scoring history.

The delivery and weekly tables add durable evidence. They do not change prior report bytes or prior score records.

Do not open the migrated database with an older image. Restore the matching backup for a rollback.

## First start

1. Build the new image.
2. Run `validate`.
3. Run `daily --dry-run`.
4. Run one manual daily command with destinations disabled.
5. Examine the report and source receipts.
6. Enable one destination and run the next approved delivery.
7. Start the scheduler.

## Behavior changes

The monitor now reads multiple configured sources through a closed registry. No company-specific source is a core default.

Daily and weekly schedules use `schedule.yaml`. Weekly reports use stored daily history and do not recollect source data.

Production mode fails when public fictional profile or strategy identities remain selected. Demo mode permits only an explicit fictional flow.

The `validate` command now prints a secret-safe configuration summary. It does not print private contents or secret paths.
