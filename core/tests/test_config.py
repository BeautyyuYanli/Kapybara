from pathlib import Path

import pytest

from k.config import Config
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
        == "script -q -c '. ${K_CONFIG_BASE:-~/.kapybara}/.bashrc; echo hello' "
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

    assert "${K_CONFIG_BASE:-~/.kapybara}/.bashrc" in command
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
    assert "K_CONFIG_BASE" in helper.last_command
    assert shell_manager.command is not None
    assert shell_manager.command.startswith("wrapped:")
