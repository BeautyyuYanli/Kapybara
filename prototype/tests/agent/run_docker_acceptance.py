"""Run the committed Execution snapshot against this worktree's Agent and Skills.

Usage: uv run python tests/agent/run_docker_acceptance.py
Only Git export and Docker launch run on the host; machine effects stay in Docker.
"""

import io
import subprocess
import tarfile
import tempfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="kapy-agent-execution-") as directory:
        snapshot = subprocess.check_output(
            ["git", "archive", "27a34dd", "src/kapy/execution"], cwd=root
        )
        with tarfile.open(fileobj=io.BytesIO(snapshot)) as archive:
            archive.extractall(directory, filter="data")
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--init",
                "--network",
                "none",
                "--memory",
                "768m",
                "--pids-limit",
                "128",
                "--mount",
                f"type=bind,src={root / 'src'},dst=/kapy-src,readonly",
                "--mount",
                f"type=bind,src={directory}/src/kapy/execution,"
                "dst=/kapy-src/kapy/execution,readonly",
                "--mount",
                f"type=bind,src={root / 'tests'},dst=/kapy-tests,readonly",
                "--env",
                "PYTHONPATH=/kapy-src:/kapy-tests",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "kapy-v2-machine:dev",
                "python",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "/kapy-tests/agent/docker_manager_acceptance.py",
            ],
            check=True,
            cwd=root,
        )


if __name__ == "__main__":
    main()
