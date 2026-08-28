# Security

## Private-data boundary

Treat resume facts, strategy values, SQLite history, receipts, errors, reports, backups, and secret files as private data.

Keep private files outside Git, or use the ignored `private/` and `.runtime/` directories. Restrict each secret file to its owner.

Compose mounts the resume, configuration, and secret files as read-only inputs. The container writes runtime data only under `/data`.

Do not copy private inputs into these locations:

- Tracked files.
- Git objects.
- Test fixtures.
- Public examples.
- Build contexts.
- Image layers.
- Shared logs.
- Task comments or metadata.

The ignore files are safeguards. They do not make repository storage safe for private data.

## Production and demo separation

Production mode is the default. It rejects the public fictional profile and strategy identities.

Demo mode permits public fictional files. Do not combine demo mode with private data or a real destination secret.

## Discord webhook secret

Create a Discord webhook in the server integration settings. Store the URL as one line in a mounted secret file.

Never put the URL in YAML, `.env`, command history, a report, a receipt, or a validation summary.

The destination configuration contains only the absolute container path. The client accepts only approved Discord HTTPS webhook hosts.

## Source network boundary

Each Workday source has one exact approved HTTPS origin. The client rejects credentials, fragments, nonstandard ports, and cross-origin redirects.

The client resolves each request host and rejects non-public addresses. It pins the checked public address for the TLS connection.

The client applies request-count, page-count, response-size, timeout, retry, and rate limits. It does not bypass source authentication.

## Optional LLM secret

Use a read-only secret file for the optional LLM API key. Do not put a key in an environment example.

LLM mode is off by default. The deterministic score and evidence remain available when the endpoint is unavailable.

## Reports and backups

A report can contain score reasons and evidence IDs. Keep reports private and review them before you share them.

Backups contain the same private data as the runtime volume. Encrypt or otherwise protect backup storage.

## Limitations

The monitor does not provide a web authentication boundary because it exposes no network port. Host and backup access controls remain operator duties.

A public source or destination can change behavior without notice. Examine failures before you retry or change limits.

## Vulnerability reports

Do not open a public issue that contains private data or an active secret. Contact the repository owner through an approved private channel.

Include the affected revision and a minimal fictional reproduction. Do not include a real resume, strategy, database, receipt, report, or webhook.
