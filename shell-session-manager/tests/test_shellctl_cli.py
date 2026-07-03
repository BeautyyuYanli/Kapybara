from __future__ import annotations

import importlib
import json
import os
import shutil
from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from shell_session_manager.shellctl.server import (
    ShellctlConfig,
    ShellctlServerError,
    cli,
)
from shell_session_manager.shellctl.shared import (
    DeleteJobResponse,
    InputJobRequest,
    JobResult,
    JobStatusName,
    JobStatusView,
    ListJobsResponse,
    RunJobRequest,
    TerminateJobRequest,
    WaitJobRequest,
)

cli_controller_module = importlib.import_module(
    "shell_session_manager.shellctl.server.cli_controller"
)

runner = CliRunner()


class RecordingShellctlService:
    created_configs: ClassVar[list[ShellctlConfig]] = []
    calls: ClassVar[list[tuple[str, tuple[object, ...], dict[str, object]]]] = []
    results: ClassVar[dict[str, object]] = {}
    error: ClassVar[ShellctlServerError | None] = None
    prepare_runtime_calls: ClassVar[int] = 0
    initialize_calls: ClassVar[int] = 0
    reconcile_calls: ClassVar[int] = 0
    gc_once_calls: ClassVar[int] = 0
    shutdown_calls: ClassVar[int] = 0

    def __init__(self, config: ShellctlConfig) -> None:
        self.config = config
        type(self).created_configs.append(config)

    @classmethod
    def reset(cls) -> None:
        cls.created_configs = []
        cls.calls = []
        cls.results = {}
        cls.error = None
        cls.prepare_runtime_calls = 0
        cls.initialize_calls = 0
        cls.reconcile_calls = 0
        cls.gc_once_calls = 0
        cls.shutdown_calls = 0

    def _result(self, method: str) -> object:
        if type(self).error is not None:
            raise type(self).error
        return type(self).results[method]

    async def prepare_runtime(self) -> None:
        type(self).prepare_runtime_calls += 1

    async def initialize(self) -> None:
        type(self).initialize_calls += 1

    async def reconcile(self) -> None:
        type(self).reconcile_calls += 1

    async def gc_once(self) -> None:
        type(self).gc_once_calls += 1

    async def shutdown(self) -> None:
        type(self).shutdown_calls += 1

    async def run_job(self, request: RunJobRequest) -> JobResult:
        type(self).calls.append(("run_job", (request,), {}))
        return self._result("run_job")  # type: ignore[return-value]

    async def wait_job(self, job_id: str, request: WaitJobRequest) -> JobResult:
        type(self).calls.append(("wait_job", (job_id, request), {}))
        return self._result("wait_job")  # type: ignore[return-value]

    async def get_job_status(self, job_id: str) -> JobStatusView:
        type(self).calls.append(("get_job_status", (job_id,), {}))
        return self._result("get_job_status")  # type: ignore[return-value]

    async def list_jobs(
        self,
        *,
        status: JobStatusName | None = None,
        limit: int,
    ) -> ListJobsResponse:
        type(self).calls.append(("list_jobs", (), {"status": status, "limit": limit}))
        return self._result("list_jobs")  # type: ignore[return-value]

    async def send_input(self, job_id: str, request: InputJobRequest) -> JobResult:
        type(self).calls.append(("send_input", (job_id, request), {}))
        return self._result("send_input")  # type: ignore[return-value]

    async def tail_job(self, job_id: str, *, output_limit: int) -> JobResult:
        type(self).calls.append(("tail_job", (job_id,), {"output_limit": output_limit}))
        return self._result("tail_job")  # type: ignore[return-value]

    async def terminate_job(
        self,
        job_id: str,
        request: TerminateJobRequest,
    ) -> JobStatusView:
        type(self).calls.append(("terminate_job", (job_id, request), {}))
        return self._result("terminate_job")  # type: ignore[return-value]

    async def delete_job(
        self,
        job_id: str,
        *,
        force: bool,
        grace_seconds: float,
    ) -> DeleteJobResponse:
        type(self).calls.append(
            (
                "delete_job",
                (job_id,),
                {"force": force, "grace_seconds": grace_seconds},
            )
        )
        return self._result("delete_job")  # type: ignore[return-value]


