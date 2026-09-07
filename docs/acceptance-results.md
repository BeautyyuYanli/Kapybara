# Acceptance results

These are incremental measurements of committed snapshots. They do not establish
that the complete product has passed acceptance. The inventory is in acceptance.md.

## State baseline — 2026-09-07

Source: `e297398` (before final review and the approved receipt/export additions).
Harness: `scripts/bench_state.py`, using explicit `migrate` before service startup.
Execution: dedicated Docker container, 2 GiB memory limit, 256 PID limit, read-only
source snapshot. PostgreSQL 17 and Valkey 8 are the Compose development services;
the run creates and drops only its unique schema. The fake runner sleeps for 2 ms
to permit independent sessions to overlap. This does not measure model latency.

| Measurement | Result |
| --- | ---: |
| Sessions | 100 |
| Inputs accepted | 2,000 |
| Inputs completed | 2,000 |
| Completions replayed after service reopen | 2,000 |
| Submission/completion phase | 15.561 s |
| Throughput | 128.52 inputs/s |
| Submission latency P50 | 623.59 ms |
| Submission latency P95 | 1,074.39 ms |
| Peak overlapping fake runners | 2 |
| Per-session exact ordering | Passed |

The harness checks exact input sequences, duplicates, omissions, overlapping runs
within one session, durable completion records and replay after reopening. It does
not simulate an abrupt process crash or external tool recovery; those remain
separate checks. The observed submission latency is a baseline, not a promised SLO.
