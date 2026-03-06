"""Helpers for building shell commands in SSH or local PTY mode.

`BasicOSHelper` always exports `K_CONFIG_BASE`, defaulting it to `~/.kapybara`
when the runtime shell does not already define it, and then sources
`$K_CONFIG_BASE/.bashrc`.
All payloads are executed through `bash -lc` so shell behavior stays consistent
even when the transport shell is `sh`, `zsh`, or a remote login shell.
When `Config.ssh_user` + `Config.ssh_addr` are configured, commands run over SSH.
When both are `None`, commands run locally via `script ... bash -lc ...` to
preserve pseudo-terminal behavior expected by shell-session tools.
"""

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from k.config import Config

if TYPE_CHECKING:
    from k.io_helpers.shell import ShellSessionManager

DEFAULT_AGENT_CONFIG_BASE = "~/.kapybara"
AGENT_CONFIG_BASE_EXPR = "$K_CONFIG_BASE"
_AGENT_CONFIG_BASE_MARKER = "__KAPY_AGENT_CONFIG_BASE__="


def _shell_bootstrap_prefix() -> str:
    """Return shell bootstrap that guarantees exported `K_CONFIG_BASE`.

    Every command launched through `BasicOSHelper.command()` runs behind this
    prefix, so downstream shell snippets can rely on `$K_CONFIG_BASE` directly
    instead of repeating fallback logic in each payload.
    """

    return (
        'if [ -z "${K_CONFIG_BASE:-}" ]; then '
        f"export K_CONFIG_BASE={DEFAULT_AGENT_CONFIG_BASE}; "
        "else export K_CONFIG_BASE; fi; "
        '. "$K_CONFIG_BASE/.bashrc"; '
    )


def _bash_login_command(payload: str) -> str:
    """Return a shell token that executes `payload` via `bash -lc`."""

    return single_quote(f"bash -lc {single_quote(payload)}")


def _local_script_command_prefix() -> str:
    """Return the platform-specific `script` prefix for local bash PTYs.

    BSD/macOS `script` accepts `script file command ...`, while util-linux
    expects `script file -- command ...` when the command is passed as argv
    instead of `-c`.
    """

    if sys.platform == "darwin":
        return "script -q /dev/null bash -lc "
    return "script -q /dev/null -- bash -lc "


async def agent_config_base_value(
    *,
    basic_os_helper: "BasicOSHelper",
    shell_manager: "ShellSessionManager",
) -> str:
    """Return the resolved runtime value of `AGENT_CONFIG_BASE_EXPR`.

    `BasicOSHelper.command()` now guarantees that `$K_CONFIG_BASE` is exported
    before the payload runs. Keep this helper for call sites that still need
    the concrete shell-side path instead of just the `$K_CONFIG_BASE` contract.
    """

    cmd = f'printf "{_AGENT_CONFIG_BASE_MARKER}%s\\n" "{AGENT_CONFIG_BASE_EXPR}"'
    session_id = await shell_manager.new_shell(
        basic_os_helper.command(cmd),
        desc="resolve-agent-config-base",
    )

    stdout, stderr, _ = await shell_manager.next(session_id, timeout_seconds=2)
    merged_output = (stdout + b"\n" + stderr).decode(errors="replace")
    for line in merged_output.splitlines():
        marker_idx = line.find(_AGENT_CONFIG_BASE_MARKER)
        if marker_idx >= 0:
            return line[marker_idx + len(_AGENT_CONFIG_BASE_MARKER) :].strip()

    raise RuntimeError(
        "Could not resolve runtime K_CONFIG_BASE value from shell output"
    )


def single_quote_escape(s: str) -> str:
    return s.replace("'", "'\\''")


def single_quote(s: str) -> str:
    return "'" + single_quote_escape(s) + "'"


@dataclass(slots=True)
class BasicOSHelper:
    """Build transport commands that always execute payloads inside bash."""

    config: Config

    def command_base(self) -> str:
        """Return the transport command prefix for shell execution."""

        if self.config.ssh_user is None or self.config.ssh_addr is None:
            return _local_script_command_prefix()
        return (
            "ssh -o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile=/dev/null "
            '-o LogLevel=ERROR -tt -i "'
            f'{self.config.ssh_key!s}" -p {self.config.ssh_port} '
            f"{self.config.ssh_user}@{self.config.ssh_addr} "
        )

    def command(self, command: str) -> str:
        """Build the final command including shell bootstrap.

        Exports `K_CONFIG_BASE` first, defaulting to `~/.kapybara`, and then
        sources `$K_CONFIG_BASE/.bashrc` before invoking the payload with
        `bash -lc`, so shell behavior matches the runtime view visible to the
        agent even if
        `Config.config_base` points elsewhere in the Python process.
        """

        payload = _shell_bootstrap_prefix() + command
        if self.config.ssh_user is None or self.config.ssh_addr is None:
            return self.command_base() + single_quote(payload)
        return self.command_base() + _bash_login_command(payload)


async def main():
    from k.io_helpers.shell import ShellSession, ShellSessionOptions

    config = Config()
    basic_os_helper = BasicOSHelper(config=config)
    realcommand = basic_os_helper.command("""
bash
python3
    """)
    async with ShellSession(
        realcommand, options=ShellSessionOptions(timeout_seconds=1)
    ) as shell:
        code = None

        stdout, stderr, code = await shell.next()
        while code is None:
            print(stdout.decode(), end="")
            print(stderr.decode(), end="")
            stdout, stderr, code = await shell.next((input("") + "\n").encode())
        print(stdout.decode())
        print(stderr.decode())


if __name__ == "__main__":
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv("core/.env")

    asyncio.run(main())
