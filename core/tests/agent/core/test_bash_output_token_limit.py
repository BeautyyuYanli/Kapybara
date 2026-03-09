import tiktoken

from k.agent.core.shell_tools import BashEvent


def test_bash_event_does_not_suppress_small_output() -> None:
    event = BashEvent.new(
        "123",
        (b"ok\n", b"", 0),
        all_active_sessions=[],
    )
    assert event.stdout == "ok\n"
    assert event.stderr == ""
    assert event.system_msg is None


def test_bash_event_suppresses_large_output_and_sets_system_msg() -> None:
    enc = tiktoken.get_encoding("cl100k_base")
    large_text = "a " * 20_000
    assert len(enc.encode(large_text)) > 8000

    event = BashEvent.new(
        "123",
        (large_text.encode(), b"", 0),
        all_active_sessions=[],
        system_msg="preexisting warning",
    )
    assert event.stdout == ""
    assert event.stderr == ""
    assert event.system_msg is not None
    assert event.system_msg.startswith("preexisting warning\n")
    assert "stdout/stderr is too long" in event.system_msg
