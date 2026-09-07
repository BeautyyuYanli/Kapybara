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

## Reviewed State integration

The final State delivery `ac18037` was merged into main as `acf69fc`. All 102
combined State/RPC tests passed in Docker in 83.57 s, using the development
PostgreSQL/Valkey services with isolated test schemas/namespaces. Repository Ruff
and pyrefly checks passed after configuring the shared test import search path.

The included State load scenario accepted, completed and replayed all 2,000 inputs
across 100 sessions in 16.117 s (124.1 inputs/s), preserving each session's order.
The event scenario delivered and consumed all 100 distinct listener inputs;
delivery took 0.506 s and completion took 1.317 s.

## First complete live task

An isolated Compose project `kapy-v2-acceptance-1` was built from committed owned
package snapshots: State `ac18037`, Execution `08f9a90`, Intelligence `22934b2`, and
Gateway `e70c2d1`. Only State had completed final review at this point. The project
has its own PostgreSQL, Valkey and machine volumes; Telegram was explicitly disabled.

`scripts/check_system.py` passed against the running HTTP API and Docker daemon:

- Repeating the creation request returned the same session.
- The real configured model invoked a process tool to generate an unpredictable
  random marker inside the execution container.
- The final model reply matched the marker in the durable tool-return history.
- Eight history records were replayed; the temporary session was deleted afterward.
- Input acceptance took 19.94 ms; task completion took 7.69 s.

Startup exposed an environment-formatting issue: unquoted JSON in `.env` loses its
double quotes under `uv --env-file`. The local machine-token mapping was regenerated
with outer single quotes, and `.env.example` now documents that format.

This is an early end-to-end result. A separate check found that the Agent's login
shell resets the configured PATH and hides the installed `kapy` CLI; Intelligence
owns its correction and recursive-control regression verification. Final component
reviews and the remaining product acceptance scenarios are still in progress.

## Reviewed Execution and independent load measurement

Execution `52746c8` (final code `8cbbfd8`) completed all five review stages and was
merged into main as `cb895c0`. Its 100 Execution/RPC tests also passed within the
lead's composed test run. That run had 204 passing tests overall; 2 failures and
25 setup errors were traced to Gateway/Skills fixtures using hardcoded host database
addresses. Those fixture corrections remain with their owners.

The independent `scripts/bench_machine.py` uses the real MachineService, SQLite,
process manager and filesystem inside a disposable Docker container: no network,
1 GiB memory, 128 PIDs, private PID namespace/init, read-only code mounts.

| Scenario | Result | Time | Peak RSS growth |
| --- | --- | ---: | ---: |
| stdio production and paged consumption | 67,108,864 bytes, exact SHA-256 and stderr | 0.601 s | 256 KiB |
| 16 simultaneously open interactive PTYs | Every distinct reply matched its job; all exited successfully | 0.388 s | 256 KiB |

Each PTY produced over 20,000 bytes before reading input; all retained exactly the
last 8,192 bytes and reported truncation. Process release and session directory
cleanup succeeded. The Python benchmark process peaked at 72,948 KiB RSS. These
are sequential peak-RSS deltas, not total container/child memory or concurrency
ceilings; the measurements do not claim complete cleanup of escaped descendants.

## Credentials and package distribution

The supplied Telegram token passed `getMe`, and `getChat` returned the configured
private chat ID. Both calls returned HTTP 200 / `ok=true`. No real message was sent,
no updates were consumed and no bot configuration was changed.

`uv build` produced the complete provisional product wheel from the recorded
`product-tests-2` snapshot. All seven package entry modules were present; bundled
`apply_patch` resource hashes matched the generated manifest for x86_64 and aarch64.
Installing this wheel with `uvx --from <wheel> kapy --help` succeeded in a temporary
Docker container and exposed `server`, `control-server` and `control`. This checks
distribution of the committed snapshot; the final reviewed main build remains due.

## Concurrent Gateway output observers

The running `kapy-v2-acceptance-1` stack passed `scripts/bench_gateway.py`: four
temporary sessions each received one short real-model prompt while 25 independent
observers per session consumed the output stream (100 observers in total). Every
observer saw unique cursors and the identical ordered record sequence for its
session through the expected final reply; there were no reported request errors.

Input acceptance was 390.50 ms median / 403.49 ms maximum for these four submissions.
All observers completed in 6.30 s, including provider latency. Depending on the
session's streamed deltas, each observer read 27–33 records. Temporary sessions were
deleted afterward. This is a small measured workload, not a production latency SLO.

The shared-pool regression was independently retested using the `product-tests-2`
Gateway `de51640` snapshot: one PostgreSQL connection alternated Gateway dict cursors
and immutable Agent payload reads three times without errors. The earlier row-factory
compatibility defect is fixed in this committed Gateway implementation.

## Control-container restart

Restarting the original development composition exposed a scaffold defect: the
daemon borrowed the control container's network namespace, which was replaced on
restart. The control service became healthy while the daemon's loopback connection
was refused. A small, persistent `network` service now owns the loopback namespace
and published port; both application containers join it independently.

The same isolated acceptance stack then passed a control-only restart. The daemon's
Docker start timestamp did not change, the published address remained stable, and
the daemon could reach the restarted service. The full live `check_system.py` task
passed afterward: stable creation replay, exact random machine marker in final and
durable history, eight history records, 18.09 ms acceptance / 5.99 s completion.
The temporary task was deleted. This check establishes transport recovery after
control-container restart; interrupted model-run recovery remains a separate test.

## Forced crash during a live machine task

The newer isolated `kapy-v2-acceptance-3` stack combines main `95e591d` (final State
and Execution), Agent `de2227e`, and Gateway `9ff3086`. `scripts/check_recovery.py`
observed the machine command's first filesystem effect, sent SIGKILL to the control
container, and restarted only that container while the machine command kept running.

The original submission completed successfully after recovery. Its history recorded
attempts **1 and 2**, and the final random marker matched a durable tool return.
The execution daemon's start timestamp was unchanged. An append-only execution
counter contained exactly **one byte**, confirming that the command ran once across
the control crash. The temporary session and counter file were cleaned afterward.

This check passed after correcting punctuation in the harness's exact-command
prompt; the initial harness attempt exited before creating the counter and never
reached the crash phase. The pass is evidence for the tested recoverable process
operation, not a general exactly-once guarantee for arbitrary external effects.

## Complete merged functional baseline

Main `b003b18` passed **259 tests in 125.58 s**, with zero failures, errors or skips.
The JUnit report contains 259 distinct test identities, including the explicitly
collected real Agent/MachineService acceptance file. This independently combines
all four reviewed module deliveries and the PTY observer correction.

Tests ran in one disposable Docker container with private process/filesystem
namespaces, init, 2 GiB memory and 256 PIDs. This baseline used host networking to
reach the development databases' published loopback ports, while the remaining
fixture-address correction is underway. Final acceptance must also pass on the
standard Compose bridge using environment-overridden service URLs. The two test
files' type errors and Agent shell PATH issue remain assigned to the closeout senior.
