from pathlib import Path

import pytest

from kapy.execution import resolve_paths


def test_xdg_paths_shared_with_cli_without_creating_directories(
    tmp_path: Path, monkeypatch
) -> None:
    for variable, directory in (
        ("XDG_STATE_HOME", "state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_RUNTIME_DIR", "runtime"),
    ):
        monkeypatch.setenv(variable, str(tmp_path / directory))
    paths = resolve_paths()
    assert paths.state_dir == tmp_path / "state" / "kapy"
    assert paths.data_dir == tmp_path / "data" / "kapy"
    assert paths.socket_path == tmp_path / "runtime" / "kapy" / "daemon.sock"
    assert not list(tmp_path.iterdir())


def test_explicit_roots_and_opaque_session_ids(tmp_path: Path) -> None:
    paths = resolve_paths(
        state_dir=tmp_path / "s", data_dir=tmp_path / "d", runtime_dir=tmp_path / "r"
    )
    first = paths.session_cwd("../../outside")
    second = paths.session_cwd("a different session")
    assert first != second
    assert first.parent.parent == tmp_path / "d" / "sessions"
    assert first.name == "cwd"
    assert len(first.parent.name) == 64
    assert resolve_paths(data_dir=tmp_path / "d").session_cwd("../../outside") == first
    assert not first.exists()


@pytest.mark.parametrize("session_id", ["", "a" * 129, "界" * 43, "\ud800"])
def test_session_id_limit_counts_utf8_bytes(tmp_path: Path, session_id: str) -> None:
    paths = resolve_paths(data_dir=tmp_path)
    with pytest.raises(ValueError):
        paths.session_cwd(session_id)
    assert paths.session_cwd("界" * 42).is_absolute()


@pytest.mark.parametrize("field", ["state_dir", "data_dir", "runtime_dir"])
def test_relative_overrides_are_rejected(field: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        resolve_paths(**{field: Path("relative")})
