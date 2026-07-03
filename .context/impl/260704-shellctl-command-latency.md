# Implementation: shellctl command latency

## Summary

Implemented the lightweight shellctl runtime command layer described by
`/home/beautyyu/.codex/worktrees/adb9/dify/.context/proposals/260703-shellctl-command-latency.md`
in the clean worktree `/tmp/kapybara-shellctl-command-latency-260703`
on branch `codex/shellctl-command-latency-260703`.

The implementation adds stdlib-only hot-path modules under
`shellctl_runtime` and wires shellctl job finalization to console scripts:

- `shellctl-sanitize-pty = shellctl_runtime.sanitize:main`
- `shellctl-runner-exit = shellctl_runtime.runner_exit:main`

`TmuxController._pipe_command_source()` now uses those scripts directly
instead of invoking the heavy shellctl package/server module paths for every
job.

## Major Additions

- Added `shellctl_runtime.sanitize`, a streaming PTY sanitizer with an
  incremental UTF-8 decoder, CSI/OSC stripping, carriage-return progress-line
  normalization, ready-file support, and a tiny CLI.
- Added `shellctl_runtime.runner_exit`, a stdlib-only SQLite finalizer for
  drained job exits. It updates running jobs to `exited`, keeps terminal rows
  idempotent, honors `ShellctlConfig.sqlite_busy_timeout_ms`, and performs the
  terminal/non-terminal decision inside the SQL update to avoid clobbering a
  concurrent terminal writer.
- Added `shellctl_runtime.paths` for shared stdlib-only runtime path constants.
- Added console script declarations in `shell-session-manager/pyproject.toml`.
- Added tests covering sanitizer behavior, direct command invocation, runner
  exit success/failure/idempotency/timeout/concurrency, tmux pipe command
  composition, console script availability, import latency, and no-heavy-import
  guards.
- Added regression tests locking down removal of the old sanitizer module and
  root/shared sanitizer export surfaces.

## Major Changes

- `ShellctlConfig` defaults now point at `shellctl-sanitize-pty` and
  `shellctl-runner-exit`. The previous default `python -m ...` hot-path command
  references were removed from the config defaults.
- `TmuxController._pipe_command_source()` preserves the existing drain ordering:
  sanitizer reaches EOF, `output.log` is flushed, the drain marker is written,
  then `runner-exit` records exit metadata. Runner-exit stderr is appended to
  `pipe-error.log`; failures are made visible there while the pipe exit code
  remains tied to sanitizer status so already-drained jobs are not transiently
  misclassified as `pipe_failed`.
- The old `shell_session_manager.shellctl.sanitize_pty` module was removed per
  user decision. Internal shellctl usage now targets `shellctl_runtime.sanitize`
  or the `shellctl-sanitize-pty` console script directly.
- The old root/shared sanitizer re-export surfaces were also removed:
  `shell_session_manager.shellctl.PtySanitizer`,
  `shell_session_manager.shellctl.sanitize_pty_output`, and the matching
  `shell_session_manager.shellctl.shared` exports no longer exist.
- Package root exports for `shell_session_manager.shellctl`,
  `shell_session_manager.shellctl.shared`, and
  `shell_session_manager.shellctl.server` were made lazier or redirected to
  concrete modules so small imports do not pull the client/server stack
  accidentally.
- Internal imports were moved away from broad package re-exports where needed.

## Differences From The Original Proposal

- The original proposal allowed shellctl itself to stop using the old
  `python -m shell_session_manager.shellctl.sanitize_pty` entrypoint. A later
  user decision confirmed there are no outside users to preserve, so the
  compatibility module and related sanitizer re-export surfaces were deleted
  instead of retained.
- Runner-exit failures are logged to `pipe-error.log` instead of making the
  tmux pipe command exit with the runner-exit status. This preserves the
  proposal invariant that drained output should not be treated as pipe failure;
  reconciliation can still recover from drained artifacts.
- The tests verify console script metadata and executable availability in the
  project environment, but they do not build a separate wheel or Docker image.
- `pyproject.toml` license metadata was normalized to a form accepted by the
  project tooling so `pdm run fix` could parse and update the file.

## Verification

Elysia ran these checks in `shell-session-manager/` after the compatibility
module removal follow-up:

- `pdm run fix` passed
- `pdm run typecheck` passed
- `pdm run test` passed with `89 passed`
