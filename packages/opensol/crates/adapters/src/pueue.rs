//! Official Pueue v4 CLI over any Connection. The caller runs pueued and retains its logs.
//!
//! Output subscriptions deliver merged bytes after completion via `pueue follow`; they are not PTYs.
//! Explicit native-log configuration also enables bounded resource reads while a task runs.
//! Configure Pueue's `client.read_local_logs=true` and run the CLI beside its daemon for binary
//! preservation. Pueue's remote-log protocol itself decodes text lossily. No daemon is started here.
//! Task environment overrides are applied inside the queued command, never to the Pueue client.
//! The execution target requires `env` and `/usr/bin/nice` supporting `-n 0 --`.

use std::{path::PathBuf, time::Duration};

use async_trait::async_trait;
use opensol_core::{
    Command, Connection, Error, ErrorKind, EventStream, ExecRequest, ExitStatus, ManagerInfo,
    OutputChannel, ProcessCapabilities, ProcessEvent, ProcessHandle, ProcessInfo, ProcessRef,
    ProcessState, Result, Signal, SpawnRequest, raw::ProcessAccess,
};
use serde_json::Value;

use crate::util::{deadline, shell_command};

#[path = "pueue_output.rs"]
mod output;

/// Native CLI selection and queue group. The SDK does not create groups or change concurrency limits.
#[derive(Clone, Debug)]
pub struct PueueConfig {
    pub program: PathBuf,
    pub config_file: Option<String>,
    pub profile: Option<String>,
    pub group: Option<String>,
    /// Native daemon data directory on the injected Connection's target; never guessed locally.
    /// Enables bounded output resources; requires Linux and GNU stat/dd on that target.
    pub pueue_directory: Option<String>,
    pub request_timeout: Duration,
}

impl Default for PueueConfig {
    fn default() -> Self {
        Self {
            program: "pueue".into(),
            config_file: None,
            profile: None,
            group: None,
            pueue_directory: None,
            request_timeout: Duration::from_secs(30),
        }
    }
}

