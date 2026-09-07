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

## Event broadcast baseline

Source and container limits are the same State snapshot as above. The independent
`scripts/bench_events.py` harness uses a new schema and observes durable waiting
records after each delivery. Event identity and exact payload sequence are checked.

An event published before any subscriber was retained and delivered to the first
later subscriber. Later subscribers did not receive that already delivered backlog.
Two subsequent broadcasts each reached all 100 subscribers exactly once.

| Round | Deliveries | Publish latency | All subscribers finished |
| --- | ---: | ---: | ---: |
| 1 | 100 | 33.63 ms | 1,016.36 ms |
| 2 | 100 | 34.79 ms | 1,077.33 ms |

## Shared RPC and XDG paths

The `1091292` source snapshot passed all 46 RPC/codec/peer/XDG tests inside Docker
in 0.15 s. Its public modules were integrated as `8e83fe4`; main Ruff and pyrefly
checks passed. This does not yet test the complete machine daemon or file manager.

## File transfer milestone

The independent `fdf4cb6` snapshot passed 71 execution/RPC tests in 4.43 s inside
a disposable Docker container with networking disabled, 1 GiB memory and 128 PIDs.
URL tests used a server on that container's loopback interface. Coverage includes
SQLite crash recovery, staging cleanup, changing source files, checksums, atomic
replacement and local proxy framing. The complete daemon/process manager is pending.

| Transfer | Duration | Reported peak RSS growth |
| --- | ---: | ---: |
| 64 MiB WebSocket push and pull | 1.92 s | 256 KiB |
| 64 MiB URL GET and PUT | 1.72 s | 4,224 KiB |

These process RSS deltas are measurements from this test run, not memory ceilings
for all transfer concurrency patterns.

## Live Runner and State

The `9e0190a` Agent snapshot and `e297398` State snapshot were composed in an isolated
Docker container with real PostgreSQL/Valkey and the configured `gpt-5.6-luna` API.
`scripts/check_runner.py` submitted one short prompt through SessionService, checked
the exact requested reply, closed/reopened the service, and replayed the complete
model response with API-reported usage: **3,821 input / 28 output tokens**.

This verifies the real model/Runner/checkpoint/history path. It has no execution
machine or Telegram frontend; those remain separate end-to-end checks. Only model
credentials were forwarded to the container, and its unique database schema was
removed afterward.
