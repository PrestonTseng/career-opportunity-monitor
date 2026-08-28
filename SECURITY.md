# Security

## Private-data boundary

Treat resume facts, strategy values, SQLite history, receipts, errors, and reports as private data.

Keep private runtime files outside Git. You can use the ignored `.runtime/` directory for local work.

Compose mounts the resume file and strategy directory as read-only paths. The container writes runtime data only under `/data`.

Do not copy private inputs into these locations:

- Tracked files.
- Git objects.
- Test fixtures.
- Public examples.
- Build contexts.
- Image layers.
- Shared logs.
- Task comments or metadata.

The `.gitignore` and `.dockerignore` files exclude common private paths. These files are safeguards, not permission to store secrets in the repository.

## API secrets

Use a read-only secret file for an optional LLM API key. Do not put a key in an environment example or command history.

Configured Workday sources need no credential. The application exposes no network port.

## Reports

A report can contain evidence IDs and job-scoring reasons. Keep report files private even when the IDs do not contain a person's name.

Review a report before you share it. Official job links are public, but the score and evidence mapping are private.

## Vulnerability reports

Do not open a public issue that contains private data or an active secret. Contact the repository owner through an approved private channel.

Include the affected revision and a minimal fictional reproduction. Do not include a real resume, strategy, database, receipt, or report.
