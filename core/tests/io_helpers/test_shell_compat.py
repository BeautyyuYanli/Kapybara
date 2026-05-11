from shell_session_manager import ShellSessionManager

from k.io_helpers.shell import ShellSessionManager as CompatShellSessionManager


def test_shell_compat_exports_standalone_package_symbol() -> None:
    assert CompatShellSessionManager is ShellSessionManager
