from pathlib import Path

import pytest

from k.config import Config, config_toml_path, load_kapybara_toml_config
from k.runner_helpers.basic_os import BasicOSHelper, agent_config_base_value


def test_config_defaults_expand_to_home_paths(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config = Config()

    assert config.config_base == (home / ".kapybara").resolve()
    assert config.ssh_key == (home / ".ssh" / "id_ed25519").resolve()
    assert config.ssh_user is None
    assert config.ssh_addr is None


def test_load_kapybara_toml_config_requires_file_when_missing(tmp_path: Path) -> None:
    config_base = tmp_path / ".kapybara"

    assert config_toml_path(config_base) == (config_base / "config.toml").resolve()
    with pytest.raises(ValueError, match="Expected config file"):
        _ = load_kapybara_toml_config(config_base)


def test_load_kapybara_toml_config_reads_agent_run_model_name(tmp_path: Path) -> None:
    config_base = tmp_path / ".kapybara"
    path = config_toml_path(config_base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[agent_run]\nmodel_name = "gpt-5.2"\n',
        encoding="utf-8",
    )

    loaded = load_kapybara_toml_config(config_base)

    assert loaded.agent_run.model_name == "gpt-5.2"


def test_load_kapybara_toml_config_rejects_blank_model_name(tmp_path: Path) -> None:
    config_base = tmp_path / ".kapybara"
    path = config_toml_path(config_base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[agent_run]\nmodel_name = "   "\n', encoding="utf-8")

    with pytest.raises(ValueError, match="model_name must not be empty"):
        _ = load_kapybara_toml_config(config_base)


def test_ssh_key_relative_path_resolves_from_cwd(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    config = Config(
        config_base=tmp_path / "state" / ".kapybara",
        ssh_key=Path("keys/id_ed25519"),
    )

    assert config.ssh_key == (workspace / "keys/id_ed25519").resolve()


def test_basic_os_helper_uses_resolved_ssh_key_path(tmp_path: Path) -> None:
    config = Config(
        config_base=tmp_path / "state" / ".kapybara",
        ssh_user="alice",
        ssh_addr="example.com",
        ssh_port=2200,
        ssh_key=Path("/tmp/custom_id_ed25519"),
    )

    helper = BasicOSHelper(config=config)

    assert helper.command_base() == (
        "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f'-o LogLevel=ERROR -tt -i "{config.ssh_key!s}" -p 2200 '
        "alice@example.com "
    )


def test_basic_os_helper_uses_local_script_when_ssh_endpoint_is_unset(
    tmp_path: Path,
) -> None:
    config = Config(config_base=tmp_path / "state" / ".kapybara")
    helper = BasicOSHelper(config=config)

    assert helper.command_base() == "script -q -c "
    assert (
        helper.command("echo hello")
        == 'script -q -c \'if [ -z "${K_CONFIG_BASE:-}" ]; then export K_CONFIG_BASE=~/.kapybara; else export K_CONFIG_BASE; fi; . "$K_CONFIG_BASE/.bashrc"; echo hello\' '
        "/dev/null"
    )


def test_basic_os_helper_uses_agent_view_config_base_not_python_config_base(
    tmp_path: Path,
) -> None:
    config = Config(
        config_base=tmp_path / "python-runtime" / ".kapybara",
        ssh_user="alice",
        ssh_addr="example.com",
    )
    helper = BasicOSHelper(config=config)
    command = helper.command("echo hello")

    assert "export K_CONFIG_BASE=~/.kapybara" in command
    assert "$K_CONFIG_BASE/.bashrc" in command
    assert str(config.config_base) not in command


@pytest.mark.parametrize(
    ("ssh_user", "ssh_addr"),
    [("alice", None), (None, "example.com")],
)
def test_config_requires_both_ssh_user_and_ssh_addr(
    tmp_path: Path,
    ssh_user: str | None,
    ssh_addr: str | None,
) -> None:
    with pytest.raises(
        ValueError,
        match="ssh_user and ssh_addr must either both be set or both be None",
    ):
        _ = Config(
            config_base=tmp_path / "state" / ".kapybara",
            ssh_user=ssh_user,
            ssh_addr=ssh_addr,
        )


@pytest.mark.anyio
async def test_agent_config_base_value_reads_shell_runtime_marker() -> None:
    class _FakeBasicOSHelper:
        last_command: str | None = None

        def command(self, command: str, env: dict[str, str] | None = None) -> str:
            _ = env
            self.last_command = command
            return f"wrapped:{command}"

    class _FakeShellManager:
        command: str | None = None

        async def new_shell(
            self,
            command: str,
            *,
            options: object | None = None,
            desc: str | None = None,
        ) -> str:
            _ = options, desc
            self.command = command
            return "000001"

        async def next(
            self,
            session_id: str,
            stdin: bytes | None = None,
            timeout_seconds: float | None = None,
        ) -> tuple[bytes, bytes, int | None]:
            _ = session_id, stdin, timeout_seconds
            return b"noisy\n__KAPY_AGENT_CONFIG_BASE__=/runtime/.kapybara\n", b"", 0

        async def interrupt(self, session_id: str) -> None:
            _ = session_id

    helper = _FakeBasicOSHelper()
    shell_manager = _FakeShellManager()

    value = await agent_config_base_value(
        basic_os_helper=helper, shell_manager=shell_manager
    )

    assert value == "/runtime/.kapybara"
    assert helper.last_command is not None
    assert (
        helper.last_command
        == 'printf "__KAPY_AGENT_CONFIG_BASE__=%s\\n" "$K_CONFIG_BASE"'
    )
    assert shell_manager.command is not None
    assert shell_manager.command.startswith("wrapped:")
