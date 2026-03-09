from pathlib import Path

from k.agent.memory.paths import (
    memory_records_root_from_config_base,
    memory_root_from_config_base,
)


def test_memory_root_from_config_base_uses_memories_subdir(tmp_path: Path) -> None:
    config_base = tmp_path / ".kapybara"
    assert memory_root_from_config_base(config_base) == (
        config_base.resolve() / "memories"
    )


def test_memory_root_from_config_base_expands_home(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert memory_root_from_config_base("~/workspace/.kapybara") == (
        (home / "workspace" / ".kapybara").resolve() / "memories"
    )


def test_memory_records_root_from_config_base_uses_records_subdir(
    tmp_path: Path,
) -> None:
    config_base = tmp_path / ".kapybara"
    assert memory_records_root_from_config_base(config_base) == (
        config_base.resolve() / "memories" / "records"
    )
