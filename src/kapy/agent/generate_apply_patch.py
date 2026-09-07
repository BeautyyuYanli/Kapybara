"""Reproduce bundled apply_patch resources from the pinned upstream release.

Run: python -m kapy.agent.generate_apply_patch [--bundle-dir DIR]
Cached archives, if supplied, undergo the same digest verification as downloads.
This generator never executes upstream binaries.
"""

import argparse
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

VERSION = "rust-v0.153.4"
COMMIT = "8639ac2d93442bcec5631b693b4ed7c0144422b7"
HASHES = {
    "x86_64": "d2b6db33f1ebdbca9691237287fe25ee4fe53b90f0255a7e3cfb82b70c721567",
    "aarch64": "0b518abbbd75016f615a6164c45f1a7571c3be6d6d55559f3019ee4913cfe0b5",
}


def generate(bundle_dir: Path | None = None) -> None:
    destination = Path(__file__).parent / "resources" / "apply_patch"
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {
        "generated_by": "kapy.agent.generate_apply_patch",
        "version": VERSION,
        "source_commit": COMMIT,
        "platforms": {},
    }
    markdown = None
    for arch, digest in HASHES.items():
        name = f"apply-patch-{VERSION}-{arch}-unknown-linux-gnu.tar.gz"
        url = (
            f"https://github.com/BeautyyuYanli/codex-apply-patch/releases/download/{VERSION}/{name}"
        )
        if bundle_dir is not None and (bundle_dir / name).exists():
            data = (bundle_dir / name).read_bytes()
        else:
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read(32 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"Upstream bundle digest mismatch: {arch}")
        files = {}
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                if member.isdir():
                    continue
                if not member.isfile() or member.size > 32 * 1024 * 1024:
                    raise ValueError("Unexpected upstream archive member")
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("Unsafe upstream archive path")
                stream = archive.extractfile(member)
                assert stream is not None
                content = stream.read()
                relative = path.as_posix()
                # Release bundles have a single directory root.
                if len(path.parts) > 1:
                    relative = Path(*path.parts[1:]).as_posix()
                out = destination / arch / relative
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(content)
                files[relative] = {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content),
                }
                if path.name == "SKILL.md":
                    if markdown is not None and markdown != content:
                        raise ValueError("Platform SKILL.md files differ")
                    markdown = content
        manifest["platforms"][arch] = {"url": url, "bundle_sha256": digest, "files": files}
    if markdown is None:
        raise ValueError("Upstream bundle has no SKILL.md")
    source = markdown.decode()
    original = (
        "- Input type: `FREEFORM`.\n- Pass exactly one raw patch string.\n"
        "- Do not wrap the patch in JSON."
    )
    if original not in source:
        raise ValueError("Upstream transport instructions changed")
    description = source.replace(
        original,
        "- Input is a JSON object with a `patch` string field.\n"
        "- The `patch` field contains exactly one raw patch string.",
        1,
    )
    (destination / "SKILL.md").write_bytes(markdown)
    (destination / "description.md").write_text(description, encoding="utf-8")
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path)
    generate(parser.parse_args().bundle_dir)
