//! Official Pueue v4 CLI over any Connection. The caller runs pueued and retains its logs.
//!
//! Output is delivered after completion, as merged bytes from `pueue follow`; it is not a PTY.
//! Configure Pueue's `client.read_local_logs=true` and run the CLI beside its daemon for binary
//! preservation. Pueue's remote-log protocol itself decodes text lossily. No daemon is started here.
//! Task environment overrides are applied inside the queued command, never to the Pueue client.
//! The execution target requires `env` and `/usr/bin/nice` supporting `-n 0 --`.

use std::{path::PathBuf, sync::Arc, time::Duration};

use async_trait::async_trait;
use opensol_core::{
    Command, Connection, Error, ErrorKind, EventStream, ExecRequest, ExitStatus, OutputChannel,
    ProcessCapabilities, ProcessEvent, ProcessHandle, ProcessInfo, ProcessManager, ProcessRef,
    ProcessState, Result, Signal, SpawnRequest,
};
use serde_json::Value;

use crate::util::{deadline, shell_command};

/// Native CLI selection and queue group. The SDK does not create groups or change concurrency limits.
#[derive(Clone, Debug)]
pub struct PueueConfig {
    pub program: PathBuf,
    pub config_file: Option<String>,
    pub profile: Option<String>,
    pub group: Option<String>,
    pub request_timeout: Duration,
}

impl Default for PueueConfig {
    fn default() -> Self {
        Self {
            program: "pueue".into(),
            config_file: None,
            profile: None,
            group: None,
            request_timeout: Duration::from_secs(30),
        }
    }
}

/// Stateless task adapter; process references remain valid only while Pueue retains the task.
#[derive(Clone)]
pub struct Pueue {
    connection: Arc<dyn Connection>,
    config: PueueConfig,
}

impl std::fmt::Debug for Pueue {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Pueue")
            .field("connection", &self.connection.backend())
            .field("config", &self.config)
            .finish()
    }
}

impl Pueue {
    pub fn new(connection: Arc<dyn Connection>, config: PueueConfig) -> Result<Self> {
        if config.request_timeout.is_zero() {
            return Err(Error::invalid(
                "pueue",
                "configure",
                "request timeout must be positive",
            ));
        }
        Ok(Self { connection, config })
    }

    fn command(&self, args: Vec<String>) -> Command {
        let mut global = vec!["--color".into(), "never".into()];
        if let Some(path) = &self.config.config_file {
            global.extend(["--config".into(), path.clone()]);
        }
        if let Some(profile) = &self.config.profile {
            global.extend(["--profile".into(), profile.clone()]);
        }
        global.extend(args);
        Command::new(self.config.program.to_string_lossy(), global)
    }

    async fn run(
        &self,
        args: Vec<String>,
        operation: &str,
        timeout: Option<Duration>,
    ) -> Result<Vec<u8>> {
        let result = self
            .connection
            .exec(ExecRequest {
                command: self.command(args),
                stdin: Vec::new(),
                timeout,
            })
            .await
            .map_err(|mut error| {
                error.backend = "pueue".into();
                error.operation = operation.into();
                error
            })?;
        if !result.exit.success() {
            return Err(Error::new(
                ErrorKind::Backend,
                "pueue",
                operation,
                "Pueue CLI returned a failure; inspect daemon configuration and task state",
            ));
        }
        if operation == "output" && !result.stderr.is_empty() {
            return Err(Error::new(
                ErrorKind::Backend,
                "pueue",
                operation,
                "Pueue could not read the retained task log",
            ));
        }
        Ok(result.stdout)
    }

    fn id(&self, process: &ProcessRef, operation: &str) -> Result<String> {
        process.validate("pueue", operation)?;
        if process.pty || process.id.parse::<u64>().is_err() {
            return Err(Error::invalid(
                "pueue",
                operation,
                "invalid Pueue task reference",
            ));
        }
        Ok(process.id.clone())
    }

    async fn task(&self, process: &ProcessRef) -> Result<Value> {
        let id = self.id(process, "status")?;
        let bytes = self
            .run(
                vec!["status".into(), "--json".into()],
                "status",
                Some(self.config.request_timeout),
            )
            .await?;
        let value: Value = serde_json::from_slice(&bytes).map_err(|_| protocol_error("status"))?;
        value
            .get("tasks")
            .and_then(|tasks| tasks.get(&id))
            .cloned()
            .ok_or_else(|| {
                Error::new(
                    ErrorKind::NotFound,
                    "pueue",
                    "status",
                    "task is not retained by the daemon",
                )
            })
    }

