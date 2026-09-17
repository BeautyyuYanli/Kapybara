"""Run only inside the disposable Docker machine; no host tool execution."""

import hashlib
import json
import os
import platform
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    if not Path("/.dockerenv").exists():
        raise RuntimeError("This verification must run inside Docker")
    resources = Path("/kapy-src/kapy/agent/resources/apply_patch")
    arch = platform.machine()
    manifest = json.loads((resources / "manifest.json").read_text())
    with tempfile.TemporaryDirectory(prefix="kapy-patch-") as directory:
        root = Path(directory)
        for name, metadata in manifest["platforms"][arch]["files"].items():
            content = (resources / arch / name).read_bytes()
            assert hashlib.sha256(content).hexdigest() == metadata["sha256"]
            (root / name).write_bytes(content)
        subprocess.run(["chmod", "700", "--", str(root / "apply_patch")], check=True)
        patch = (
            "*** Begin Patch\n*** Add File: result.txt\n+first line\n"
            "+literal $(touch forbidden)\n*** End Patch\n"
        )
        stdin_path = root / "stdin"
        stdin_path.write_bytes(patch.encode())
        command = ["/bin/sh", "-c", 'exec "$@" < "$0"', str(stdin_path), str(root / "apply_patch")]
        result = subprocess.run(command, cwd=root, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert (root / "result.txt").read_text() == "first line\nliteral $(touch forbidden)\n"
        assert not (root / "forbidden").exists()
        stdin_path.write_text(
            "*** Begin Patch\n*** Update File: result.txt\n@@\n-first line\n"
            "+updated\n*** End Patch\n"
        )
        subprocess.run(command, cwd=root, check=True, capture_output=True, timeout=10)
        assert (root / "result.txt").read_text().startswith("updated\n")
        os.unlink(stdin_path)
        print("verified pinned binary, stdin redirection, add/update, literal shell content")


if __name__ == "__main__":
    main()
