import json
import re
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from pydantic import ValidationError

from shell_session_manager.shellctl.proto.v1 import shellctl_pb2 as pb
from shell_session_manager.shellctl.shared import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_TERMINATE_GRACE_SECONDS,
    JOB_ID_ALPHABET,
    MAX_LIST_LIMIT,
    MAX_OUTPUT_LIMIT_BYTES,
    JobInfo,
    JobStatusName,
    JobStatusView,
    PtySanitizer,
    RunJobRequest,
    generate_job_id,
    read_output_window,
    sanitize_pty_output,
    sanitize_pty_stream,
    tail_output_window,
)
from shell_session_manager.shellctl.shared import (
    protobuf as proto_codec,
)


def test_generate_job_id_matches_proposal_format() -> None:
    job_id = generate_job_id(now=datetime(2026, 5, 21, 15, 30, tzinfo=UTC))

    assert re.fullmatch(r"05211530-[0-9abcdefghjkmnpqrstvwxyz]{3}", job_id)
    assert all(char in f"{JOB_ID_ALPHABET}-" for char in job_id[9:])


def test_sanitize_pty_golden_cases() -> None:
    fixture_path = Path(__file__).with_name("golden_shellctl_sanitize.json")
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))

    for case in cases:
        chunks = [bytes.fromhex(chunk) for chunk in case["chunks_hex"]]
        sanitizer = PtySanitizer()
        streamed = (
            "".join(sanitizer.feed(chunk) for chunk in chunks) + sanitizer.flush()
        )
        batch = sanitize_pty_output(b"".join(chunks))

        assert streamed == case["expected"], case["name"]
        assert batch == case["expected"], case["name"]


def test_read_output_window_preserves_utf8_boundaries(tmp_path: Path) -> None:
    output_path = tmp_path / "output.log"
    output_path.write_text("A🙂B", encoding="utf-8")

    first = read_output_window(output_path, offset=0, limit=3)
    second = read_output_window(output_path, offset=first.offset, limit=4)
    third = read_output_window(output_path, offset=second.offset, limit=8)

    assert first.output == "A"
    assert first.offset == 1
    assert first.truncated is True
    assert second.output == "🙂"
    assert second.offset == 5
    assert second.truncated is True
    assert third.output == "B"
    assert third.offset == output_path.stat().st_size
    assert third.truncated is False