@pytest.fixture
def patched_service(monkeypatch: pytest.MonkeyPatch) -> type[RecordingShellctlService]:
    RecordingShellctlService.reset()
    monkeypatch.setattr(
        cli_controller_module,
        "ShellctlService",
        RecordingShellctlService,
    )
    return RecordingShellctlService


def test_shellctl_help_lists_direct_controller_commands() -> None:
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.stderr
    assert "health" in result.stdout
    assert "run" in result.stdout
    assert "wait" in result.stdout
    assert "status" in result.stdout
    assert "list" in result.stdout
    assert "input" in result.stdout
    assert "tail" in result.stdout
    assert "terminate" in result.stdout
    assert "delete" in result.stdout


def test_shellctl_run_help_shows_direct_options() -> None:
    result = runner.invoke(cli, ["run", "--help"])

    assert result.exit_code == 0, result.stderr
    assert "--cwd" in result.stdout
    assert "--env" in result.stdout
    assert "--timeout" in result.stdout
    assert "--output-limit" in result.stdout
    assert "--idle-flush-seconds" in result.stdout
    assert "--cols" in result.stdout
    assert "--rows" in result.stdout
    assert "--state-dir" in result.stdout
    assert "--runtime-dir" in result.stdout
    assert "--auth-token" not in result.stdout


