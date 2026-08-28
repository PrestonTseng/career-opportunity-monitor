# Workday adapter examples

The closed source registry supports the `workday` adapter. Each source instance needs explicit operator approval.

## Find the instance values

Open the public Workday career site in a browser. Identify these values from its public requests:

- The canonical HTTPS origin.
- The Workday tenant name.
- The career site name.
- A clear company label.
- The search text.

Make sure that the source terms permit this use. Do not add a source that requires authentication or bypass controls.

## Add an approved source

Add one ordered entry to `sources.yaml`. Use a stable ID and bounded options.

```yaml
- id: approved-company-workday
  enabled: true
  adapter: workday
  origin: https://approved-company.wd5.myworkdayjobs.com
  tenant: approved-company
  site: ExternalCareerSite
  company: Approved Company
  search_text: Taiwan
  options:
    page_size: 20
    max_pages: 10
    max_requests: 420
    timeout_seconds: 10
    response_limit_bytes: 1000000
    retries: 1
    rate_limit_seconds: 0
```

The origin must not include a path, query, fragment, credential, or port. The runtime keeps all requests and redirects on this origin.

## NVIDIA adapter example

NVIDIA uses the same generic Workday adapter. This sanitized example is disabled until an operator approves it.

```yaml
- id: nvidia-workday-example
  enabled: false
  adapter: workday
  origin: https://nvidia.wd5.myworkdayjobs.com
  tenant: nvidia
  site: NVIDIAExternalCareerSite
  company: NVIDIA
  search_text: Taiwan
  options:
    page_size: 20
    max_pages: 10
    max_requests: 420
    timeout_seconds: 10
    response_limit_bytes: 1000000
    retries: 1
    rate_limit_seconds: 0
```

This example is adapter documentation, not a product default. Make sure that the current public instance values and terms remain valid before use.

## Failure isolation

The runtime processes enabled sources in document order. One source failure does not discard accepted jobs from another source.

A partial source run records accepted jobs and item failures. It does not mark unseen jobs as stale.
