"""Model observations: bounded UTF-8 excerpts and explicit, limited redaction.

Redact complete available pages before slicing so the display budget cannot
split a known secret into an unrecognized fragment. Unknown secrets and secrets
split across separate server pages are outside this best-effort filter's scope.
"""

import json
import re

from shellctl import JobResult, JobStatusView

EXCERPT_BYTES = 4096


class Redactor:
    """Execution-local token and compiled configuration; never persisted."""

    def __init__(self, patterns: list[str], token: str) -> None:
        self._patterns = tuple(re.compile(pattern) for pattern in patterns)
        self._token = token

    def __call__(self, text: str) -> str:
        if self._token:
            text = text.replace(self._token, "[REDACTED]")
        for pattern in self._patterns:
            text = pattern.sub("[REDACTED]", text)
        return text

    def error(self, error: object, job_id: str | None = None) -> str:
        value = {"error": self(str(error))}
        if job_id is not None:
            value["job_id"] = self(job_id)
        return json.dumps(value, ensure_ascii=False)


def format_output(result: JobResult, tail: JobResult | None, redact: Redactor) -> str:
    """Render up to 4 KiB head/tail each; tail may deliberately skip middle bytes."""
    raw = redact(result.output).encode("utf-8")
    if result.truncated or len(raw) > 2 * EXCERPT_BYTES:
        end = redact(tail.output).encode("utf-8") if tail is not None else raw
        output = (
            raw[:EXCERPT_BYTES].decode("utf-8", errors="ignore")
            + "\n[Output truncated; middle omitted. Full log: "
            + redact(result.output_path)
            + "]\n"
            + end[-EXCERPT_BYTES:].decode("utf-8", errors="ignore")
        )
    else:
        output = raw.decode("utf-8")
    return _render(result, output, result.output_path, redact)


def format_interrupt(result: JobStatusView, output_path: str | None, redact: Redactor) -> str:
    return _render(
        result, "Job interrupted; retained output can still be read.", output_path, redact
    )


def _render(
    result: JobResult | JobStatusView, output: str, output_path: str | None, redact: Redactor
) -> str:
    metadata: dict[str, str | bool | int | None] = {
        "job_id": redact(result.job_id),
        "status": result.status.value,
        "done": result.done,
        "exit_code": result.exit_code,
    }
    if output_path:
        metadata["output_path"] = redact(output_path)
    return (
        f"<metadata>\n{json.dumps(metadata, ensure_ascii=False)}\n</metadata>\n\n"
        f"<output>\n{output}\n</output>"
    )