def test_shellctl_health_uses_prepare_runtime_and_ignores_http_auth_env(
    monkeypatch: pytest.MonkeyPatch,
    patched_service: type[RecordingShellctlService],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SHELLCTL_AUTH_TOKEN", "from-http-only-env")

    result = runner.invoke(
        cli,
        [
            "health",
            "--state-dir",
            str(tmp_path / "state"),
            "--runtime-dir",
            str(tmp_path / "run"),
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "ok"}
    assert result.stderr == ""
    assert patched_service.prepare_runtime_calls == 1
    assert patched_service.initialize_calls == 0
    assert patched_service.reconcile_calls == 0
    assert patched_service.gc_once_calls == 0
    assert patched_service.shutdown_calls == 1
    assert len(patched_service.created_configs) == 1
    config = patched_service.created_configs[0]
    assert config.state_dir == tmp_path / "state"
    assert config.runtime_dir == tmp_path / "run"
    assert config.auth_token is None


def test_shellctl_run_builds_request_and_emits_json(
    patched_service: type[RecordingShellctlService],
    tmp_path: Path,
) -> None:
    patched_service.results["run_job"] = JobResult(
        job_id="job-run",
        done=False,
        status=JobStatusName.RUNNING,
        output_path="/tmp/output.log",
        output="hello\n",
        offset=6,
        truncated=False,
    )

    result = runner.invoke(
        cli,
        [
            "run",
            "printf hello\\n",
            "--cwd",
            str(tmp_path / "workspace"),
            "--env",
            "A=1",
            "--env",
            "EMPTY=",
            "--timeout",
            "12",
            "--output-limit",
            "4096",
            "--idle-flush-seconds",
            "0.25",
            "--cols",
            "90",
            "--state-dir",
            str(tmp_path / "state"),
            "--runtime-dir",
            str(tmp_path / "run"),
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "job_id": "job-run",
        "done": False,
        "status": "running",
        "output_path": "/tmp/output.log",
        "output": "hello\n",
        "offset": 6,
        "truncated": False,
    }
    assert patched_service.prepare_runtime_calls == 1
    assert patched_service.initialize_calls == 0
    assert patched_service.reconcile_calls == 0
    assert patched_service.gc_once_calls == 0
    assert patched_service.shutdown_calls == 1

    call_name, args, kwargs = patched_service.calls[0]
    assert call_name == "run_job"
    assert kwargs == {}
    request = args[0]
    assert isinstance(request, RunJobRequest)
    assert request.script == "printf hello\\n"
    assert request.cwd == str(tmp_path / "workspace")
    assert request.env == {"A": "1", "EMPTY": ""}
    assert request.timeout == 12.0
    assert request.output_limit == 4096
    assert request.idle_flush_seconds == 0.25
    assert request.terminal is not None
    assert request.terminal.cols == 90
    assert request.terminal.rows == 80


def test_shellctl_wait_and_input_require_offset() -> None:
    wait_result = runner.invoke(cli, ["wait", "job-1"])
    input_result = runner.invoke(cli, ["input", "job-1", "hello"])

    assert wait_result.exit_code == 2
    assert "Missing option '--offset'" in wait_result.stderr
    assert input_result.exit_code == 2
    assert "Missing option '--offset'" in input_result.stderr


def test_shellctl_wait_and_input_map_requests(
    patched_service: type[RecordingShellctlService],
) -> None:
    patched_service.results["wait_job"] = JobResult(
        job_id="job-1",
        done=False,
        status=JobStatusName.RUNNING,
        output_path="/tmp/wait.log",
        output="chunk",
        offset=5,
        truncated=False,
    )
    wait_result = runner.invoke(
        cli,
        [
            "wait",
            "job-1",
            "--offset",
            "3",
            "--timeout",
            "9",
            "--output-limit",
            "2048",
            "--idle-flush-seconds",
            "0.1",
        ],
    )

    assert wait_result.exit_code == 0, wait_result.stderr
    wait_call = patched_service.calls[0]
    assert wait_call[0] == "wait_job"
    assert wait_call[1][0] == "job-1"
    wait_request = wait_call[1][1]
    assert isinstance(wait_request, WaitJobRequest)
    assert wait_request.offset == 3
    assert wait_request.timeout == 9.0
    assert wait_request.output_limit == 2048
    assert wait_request.idle_flush_seconds == 0.1

    RecordingShellctlService.reset()
    patched_service.results["send_input"] = JobResult(
        job_id="job-1",
        done=False,
        status=JobStatusName.RUNNING,
        output_path="/tmp/input.log",
        output="reply",
        offset=8,
        truncated=False,
    )
    input_result = runner.invoke(
        cli,
        [
            "input",
            "job-1",
            "hello\n",
            "--offset",
            "5",
            "--timeout",
            "4",
            "--output-limit",
            "512",
            "--idle-flush-seconds",
            "0",
        ],
    )

    assert input_result.exit_code == 0, input_result.stderr
    input_call = patched_service.calls[0]
    assert input_call[0] == "send_input"
    assert input_call[1][0] == "job-1"
    input_request = input_call[1][1]
    assert isinstance(input_request, InputJobRequest)
    assert input_request.text == "hello\n"
    assert input_request.offset == 5
    assert input_request.timeout == 4.0
    assert input_request.output_limit == 512
    assert input_request.idle_flush_seconds == 0.0


def test_shellctl_list_tail_status_terminate_and_delete_map_arguments(
    patched_service: type[RecordingShellctlService],
) -> None:
    patched_service.results["list_jobs"] = ListJobsResponse(
        jobs=[
            {
                "job_id": "job-2",
                "status": "running",
                "created_at": "2026-05-21T15:30:12Z",
            }
        ]
    )
    list_result = runner.invoke(cli, ["list", "--status", "running", "--limit", "5"])

    assert list_result.exit_code == 0, list_result.stderr
    assert json.loads(list_result.stdout) == {
        "jobs": [
            {
                "job_id": "job-2",
                "status": "running",
                "created_at": "2026-05-21T15:30:12Z",
            }
        ]
    }
    assert patched_service.calls[0] == (
        "list_jobs",
        (),
        {"status": JobStatusName.RUNNING, "limit": 5},
    )

    RecordingShellctlService.reset()
    patched_service.results["tail_job"] = JobResult(
        job_id="job-2",
        done=False,
        status=JobStatusName.RUNNING,
        output_path="/tmp/tail.log",
        output="tail",
        offset=4,
        truncated=False,
    )
    tail_result = runner.invoke(cli, ["tail", "job-2", "--output-limit", "16"])
    assert tail_result.exit_code == 0, tail_result.stderr
    assert patched_service.calls[0] == (
        "tail_job",
        ("job-2",),
        {"output_limit": 16},
    )

    RecordingShellctlService.reset()
    patched_service.results["get_job_status"] = JobStatusView(
        job_id="job-2",
        status=JobStatusName.RUNNING,
        done=False,
        created_at="2026-05-21T15:30:12Z",
        started_at="2026-05-21T15:30:13Z",
        offset=4,
    )
    status_result = runner.invoke(cli, ["status", "job-2"])
    assert status_result.exit_code == 0, status_result.stderr
    assert patched_service.calls[0] == ("get_job_status", ("job-2",), {})

    RecordingShellctlService.reset()
    patched_service.results["terminate_job"] = JobStatusView(
        job_id="job-2",
        status=JobStatusName.TERMINATED,
        done=True,
        created_at="2026-05-21T15:30:12Z",
        started_at="2026-05-21T15:30:13Z",
        ended_at="2026-05-21T15:30:18Z",
        offset=4,
    )
    terminate_result = runner.invoke(
        cli,
        ["terminate", "job-2", "--grace-seconds", "0.25"],
    )
    assert terminate_result.exit_code == 0, terminate_result.stderr
    terminate_call = patched_service.calls[0]
    assert terminate_call[0] == "terminate_job"
    assert terminate_call[1][0] == "job-2"
    terminate_request = terminate_call[1][1]
    assert isinstance(terminate_request, TerminateJobRequest)
    assert terminate_request.grace_seconds == 0.25

    RecordingShellctlService.reset()
    patched_service.results["delete_job"] = DeleteJobResponse(job_id="job-2")
    delete_result = runner.invoke(
        cli,
        ["delete", "job-2", "--force", "--grace-seconds", "0.5"],
    )
    assert delete_result.exit_code == 0, delete_result.stderr
    assert patched_service.calls[0] == (
        "delete_job",
        ("job-2",),
        {"force": True, "grace_seconds": 0.5},
    )


def test_shellctl_run_rejects_invalid_env_entry() -> None:
    result = runner.invoke(cli, ["run", "printf bad", "--env", "MISSING_EQUALS"])

    assert result.exit_code == 2
    assert "env entries must use NAME=VALUE format" in result.stderr


def test_shellctl_direct_commands_render_server_errors_on_stderr(
    patched_service: type[RecordingShellctlService],
) -> None:
    patched_service.error = ShellctlServerError(
        404,
        "job_not_found",
        "Job missing is not found",
    )

    result = runner.invoke(cli, ["status", "missing"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "job_not_found: Job missing is not found\n"


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_shellctl_run_and_delete_work_without_shellctl_serve(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[1]
    src_path = package_root / "src"
    env = dict(os.environ)
    current_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{src_path}{os.pathsep}{current_pythonpath}"
        if current_pythonpath
        else str(src_path)
    )

    run_result = runner.invoke(
        cli,
        [
            "run",
            "echo Hello World",
            "--state-dir",
            str(tmp_path / "state"),
            "--runtime-dir",
            str(tmp_path / "run"),
        ],
        env=env,
    )

    assert run_result.exit_code == 0, run_result.stderr
    payload = json.loads(run_result.stdout)
    assert payload["output"] == "Hello World\n"

    delete_result = runner.invoke(
        cli,
        [
            "delete",
            payload["job_id"],
            "--state-dir",
            str(tmp_path / "state"),
            "--runtime-dir",
            str(tmp_path / "run"),
        ],
        env=env,
    )

    assert delete_result.exit_code == 0, delete_result.stderr
    assert json.loads(delete_result.stdout) == {
        "job_id": payload["job_id"],
        "deleted": True,
    }
