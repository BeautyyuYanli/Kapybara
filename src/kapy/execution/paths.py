"""Pure XDG path calculations shared by the daemon and Gateway CLI."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from platformdirs import user_data_path, user_runtime_path, user_state_path


@dataclass(frozen=True, slots=True)
class ExecutionPaths:
    state_dir: Path
    data_dir: Path
    runtime_dir: Path

    @property
    def socket_path(self) -> Path:
        return self.runtime_dir / "daemon.sock"

    def session_cwd(self, session_id: str) -> Path:
        encoded = session_id.encode("utf-8")
        if not encoded or len(encoded) > 128:
            raise ValueError("session_id must contain between 1 and 128 UTF-8 bytes")
        return self.data_dir / "sessions" / sha256(encoded).hexdigest() / "cwd"


def resolve_paths(
    *,
    state_dir: Path | None = None,
    data_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> ExecutionPaths:
    """Calculate absolute paths without creating directories or opening files.

    platformdirs supplies the Linux defaults, including its per-UID temporary
    fallback when XDG_RUNTIME_DIR is unset. The daemon, not this helper, verifies
    directory ownership/permissions and obtains exclusive locks before using it.
    """
    paths = ExecutionPaths(
        state_dir=state_dir if state_dir is not None else user_state_path("kapy"),
        data_dir=data_dir if data_dir is not None else user_data_path("kapy"),
        runtime_dir=runtime_dir if runtime_dir is not None else user_runtime_path("kapy"),
    )
    if not all(path.is_absolute() for path in (paths.state_dir, paths.data_dir, paths.runtime_dir)):
        raise ValueError("Execution directories must be absolute paths")
    return paths