/// Stateless task adapter; process references remain valid only while Pueue retains the task.
#[derive(Clone)]
pub struct Pueue {
    connection: Connection,
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
    pub fn new(connection: Connection, config: PueueConfig) -> Result<Self> {
        if config.request_timeout.is_zero() {
            return Err(Error::invalid(
                "pueue",
                "configure",
                "request timeout must be positive",
            ));
        }
        if config
            .pueue_directory
            .as_ref()
            .is_some_and(|path| path.is_empty() || path.contains('\0'))
        {
            return Err(Error::invalid(
                "pueue",
                "configure",
                "invalid native data directory",
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
        if process.id.parse::<u64>().is_err() {
            return Err(Error::invalid(
                "pueue",
                operation,
                "invalid Pueue task reference",
            ));
        }
        Ok(process.id.clone())
    }

    async fn tasks(&self) -> Result<Value> {
        let bytes = self
            .run(
                vec!["status".into(), "--json".into()],
                "status",
                Some(self.config.request_timeout),
            )
            .await?;
        let value: Value = serde_json::from_slice(&bytes).map_err(|_| protocol_error("status"))?;
        if !value.get("tasks").is_some_and(Value::is_object) {
            return Err(protocol_error("status"));
        }
        Ok(value)
    }

    async fn task(&self, process: &ProcessRef) -> Result<Value> {
        let id = self.id(process, "status")?;
        self.tasks()
            .await?
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
        let mut native_result = None;
        let exit = if native_state == "Done" {
            let result = payload
                .and_then(|payload| payload.get("result"))
                .ok_or_else(|| protocol_error("status"))?;
            native_result = Some(match result {
                Value::String(name)
                    if matches!(
                        name.as_str(),
                        "Success" | "Killed" | "Errored" | "DependencyFailed"
                    ) =>
                {
                    name.clone()
                }
                Value::Object(object)
                    if object.len() == 1
                        && (object.contains_key("Failed")
                            || object.contains_key("FailedToSpawn")) =>
                {
                    object.keys().next().unwrap().clone()
                }
                _ => return Err(protocol_error("status")),
            });
            match result {
                Value::String(value) if value == "Success" => Some(ExitStatus::code(0)),
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
            native_result,
        })
    }

    /// Native commands with one explicit ID. Precondition checks are advisory; the daemon
    /// remains authoritative and may reject an operation racing another native client.
    async fn control(
        &self,
        process: &ProcessRef,
        operation: &str,
        args: Vec<String>,
        allowed: &[&str],
    ) -> Result<()> {
        self.id(process, operation)?;
        let before = self.task(process).await?;
        let info = self.task_info(process, &before)?;
        if !allowed.contains(&info.native_state.as_str()) {
            return Err(Error::new(
                ErrorKind::Conflict,
                "pueue",
                operation,
                "native task state does not permit this operation",
            ));
        }
        let response = self
            .run(args, operation, Some(self.config.request_timeout))
            .await?;
        // v4 single-ID acknowledgement is explicit, unlike a generic successful CLI exit.
        let text = std::str::from_utf8(&response).map_err(|_| protocol_error(operation))?;
        let prefix = match operation {
            "pause" => "Tasks have been paused",
            "start" => "Tasks have been started/resumed",
            "restart" => "Restarted tasks",
            "kill" | "signal" => "Tasks are being killed",
            "remove" => "Tasks removed from list",
            _ => return Err(protocol_error(operation)),
        };
        let expected = if operation == "restart" {
            format!("{prefix}: [{}]", process.id)
        } else {
            format!("{prefix}: {}", process.id)
        };
        if text.trim() != expected {
            return Err(protocol_error(operation));
        }
        let after = self.task(process).await;
        if operation == "remove" {
            return match after {
                Err(error) if error.kind == ErrorKind::NotFound => Ok(()),
                Err(error) => Err(error),
                Ok(_) => Err(Error::new(
                    ErrorKind::Conflict,
                    "pueue",
                    operation,
                    "native task was not removed",
                )),
            };
        }
        let after = after?;
        let state = self.task_info(process, &after)?;
        let changed = match operation {
            "pause" => matches!(state.state, ProcessState::Paused | ProcessState::Exited),
            "start" => matches!(state.state, ProcessState::Running | ProcessState::Exited),
            "restart" => before.get("status") != after.get("status"),
            // Native kill/signal acceptance need not synchronously change task state.
            "kill" | "signal" => true,
            _ => false,
        };
        if !changed {
            return Err(Error::new(
                ErrorKind::Conflict,
                "pueue",
                operation,
                "native task did not enter the requested state",
            ));
        }
        Ok(())
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
impl ProcessAccess for Pueue {
    fn backend(&self) -> &'static str {
        "pueue"
    }

    fn capabilities(&self) -> ProcessCapabilities {
        ProcessCapabilities {
            list: true,
            pause: true,
            start: true,
            restart: true,
            kill: true,
            remove: true,
            output_resource: self.config.pueue_directory.is_some(),
            stdin: true,
            binary_stdin: false,
            supported_signals: &[Signal::Interrupt, Signal::Terminate, Signal::Kill],
            detached: true,
            reconnect: true,
            historical_output: true,
            ..Default::default()
        }
    }

    async fn probe(&self) -> Result<ManagerInfo> {
        let version = self
            .run(
                vec!["--version".into()],
                "probe",
                Some(self.config.request_timeout),
            )
            .await?;
        let version = std::str::from_utf8(&version)
            .map_err(|_| protocol_error("probe"))?
            .trim();
        let client_version = version
            .strip_prefix("pueue ")
            .map(|version| version.trim_start_matches('v'))
            .ok_or_else(|| protocol_error("probe"))?;
        if !client_version.starts_with("4.") {
            return Err(Error::unsupported("pueue", "probe_cli_version"));
        }
        self.tasks().await?;
        if self.config.pueue_directory.is_some() {
            self.probe_output().await?;
        }
        Ok(ManagerInfo {
            backend: "pueue".into(),
            client_version: Some(client_version.into()),
            server_version: None,
        })
    }

    async fn list(&self) -> Result<Vec<ProcessInfo>> {
        let value = self.tasks().await?;
        value["tasks"]
            .as_object()
            .ok_or_else(|| protocol_error("list"))?
            .iter()
            .filter(|(_, task)| {
                self.config
                    .group
                    .as_ref()
                    .is_none_or(|group| task.get("group").and_then(Value::as_str) == Some(group))
            })
            .map(|(id, task)| {
                let process = ProcessRef::new("pueue", id, Some(false));
                self.id(&process, "list")?;
                self.task_info(&process, task)
            })
            .collect()
    }

    async fn output_stat(&self, process: &ProcessRef) -> Result<opensol_core::OutputInfo> {
        self.stat_output(process).await
    }
    async fn read_output(
        &self,
        process: &ProcessRef,
        cursor: Option<&opensol_core::OutputCursor>,
        limit: usize,
    ) -> Result<opensol_core::OutputPage> {
        self.read_output_page(process, cursor, limit).await
    }

    async fn pause(&self, process: &ProcessRef) -> Result<()> {
        let id = self.id(process, "pause")?;
        self.control(process, "pause", vec!["pause".into(), id], &["Running"])
            .await
    }
    async fn start(&self, process: &ProcessRef) -> Result<()> {
        let id = self.id(process, "start")?;
        self.control(
            process,
            "start",
            vec!["start".into(), id],
            &["Queued", "Stashed", "Paused"],
        )
        .await
    }
    async fn restart(&self, process: &ProcessRef) -> Result<()> {
        let id = self.id(process, "restart")?;
        self.control(
            process,
            "restart",
            vec!["restart".into(), "--in-place".into(), id],
            &["Done"],
        )
        .await
    }
    async fn kill(&self, process: &ProcessRef) -> Result<()> {
        let id = self.id(process, "kill")?;
        self.control(
            process,
            "kill",
            vec!["kill".into(), id],
            &["Running", "Paused"],
        )
        .await
    }
    async fn remove(&self, process: &ProcessRef) -> Result<()> {
        let id = self.id(process, "remove")?;
        self.control(
            process,
            "remove",
            vec!["remove".into(), id],
            &["Queued", "Stashed", "Locked", "Done"],
        )
        .await
    }

    async fn spawn(&self, request: SpawnRequest) -> Result<ProcessHandle> {
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
        let process = ProcessRef::new("pueue", id, Some(false));
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
            let info = self.status(process).await?;
            info.exit.ok_or_else(|| {
                Error::new(
                    ErrorKind::Backend,
                    "pueue",
                    "wait",
                    &format!(
                        "native result {} has no command exit status",
                        info.native_result.as_deref().unwrap_or("unknown")
                    ),
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
            String::from_utf8(data).map_err(|_| Error::unsupported("pueue", "send_input"))?;
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
        self.control(
            process,
            "signal",
            vec![
                "kill".into(),
                "--signal".into(),
                signal.number().to_string(),
                id,
            ],
            &["Running", "Paused"],
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