def test_read_output_window_advances_past_wide_char_even_when_limit_is_smaller(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "output.log"
    output_path.write_text("🙂B", encoding="utf-8")

    first = read_output_window(output_path, offset=0, limit=1)
    second = read_output_window(output_path, offset=first.offset, limit=1)

    assert first.output == "🙂"
    assert first.offset == len("🙂".encode())
    assert first.truncated is True
    assert second.output == "B"
    assert second.truncated is False


def test_tail_output_window_skips_partial_utf8_prefix(tmp_path: Path) -> None:
    output_path = tmp_path / "output.log"
    output_path.write_text("a🙂b", encoding="utf-8")

    tail = tail_output_window(output_path, limit=3)

    assert tail.output == "b"
    assert tail.offset == output_path.stat().st_size
    assert tail.truncated is False


def test_sanitize_pty_stream_flushes_incrementally() -> None:
    class ChunkedInput:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks

        def read(self, _size: int) -> bytes:
            if not self._chunks:
                return b""
            return self._chunks.pop(0)

    class FlushCountingOutput(BytesIO):
        def __init__(self) -> None:
            super().__init__()
            self.flush_count = 0

        def flush(self) -> None:
            self.flush_count += 1
            super().flush()

    stdout = FlushCountingOutput()
    sanitize_pty_stream(
        ChunkedInput([b"ready\n", b"next\n"]),
        stdout,
        chunk_size=5,
    )

    assert stdout.getvalue() == b"ready\nnext\n"
    assert stdout.flush_count >= 2


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"": "x"}, "non-empty"),
        ({"A=B": "x"}, "must not contain '='"),
        ({"A\x00B": "x"}, "must not contain NUL"),
        ({"A": "x\x00y"}, "must not contain NUL"),
    ],
)
def test_run_job_request_rejects_invalid_env_entries(
    env: dict[str, str], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        RunJobRequest(script="printf ready\n", env=env)


def test_protobuf_job_status_round_trip() -> None:
    status = proto_codec.job_status_from_protobuf(
        proto_codec.job_status_to_protobuf(JobStatusName.RUNNING)
    )

    assert status is JobStatusName.RUNNING


def test_protobuf_optional_fields_round_trip() -> None:
    view = JobStatusView(
        job_id="job-1",
        status=JobStatusName.EXITED,
        done=True,
        exit_code=7,
        created_at="2026-05-21T15:30:12Z",
        started_at="2026-05-21T15:30:13Z",
        ended_at="2026-05-21T15:30:14Z",
        offset=9,
    )
    info = JobInfo(
        job_id="job-1",
        status=JobStatusName.EXITED,
        created_at="2026-05-21T15:30:12Z",
        started_at="2026-05-21T15:30:13Z",
        ended_at="2026-05-21T15:30:14Z",
    )

    encoded_view = proto_codec.job_status_view_to_protobuf(view)
    encoded_info = proto_codec.job_info_to_protobuf(info)

    assert encoded_view.HasField("exit_code")
    assert encoded_view.HasField("started_at")
    assert encoded_view.HasField("ended_at")
    assert encoded_info.HasField("started_at")
    assert encoded_info.HasField("ended_at")
    assert proto_codec.job_status_view_from_protobuf(encoded_view) == view
    assert proto_codec.job_info_from_protobuf(encoded_info) == info


def test_protobuf_run_request_preserves_explicit_empty_optional_string() -> None:
    message = pb.RunJobRequest(script="printf ready\n")
    message.cwd = ""

    decoded = proto_codec.run_job_request_from_protobuf(message)

    assert decoded.cwd == ""


def test_protobuf_run_request_keeps_env_validation_in_pydantic() -> None:
    message = pb.RunJobRequest(script="printf ready\n")
    message.env[""] = "bad"

    with pytest.raises(ValidationError, match="non-empty"):
        proto_codec.run_job_request_from_protobuf(message)


def test_protobuf_list_jobs_request_uses_unspecified_as_none_filter() -> None:
    status, limit = proto_codec.list_jobs_request_from_protobuf(
        pb.ListJobsRequest(status=pb.JOB_STATUS_UNSPECIFIED)
    )

    assert status is None
    assert limit == DEFAULT_LIST_LIMIT


def test_protobuf_list_jobs_request_rejects_out_of_range_limit() -> None:
    with pytest.raises(ValueError, match="limit"):
        proto_codec.list_jobs_request_from_protobuf(
            pb.ListJobsRequest(status=pb.JOB_STATUS_UNSPECIFIED, limit=0)
        )

    with pytest.raises(ValueError, match="limit"):
        proto_codec.list_jobs_request_from_protobuf(
            pb.ListJobsRequest(
                status=pb.JOB_STATUS_UNSPECIFIED,
                limit=MAX_LIST_LIMIT + 1,
            )
        )


def test_protobuf_tail_job_request_rejects_out_of_range_output_limit() -> None:
    with pytest.raises(ValueError, match="output_limit"):
        proto_codec.tail_job_request_from_protobuf(
            pb.TailJobRequest(job_id="job-1", output_limit=0)
        )

    with pytest.raises(ValueError, match="output_limit"):
        proto_codec.tail_job_request_from_protobuf(
            pb.TailJobRequest(
                job_id="job-1",
                output_limit=MAX_OUTPUT_LIMIT_BYTES + 1,
            )
        )


def test_protobuf_delete_request_uses_default_grace_when_absent() -> None:
    job_id, force, grace_seconds = proto_codec.delete_job_request_from_protobuf(
        pb.DeleteJobRequest(job_id="job-1", force=True)
    )

    assert job_id == "job-1"
    assert force is True
    assert grace_seconds == DEFAULT_TERMINATE_GRACE_SECONDS