    fn task_info(&self, process: &ProcessRef, task: &Value) -> Result<ProcessInfo> {
        let status = task.get("status").ok_or_else(|| protocol_error("status"))?;
        let (native_state, payload) = if let Some(name) = status.as_str() {
            (name, None)
        } else {
            let object = status
                .as_object()
                .filter(|value| value.len() == 1)
                .ok_or_else(|| protocol_error("status"))?;
            let (name, payload) = object
                .iter()
                .next()
                .ok_or_else(|| protocol_error("status"))?;
            (name.as_str(), Some(payload))
        };
        let state = match native_state {
            "Queued" | "Stashed" | "Locked" => ProcessState::Queued,
            "Running" => ProcessState::Running,
            "Paused" => ProcessState::Paused,
            "Done" => ProcessState::Exited,
            _ => ProcessState::Unknown,
        };
        let exit = if native_state == "Done" {
            let result = payload
                .and_then(|payload| payload.get("result"))
                .ok_or_else(|| protocol_error("status"))?;
            match result {
                Value::String(value) if value == "Success" => Some(ExitStatus::code(0)),
                Value::String(value) if value == "Killed" => Some(ExitStatus {
                    code: None,
                    signal: None,
                }),
                Value::Object(value) if value.contains_key("Failed") => {
                    let code = value["Failed"]
                        .as_i64()
                        .and_then(|code| i32::try_from(code).ok())
                        .ok_or_else(|| protocol_error("status"))?;
                    Some(ExitStatus::code(code))
                }
                _ => None,
            }
        } else {
            None
        };
        Ok(ProcessInfo {
            process: process.clone(),
            state,
            exit,
            native_state: native_state.into(),
        })
    }
}

fn protocol_error(operation: &str) -> Error {
    Error::new(
        ErrorKind::Backend,
        "pueue",
        operation,
        "unexpected Pueue CLI response",
    )
}

#[async_trait]
impl ProcessManager for Pueue {
    fn backend(&self) -> &'static str {
        "pueue"
    }

    fn capabilities(&self) -> ProcessCapabilities {
        ProcessCapabilities {
            stdin: true,
            binary_stdin: false,
            supported_signals: &[Signal::Interrupt, Signal::Terminate, Signal::Kill],
            detached: true,
            reconnect: true,
            historical_output: true,
            ..Default::default()
        }
    }

    async fn spawn(&self, request: SpawnRequest) -> Result<ProcessHandle> {
        request.validate("pueue")?;
        if request.pty.is_some() {
            return Err(Error::unsupported("pueue", "spawn_pty"));
        }
        let mut args = vec!["add".into(), "--print-task-id".into()];
        if let Some(group) = &self.config.group {
            args.extend(["--group".into(), group.clone()]);
        }
        if let Some(cwd) = &request.command.cwd {
            args.extend(["--working-directory".into(), cwd.clone()]);
        }
        args.extend(["--".into(), shell_command(&request.command)]);
        let result = self
            .connection
            .exec(ExecRequest {
                command: self.command(args),
                stdin: Vec::new(),
                timeout: Some(self.config.request_timeout),
            })
            .await
            .map_err(|mut error| {
                error.backend = "pueue".into();
                error.operation = "spawn".into();
                error
            })?;
        if !result.exit.success() {
            return Err(protocol_error("spawn"));
        }
        let id = std::str::from_utf8(&result.stdout)
            .ok()
            .map(str::trim)
            .filter(|id| id.parse::<u64>().is_ok())
            .ok_or_else(|| protocol_error("spawn"))?
            .to_string();
        let process = ProcessRef::new("pueue", id, false);
        let events = self.output(&process).await?;
        Ok(ProcessHandle { process, events })
    }

    async fn status(&self, process: &ProcessRef) -> Result<ProcessInfo> {
        self.task_info(process, &self.task(process).await?)
    }

    async fn wait(&self, process: &ProcessRef, timeout: Option<Duration>) -> Result<ExitStatus> {
        let id = self.id(process, "wait")?;
        deadline(timeout, "pueue", "wait", async {
            self.status(process).await?;
            self.run(vec!["wait".into(), "--quiet".into(), id], "wait", None)
                .await?;
            self.status(process).await?.exit.ok_or_else(|| {
                Error::new(
                    ErrorKind::Backend,
                    "pueue",
                    "wait",
                    "task ended without a command exit status",
                )
            })
        })
        .await
    }

    async fn output(&self, process: &ProcessRef) -> Result<EventStream> {
        let id = self.id(process, "output")?;
        let manager = self.clone();
        let process = process.clone();
        Ok(Box::pin(async_stream::try_stream! {
            let exit = manager.wait(&process, None).await?;
            let data = manager.run(vec!["follow".into(), id], "output", None).await?;
            if !data.is_empty() { yield ProcessEvent::Output { channel: OutputChannel::Combined, data }; }
            yield ProcessEvent::Exit(exit);
        }))
    }

    async fn send_input(&self, process: &ProcessRef, data: Vec<u8>) -> Result<()> {
        let id = self.id(process, "send_input")?;
        let text =
            String::from_utf8(data).map_err(|_| Error::unsupported("pueue", "binary_stdin"))?;
        if text.contains('\0') {
            return Err(Error::unsupported("pueue", "NUL_stdin"));
        }
        self.run(
            vec!["send".into(), id, "--".into(), text],
            "send_input",
            Some(self.config.request_timeout),
        )
        .await?;
        Ok(())
    }

    async fn signal(&self, process: &ProcessRef, signal: Signal) -> Result<()> {
        let id = self.id(process, "signal")?;
        self.run(
            vec![
                "kill".into(),
                "--signal".into(),
                signal.number().to_string(),
                id,
            ],
            "signal",
            Some(self.config.request_timeout),
        )
        .await?;
        Ok(())
    }

    async fn reconnect(&self, process: &ProcessRef) -> Result<ProcessHandle> {
        self.status(process).await?;
        Ok(ProcessHandle {
            process: process.clone(),
            events: self.output(process).await?,
        })
    }
}
