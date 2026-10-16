//! Local completed commands, without sandbox isolation. Unix process groups are killed on cancellation.

use std::{io::ErrorKind as IoErrorKind, process::Stdio};

use async_trait::async_trait;
use opensol_core::{
    Connection, ConnectionCapabilities, Error, ErrorKind, ExecRequest, ExecResult, ExitStatus,
    Result,
};
use tokio::{io::AsyncWriteExt, process::Command as TokioCommand};

use crate::util::deadline;

/// Executes argv locally, inheriting the parent environment with the command's explicit overlay.
#[derive(Clone, Copy, Debug, Default)]
pub struct Local;

#[cfg(unix)]
struct ProcessGroup(nix::unistd::Pid);

#[cfg(unix)]
impl Drop for ProcessGroup {
    fn drop(&mut self) {
        let _ = nix::sys::signal::killpg(self.0, nix::sys::signal::Signal::SIGKILL);
    }
}

fn io_error(error: std::io::Error) -> Error {
    let kind = match error.kind() {
        IoErrorKind::NotFound => ErrorKind::NotFound,
        IoErrorKind::PermissionDenied => ErrorKind::Permission,
        _ => ErrorKind::Transport,
    };
    Error::new(kind, "local", "exec", "local process I/O failed")
}

#[async_trait]
impl Connection for Local {
    fn backend(&self) -> &'static str {
        "local"
    }

    fn capabilities(&self) -> ConnectionCapabilities {
        ConnectionCapabilities {
            stdin: true,
            timeout: true,
            separate_stderr: true,
        }
    }

    async fn exec(&self, request: ExecRequest) -> Result<ExecResult> {
        request.validate("local")?;
        deadline(request.timeout, "local", "exec", async move {
            let mut command = TokioCommand::new(&request.command.argv[0]);
            command
                .args(&request.command.argv[1..])
                .envs(&request.command.env)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .kill_on_drop(true);
            if let Some(cwd) = &request.command.cwd {
                command.current_dir(cwd);
            }
            #[cfg(unix)]
            command.process_group(0);
            let mut child = command.spawn().map_err(io_error)?;
            #[cfg(unix)]
            let _group = ProcessGroup(nix::unistd::Pid::from_raw(child.id().ok_or_else(|| {
                Error::new(
                    ErrorKind::Backend,
                    "local",
                    "exec",
                    "spawn returned no process id",
                )
            })? as i32));
            let stdin = child.stdin.take();
            let write = async move {
                if let Some(mut stdin) = stdin {
                    match stdin.write_all(&request.stdin).await {
                        Ok(()) => {}
                        Err(error) if error.kind() == IoErrorKind::BrokenPipe => {}
                        Err(error) => return Err(io_error(error)),
                    }
                }
                Ok(())
            };
            let (_, output) = tokio::try_join!(write, async {
                child.wait_with_output().await.map_err(io_error)
            })?;
            #[cfg(unix)]
            let signal = {
                use std::os::unix::process::ExitStatusExt;
                output.status.signal()
            };
            #[cfg(not(unix))]
            let signal = None;
            Ok(ExecResult {
                stdout: output.stdout,
                stderr: output.stderr,
                exit: ExitStatus {
                    code: output.status.code(),
                    signal,
                },
            })
        })
        .await
    }
}
