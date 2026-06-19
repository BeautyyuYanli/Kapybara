"""Minimal PTY sanitizer entrypoint used by tmux `pipe-pane`.

This module intentionally stays isolated from the FastAPI/SQLite server stack
so the tmux-side subprocess can touch its ready-file quickly and emit useful
stderr when startup fails before any PTY output is drained.
"""

import argparse
import sys
from pathlib import Path

from shell_session_manager.shellctl.shared.sanitize import sanitize_pty_stream


def parse_args():
    """Parse the tiny CLI contract used by tmux `pipe-pane`."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready-file", type=Path)
    return parser.parse_args()


def run_sanitize_pty(ready_file: Path | None) -> None:
    """Touch the ready file, then sanitize stdin into stdout."""

    if ready_file is not None:
        ready_file.touch()
    sanitize_pty_stream(sys.stdin.buffer, sys.stdout.buffer)


def main() -> None:
    """Run the standalone PTY sanitizer module."""

    args = parse_args()
    run_sanitize_pty(args.ready_file)


if __name__ == "__main__":
    main()
