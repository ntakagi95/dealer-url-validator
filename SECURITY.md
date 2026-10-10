# Security review

Reviewed October 9, 2026, for a locally operated Codex skill. This is a scoped code and dependency review, not a penetration test or a guarantee against future vulnerabilities.

## Controls

- HTTP and browser checks keep TLS verification enabled. A certificate failure remains unverified; the checker never retries with `verify=False`.
- Captures that bypassed TLS cannot supply successful reusable evidence. Offline review also downgrades historical insecure observations unless fresh secure evidence or a separate explicit human confirmation establishes the result.
- URL normalization accepts HTTP/HTTPS and rejects embedded credentials. Website text, CSV values and spreadsheet notes are data, never agent instructions.
- Browser fallback uses a fresh browser context, bounded visits and persisted cooldowns. Persistent bot challenges are unverified and verification controls are not clicked.
- Manual confirmations require matching dealer identity, source homepage and compatible brand metadata. They expire after 24 hours; route hints expire after 30 days. HTTP success is never fabricated from human confirmation.
- Release packaging uses a file allowlist. Local inputs, run reports, account/dealer records, spreadsheet identifiers, captured pages/screenshots, browser history and manual confirmations are excluded. Empty reference stores replace local learning data.

## Validation

The October 9 review passed 79 behavior tests, including TLS failure, insecure capture rejection, expiry, identity matching, request reuse, subset rejection and browser deduplication. Bandit reported no findings. pip-audit reported no known vulnerabilities for 22 resolved runtime, browser and development dependencies, with none skipped. Re-run dependency auditing before publishing later versions because advisory data and dependency resolution change.

## Boundaries

Run locally on websites selected by the operator. The skill is not a hardened network service: arbitrary URL inputs and redirects can reach destinations available to the local machine. Deploying it behind a public API would require network isolation and destination checks at every redirect and browser request.

Browsers execute third-party site JavaScript. Captured evidence and CSV outputs can contain sensitive business data or untrusted strings; keep run folders private and review CSVs before opening them in spreadsheet software. Local evidence files are trusted operator input, not cryptographically authenticated website attestations.

No authenticated dealership sessions, production changes, email sending or GitHub publishing are part of the validator workflow.
