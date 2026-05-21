"""PTY sanitize helpers for shellctl output capture."""

from __future__ import annotations

import codecs
from typing import BinaryIO


class PtySanitizer:
    """Incrementally convert PTY bytes into stable, readable UTF-8 text.

    Responsibilities:
    - preserve UTF-8 decoder state across chunk boundaries
    - strip common ANSI control sequences without leaking partial escape state
    - normalize carriage-return progress updates into the final visible line

    This adapter intentionally keeps only minimal terminal state. It aims for a
    practical log representation rather than a full terminal emulator.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._line_buffer = ""
        self._pending_cr = False
        self._escape_state = "normal"

    def feed(self, raw: bytes) -> str:
        """Consume one PTY byte chunk and return newly stable text."""

        return self._consume_text(self._decoder.decode(raw, final=False), final=False)

    def flush(self) -> str:
        """Flush buffered decoder/line state at end-of-stream."""

        tail = self._consume_text(self._decoder.decode(b"", final=True), final=True)
        if self._line_buffer:
            tail += self._line_buffer
            self._line_buffer = ""
        self._pending_cr = False
        self._escape_state = "normal"
        return tail

    def _consume_text(self, text: str, *, final: bool) -> str:
        parts: list[str] = []
        for char in text:
            self._consume_char(char, parts)
        if final and self._escape_state != "normal":
            self._escape_state = "normal"
        return "".join(parts)

    def _consume_char(self, char: str, parts: list[str]) -> None:
        state = self._escape_state
        if state == "normal":
            if char == "\x1b":
                self._escape_state = "esc"
                return
            self._consume_visible_char(char, parts)
            return
        if state == "esc":
            if char == "[":
                self._escape_state = "csi"
                return
            if char == "]":
                self._escape_state = "osc"
                return
            self._escape_state = "normal"
            if char.isprintable() and char not in "\x1b":
                self._consume_visible_char(char, parts)
            return
        if state == "csi":
            if "@" <= char <= "~":
                self._escape_state = "normal"
            return
        if state == "osc":
            if char == "\x07":
                self._escape_state = "normal"
                return
            if char == "\x1b":
                self._escape_state = "osc_esc"
            return
        if state == "osc_esc":
            self._escape_state = "normal" if char == "\\" else "osc"

    def _consume_visible_char(self, char: str, parts: list[str]) -> None:
        if self._pending_cr:
            if char == "\n":
                parts.append(self._line_buffer)
                parts.append("\n")
                self._line_buffer = ""
                self._pending_cr = False
                return
            self._line_buffer = ""
            self._pending_cr = False
        if char == "\r":
            self._pending_cr = True
            return
        if char == "\n":
            parts.append(self._line_buffer)
            parts.append("\n")
            self._line_buffer = ""
            return
        self._line_buffer += char


def sanitize_pty_output(raw: bytes) -> str:
    """Sanitize a complete PTY byte string into readable UTF-8 text."""

    sanitizer = PtySanitizer()
    return sanitizer.feed(raw) + sanitizer.flush()


def sanitize_pty_stream(
    stdin: BinaryIO,
    stdout: BinaryIO,
    *,
    chunk_size: int = 65536,
) -> None:
    """Run the streaming PTY sanitizer as a Unix-style filter."""

    sanitizer = PtySanitizer()
    while True:
        chunk = stdin.read(chunk_size)
        if not chunk:
            break
        output = sanitizer.feed(chunk)
        if output:
            stdout.write(output.encode("utf-8"))
            if hasattr(stdout, "flush"):
                stdout.flush()
    tail = sanitizer.flush()
    if tail:
        stdout.write(tail.encode("utf-8"))
        if hasattr(stdout, "flush"):
            stdout.flush()
    if hasattr(stdout, "flush"):
        stdout.flush()


__all__ = ["PtySanitizer", "sanitize_pty_output", "sanitize_pty_stream"]
