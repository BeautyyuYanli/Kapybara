"""Refresh the shellctl snapshot from one pinned Dify commit.

Only COPY_PATHS destinations and UPSTREAM.json are generated. Local packaging,
build entry points and integration tests live outside those destinations. The
source is always a Git commit, including with --source: uncommitted changes and
build artifacts in an existing clone are never copied. Requires Python 3.12+ and
Git; Kapy's runtime dependencies are not needed.
"""

import argparse
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory

UPSTREAM_REPOSITORY = "https://github.com/langgenius/dify.git"
UPSTREAM_COMMIT = "509f3407a5e3dfd7b7cb93b35f08084fabd12f3e"
PACKAGE = Path(__file__).resolve().parents[1] / "packages" / "shellctl"
COPY_PATHS = {
    "dify-agent-runtime": "server",
    "dify-agent/src/shellctl": "src/shellctl",
    "dify-agent/tests/local/shellctl": "tests/upstream",
    "LICENSE": "LICENSE",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Reuse a Dify Git clone containing the commit")
    args = parser.parse_args()

    with TemporaryDirectory(prefix="kapy-shellctl-") as temporary:
        staging = Path(temporary)
        repository = args.source
        if repository is None:
            repository = staging / "dify.git"
            subprocess.run(["git", "init", "--bare", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "remote", "add", "origin", UPSTREAM_REPOSITORY],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "fetch",
                    "--depth=1",
                    "--filter=blob:none",
                    "origin",
                    UPSTREAM_COMMIT,
                ],
                check=True,
            )

        archive = staging / "source.tar"
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "archive",
                "--format=tar",
                f"--output={archive}",
                UPSTREAM_COMMIT,
                *COPY_PATHS,
            ],
            check=True,
        )
        extracted = staging / "source"
        with tarfile.open(archive) as source:
            source.extractall(extracted, filter="data")

        # Finish fetching and extracting before replacing any generated paths.
        for upstream, local in COPY_PATHS.items():
            source = extracted / upstream
            destination = PACKAGE / local
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)

        (PACKAGE / "UPSTREAM.json").write_text(
            json.dumps(
                {"repository": UPSTREAM_REPOSITORY, "commit": UPSTREAM_COMMIT, "paths": COPY_PATHS},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Copied shellctl at {UPSTREAM_COMMIT} to {PACKAGE}")


if __name__ == "__main__":
    main()
