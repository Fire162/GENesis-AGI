# Transcript analytics

The optional local CLI collects Claude Code project and subagent JSONL from
`~/.claude/projects` into `~/.genesis/analytics/transcripts`. Codex logs are outside
v1. It uses the optional `transcript-analytics` package extra and systemd user
scopes; the running Genesis database is not involved.

The shipped `config/transcript_analytics.yaml` defaults to disabled. Opt in through
the existing private `~/.genesis/config/transcript_analytics.local.yaml` overlay:

```yaml
enabled: true
```

The canonical configuration must remain installed even with a private opt-in.
Defaults cap each admitted command at 25% of effective memory and CPU capacity;
`ram_bytes`, `ram_pct` and `cpu_pct` provide explicit overrides. Admission can defer
work with exit 75. Unavailable resource enforcement refuses a launch with exit 69.
Very small CPU caps can exceed the existing 30-second startup deadline.

With the optional extra available, manual use follows the existing CLI:

```bash
python -m genesis transcripts status
python -m genesis transcripts ingest
python -m genesis transcripts sql 'SELECT tool,count(*) FROM tool_calls GROUP BY tool' --live --csv
python -m genesis transcripts verify
```

`ingest` refreshes source records and derives a disposable snapshot unless
`--no-derive` is supplied. `derive` builds it separately. Cached SQL uses only a
compatible, current snapshot; a stale, missing or unreadable cache falls back to
live views over the same captured source selection. `verify` requires an available
current cache and compares schemas, counts and row digests with forced live views.
Excluded sources keep verification incomplete.

Use `sql --live` for raw tables and SQL text containing raw-looking names, including
harmless literals or comments. The first release retains that documented text-gate
usability quirk. A query's `--window` is a provenance label; filter timestamps in
SQL for an actual analysis window. `--since` selects source files during ingestion
and does not filter the records returned by SQL. Use a distinct `--manifest PATH`
for each query so later queries do not overwrite earlier evidence.

`GENESIS_TRANSCRIPT_ANALYTICS_DISABLED=1` or a `DISABLED` file in the configured
data directory blocks analytics commands while retaining saved opt-in. Status
reports disabled; timer invocations skip without failing. First cancellation asks
for cooperative shutdown; a second cancellation forcibly stops the owned scope.
Wait for supervised commands and their scopes to finish before destructive
uninstallation. The separate installer/retirement rollout is required before
unattended hourly collection; this CLI does not activate scheduling.

Accepted revisions remain until explicit maintenance. Stop readers before pruning.
Historical adapters retain original provenance and mark unavailable facts unknown.
See the [store and reader contract](../../src/genesis/transcript_analytics/README.md)
for source selection, retention, reconciliation and snapshot details.
