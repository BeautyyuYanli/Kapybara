//! Independent contracts for resource lifecycle, completed commands, and managed processes.
//!
//! Dropping clients never destroys environments. References contain identity, not credentials.
//! A transport failure or cancelled call does not imply that a remote side effect was undone.

use std::{collections::BTreeMap, fmt, pin::Pin, sync::Arc, time::Duration};

use async_trait::async_trait;
use futures::{Stream, StreamExt};
use serde::{Deserialize, Serialize};

/// Stable error categories; nonzero command exit codes are successful SDK results.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    InvalidArgument,
    Unsupported,
    NotFound,
    Conflict,
    Permission,
    Timeout,
    Transport,
    Backend,
}

/// Contextual, credential-safe error. Adapters intentionally omit raw provider response bodies.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Error {
    pub kind: ErrorKind,
    pub backend: String,
    pub operation: String,
    pub message: String,
}

impl Error {
    pub fn new(kind: ErrorKind, backend: &str, operation: &str, message: &str) -> Self {
        Self {
            kind,
            backend: backend.into(),
            operation: operation.into(),
            message: message.into(),
        }
    }

    pub fn unsupported(backend: &str, operation: &str) -> Self {
        Self::new(
            ErrorKind::Unsupported,
            backend,
            operation,
            "operation is not supported",
        )
    }

    pub fn invalid(backend: &str, operation: &str, message: &str) -> Self {
        Self::new(ErrorKind::InvalidArgument, backend, operation, message)
    }
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} {}: {} ({:?})",
            self.backend, self.operation, self.message, self.kind
        )
    }
}

impl std::error::Error for Error {}

pub type Result<Value> = std::result::Result<Value, Error>;

/// Opaque resource identity. Its meaning is scoped to the configured provider account/namespace.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ResourceRef {
    pub backend: String,
    pub id: String,
}

impl ResourceRef {
    pub fn new(backend: &str, id: impl Into<String>) -> Self {
        Self {
            backend: backend.into(),
            id: id.into(),
        }
    }

    pub fn validate(&self, backend: &str, operation: &str) -> Result<()> {
        if self.backend != backend || self.id.is_empty() || self.id.contains('\0') {
            return Err(Error::invalid(
                backend,
                operation,
                "invalid resource reference",
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResourceState {
    Pending,
    Running,
    Stopped,
    Completed,
    Failed,
    Unknown,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ResourceInfo {
    pub resource: ResourceRef,
    pub state: ResourceState,
    pub native_state: String,
}

/// Common creation metadata. Provider images, templates and policies belong to adapter configs.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct CreateOptions {
    pub name: Option<String>,
    pub labels: BTreeMap<String, String>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize)]
pub struct LifecycleCapabilities {
    pub create: bool,
    pub inspect: bool,
    pub start: bool,
    pub stop: bool,
    /// A successful native stop preserves process memory for a later start.
    pub stop_preserves_memory: bool,
    pub delete: bool,
    pub fork: bool,
}

impl LifecycleCapabilities {
    pub const BASIC: Self = Self {
        create: true,
        inspect: true,
        start: true,
        stop: true,
        stop_preserves_memory: false,
        delete: true,
        fork: false,
    };
}

/// Explicit resource operations. Start/stop request native behavior, not uniform memory snapshots.
#[async_trait]
pub trait Lifecycle: Send + Sync {
    fn backend(&self) -> &'static str;
    fn capabilities(&self) -> LifecycleCapabilities;
    async fn create(&self, options: CreateOptions) -> Result<ResourceRef>;
    async fn inspect(&self, resource: &ResourceRef) -> Result<ResourceInfo>;
    async fn start(&self, resource: &ResourceRef) -> Result<()>;
    async fn stop(&self, resource: &ResourceRef) -> Result<()>;
    async fn delete(&self, resource: &ResourceRef) -> Result<()>;
    async fn fork(&self, _resource: &ResourceRef) -> Result<ResourceRef> {
        Err(Error::unsupported(self.backend(), "fork"))
    }
}

/// An explicit argv, environment overlay, and optional working directory.
///
/// `shell` is the only implicit shell constructor. Debug omits arguments and environment values.
#[derive(Clone, Eq, PartialEq)]
pub struct Command {
    pub argv: Vec<String>,
    pub cwd: Option<String>,
    pub env: BTreeMap<String, String>,
}

impl Command {
    pub fn new(
        program: impl Into<String>,
        args: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        let mut argv = vec![program.into()];
        argv.extend(args.into_iter().map(Into::into));
        Self {
            argv,
            cwd: None,
            env: BTreeMap::new(),
        }
    }

    pub fn shell(script: impl Into<String>) -> Self {
        Self::new("/bin/sh", ["-c".into(), script.into()])
    }

    pub fn validate(&self, backend: &str, operation: &str) -> Result<()> {
        if self.argv.is_empty()
            || self.argv[0].is_empty()
            || self.argv.iter().any(|arg| arg.contains('\0'))
        {
            return Err(Error::invalid(
                backend,
                operation,
                "argv must contain a program and no NUL bytes",
            ));
        }
        if self
            .cwd
            .as_ref()
            .is_some_and(|cwd| cwd.is_empty() || cwd.contains('\0'))
        {
            return Err(Error::invalid(
                backend,
                operation,
                "invalid working directory",
            ));
        }
        if self
            .env
            .iter()
            .any(|(key, value)| key.is_empty() || key.contains(['=', '\0']) || value.contains('\0'))
        {
            return Err(Error::invalid(
                backend,
                operation,
                "invalid environment entry",
            ));
        }
        Ok(())
    }
}

impl fmt::Debug for Command {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Command")
            .field("argv", &"[redacted]")
            .field("cwd", &self.cwd)
            .field("env", &"[redacted]")
            .finish()
    }
}

/// Waits for completion. `None` means no SDK deadline; zero durations are invalid.
#[derive(Clone)]
pub struct ExecRequest {
    pub command: Command,
    pub stdin: Vec<u8>,
    pub timeout: Option<Duration>,
}

impl ExecRequest {
    pub fn new(command: Command) -> Self {
        Self {
            command,
            stdin: Vec::new(),
            timeout: None,
        }
    }

    pub fn validate(&self, backend: &str) -> Result<()> {
        self.command.validate(backend, "exec")?;
        validate_timeout(self.timeout, backend, "exec")
    }
}

impl fmt::Debug for ExecRequest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ExecRequest")
            .field("command", &self.command)
            .field("stdin_bytes", &self.stdin.len())
            .field("timeout", &self.timeout)
            .finish()
    }
}

/// A signal-terminated local command has no numeric exit code; providers may encode signals as codes.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ExitStatus {
    pub code: Option<i32>,
    pub signal: Option<i32>,
}

impl ExitStatus {
    pub fn code(code: i32) -> Self {
        Self {
            code: Some(code),
            signal: None,
        }
    }

    pub fn success(self) -> bool {
        self.code == Some(0) && self.signal.is_none()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExecResult {
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub exit: ExitStatus,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct ConnectionCapabilities {
    pub stdin: bool,
    pub timeout: bool,
    pub separate_stderr: bool,
}

/// Completed-command interface bound to one execution target. Calls are never retried implicitly.
#[async_trait]
pub trait Connection: Send + Sync {
    fn backend(&self) -> &'static str;
    fn capabilities(&self) -> ConnectionCapabilities;
    async fn exec(&self, request: ExecRequest) -> Result<ExecResult>;
}

/// Process identity is opaque and scoped to the manager's target, not necessarily an OS PID.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ProcessRef {
    pub backend: String,
    pub id: String,
    pub pty: bool,
}

impl ProcessRef {
    pub fn new(backend: &str, id: impl Into<String>, pty: bool) -> Self {
        Self {
            backend: backend.into(),
            id: id.into(),
            pty,
        }
    }

    pub fn validate(&self, backend: &str, operation: &str) -> Result<()> {
        if self.backend != backend || self.id.is_empty() || self.id.contains('\0') {
            return Err(Error::invalid(
                backend,
                operation,
                "invalid process reference",
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct PtySize {
    pub cols: u32,
    pub rows: u32,
}

impl PtySize {
    pub fn validate(self, backend: &str, operation: &str) -> Result<()> {
        if self.cols == 0 || self.rows == 0 {
            return Err(Error::invalid(
                backend,
                operation,
                "PTY dimensions must be positive",
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Debug)]
pub struct SpawnRequest {
    pub command: Command,
    pub stdin: bool,
    pub pty: Option<PtySize>,
}

impl SpawnRequest {
    pub fn new(command: Command) -> Self {
        Self {
            command,
            stdin: false,
            pty: None,
        }
    }

    pub fn validate(&self, backend: &str) -> Result<()> {
        self.command.validate(backend, "spawn")?;
        if let Some(size) = self.pty {
            size.validate(backend, "spawn")?;
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProcessState {
    Queued,
    Running,
    Paused,
    Exited,
    Unknown,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ProcessInfo {
    pub process: ProcessRef,
    pub state: ProcessState,
    pub exit: Option<ExitStatus>,
    pub native_state: String,
    /// Native result name only, never the provider's error body or task environment.
    pub native_result: Option<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OutputChannel {
    Stdout,
    Stderr,
    Terminal,
    Combined,
}

/// Retained native output metadata. A missing version means this execution has not started.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct OutputInfo {
    pub channels: Vec<OutputChannel>,
    pub size: u64,
    pub version: Option<String>,
}

/// Persistable opaque position, scoped to one manager target, task and log version.
/// Adapters validate their encoding; callers must not interpret or modify it.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct OutputCursor(pub String);

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct OutputChunk {
    pub channel: OutputChannel,
    pub data: Vec<u8>,
}

/// `at_end` is the observed log tail, not process completion. Reads never wait for new output.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct OutputPage {
    pub chunks: Vec<OutputChunk>,
    pub next_cursor: OutputCursor,
    pub at_end: bool,
}

pub const DEFAULT_OUTPUT_LIMIT: usize = 65_536;
pub const MAX_OUTPUT_LIMIT: usize = 1_048_576;

/// Enforce a bounded read before doing any target I/O.
pub fn validate_output_limit(limit: usize, backend: &str) -> Result<()> {
    if !(1..=MAX_OUTPUT_LIMIT).contains(&limit) {
        return Err(Error::invalid(
            backend,
            "read_output",
            "limit must be between 1 and 1048576 bytes",
        ));
    }
    Ok(())
}

/// Successful native management request, not just an open port. Versions may be unavailable.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ManagerInfo {
    pub backend: String,
    pub client_version: Option<String>,
    pub server_version: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ProcessEvent {
    Output {
        channel: OutputChannel,
        data: Vec<u8>,
    },
    Exit(ExitStatus),
}

pub type EventStream = Pin<Box<dyn Stream<Item = Result<ProcessEvent>> + Send + 'static>>;

/// Owns a native output subscription, not the process lifetime. Dropping it does not delete resources.
pub struct ProcessHandle {
    pub process: ProcessRef,
    pub events: EventStream,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize)]
pub struct ProcessCapabilities {
    pub list: bool,
    pub pause: bool,
    pub start: bool,
    pub restart: bool,
    pub kill: bool,
    pub remove: bool,
    /// Configured access to a retained, bounded native log resource.
    pub output_resource: bool,
    pub stdin: bool,
    pub close_stdin: bool,
    pub binary_stdin: bool,
    pub pty: bool,
    pub resize: bool,
    /// Exact portable signals accepted by this instance's native protocol.
    pub supported_signals: &'static [Signal],
    pub detached: bool,
    pub reconnect: bool,
    pub historical_output: bool,
    pub streaming_output: bool,
    pub daemon_restart_recovery: bool,
}

/// Supported portable signals. Adapters reject signals missing from their native protocol.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub enum Signal {
    Interrupt,
    Terminate,
    Kill,
}

impl Signal {
    pub fn number(self) -> i32 {
        match self {
            Self::Interrupt => 2,
            Self::Terminate => 15,
            Self::Kill => 9,
        }
    }
}

/// Managed processes. Output may be live-only; consult capabilities and each adapter's retention contract.
#[async_trait]
pub trait ProcessManager: Send + Sync {
    fn backend(&self) -> &'static str;
    fn capabilities(&self) -> ProcessCapabilities;
    async fn probe(&self) -> Result<ManagerInfo> {
        Err(Error::unsupported(self.backend(), "probe"))
    }
    async fn list(&self) -> Result<Vec<ProcessInfo>> {
        Err(Error::unsupported(self.backend(), "list"))
    }
    async fn spawn(&self, request: SpawnRequest) -> Result<ProcessHandle>;
    async fn status(&self, process: &ProcessRef) -> Result<ProcessInfo>;
    async fn wait(&self, process: &ProcessRef, timeout: Option<Duration>) -> Result<ExitStatus>;
    async fn output(&self, process: &ProcessRef) -> Result<EventStream>;
    /// Backend extension contract. Callers normally use `Process::output().stat()`.
    async fn output_stat(&self, _process: &ProcessRef) -> Result<OutputInfo> {
        Err(Error::unsupported(self.backend(), "output_stat"))
    }
    /// Backend extension contract; cursor validation and log retention are adapter-specific.
    async fn read_output(
        &self,
        _process: &ProcessRef,
        _cursor: Option<&OutputCursor>,
        limit: usize,
    ) -> Result<OutputPage> {
        validate_output_limit(limit, self.backend())?;
        Err(Error::unsupported(self.backend(), "read_output"))
    }
    async fn pause(&self, _process: &ProcessRef) -> Result<()> {
        Err(Error::unsupported(self.backend(), "pause"))
    }
    /// Native start/resume; a manager may force a queued task past normal queue limits.
    async fn start(&self, _process: &ProcessRef) -> Result<()> {
        Err(Error::unsupported(self.backend(), "start"))
    }
    /// Native restart, preserving the manager's task identity and replacing the prior execution.
    async fn restart(&self, _process: &ProcessRef) -> Result<()> {
        Err(Error::unsupported(self.backend(), "restart"))
    }
    /// Native termination path. Acceptance does not imply the process has already exited.
    async fn kill(&self, _process: &ProcessRef) -> Result<()> {
        Err(Error::unsupported(self.backend(), "kill"))
    }
    /// Remove a retained task and its native logs; never implicitly kill it.
    async fn remove(&self, _process: &ProcessRef) -> Result<()> {
        Err(Error::unsupported(self.backend(), "remove"))
    }
    async fn send_input(&self, _process: &ProcessRef, _data: Vec<u8>) -> Result<()> {
        Err(Error::unsupported(self.backend(), "send_input"))
    }
    async fn close_stdin(&self, _process: &ProcessRef) -> Result<()> {
        Err(Error::unsupported(self.backend(), "close_stdin"))
    }
    async fn signal(&self, _process: &ProcessRef, _signal: Signal) -> Result<()> {
        Err(Error::unsupported(self.backend(), "signal"))
    }
    async fn resize(&self, _process: &ProcessRef, _size: PtySize) -> Result<()> {
        Err(Error::unsupported(self.backend(), "resize"))
    }
    async fn reconnect(&self, _process: &ProcessRef) -> Result<ProcessHandle> {
        Err(Error::unsupported(self.backend(), "reconnect"))
    }
}

/// Bound resource facade; owns neither a process nor authoritative cached state.
#[derive(Clone)]
pub struct Process {
    manager: Arc<dyn ProcessManager>,
    reference: ProcessRef,
}

impl Process {
    /// Binding is local; existence and access are checked by subsequent native operations.
    pub fn new(manager: Arc<dyn ProcessManager>, reference: ProcessRef) -> Self {
        Self { manager, reference }
    }
    pub fn reference(&self) -> &ProcessRef {
        &self.reference
    }
    pub fn output(&self) -> ProcessOutput {
        ProcessOutput {
            process: self.clone(),
        }
    }
    pub async fn inspect(&self) -> Result<ProcessInfo> {
        self.manager.status(&self.reference).await
    }
    pub async fn wait(&self, timeout: Option<Duration>) -> Result<ExitStatus> {
        self.manager.wait(&self.reference, timeout).await
    }
    pub async fn pause(&self) -> Result<()> {
        self.manager.pause(&self.reference).await
    }
    pub async fn start(&self) -> Result<()> {
        self.manager.start(&self.reference).await
    }
    pub async fn restart(&self) -> Result<()> {
        self.manager.restart(&self.reference).await
    }
    pub async fn kill(&self) -> Result<()> {
        self.manager.kill(&self.reference).await
    }
    pub async fn remove(&self) -> Result<()> {
        self.manager.remove(&self.reference).await
    }
    pub async fn signal(&self, signal: Signal) -> Result<()> {
        self.manager.signal(&self.reference, signal).await
    }
    pub async fn send_input(&self, data: Vec<u8>) -> Result<()> {
        self.manager.send_input(&self.reference, data).await
    }
    pub async fn close_stdin(&self) -> Result<()> {
        self.manager.close_stdin(&self.reference).await
    }
    pub async fn resize(&self, size: PtySize) -> Result<()> {
        self.manager.resize(&self.reference, size).await
    }
}

impl dyn ProcessManager {
    /// Bind a retained reference without contacting the native manager.
    pub fn get(self: &Arc<Self>, reference: ProcessRef) -> Process {
        Process::new(self.clone(), reference)
    }
}

/// Independent access to the manager's native log; subscribing does not advance read cursors.
#[derive(Clone)]
pub struct ProcessOutput {
    process: Process,
}

impl ProcessOutput {
    pub async fn stat(&self) -> Result<OutputInfo> {
        self.process
            .manager
            .output_stat(&self.process.reference)
            .await
    }
    /// Default page size is `DEFAULT_OUTPUT_LIMIT`; callers choose an explicit limit in Rust.
    pub async fn read(&self, cursor: Option<&OutputCursor>, limit: usize) -> Result<OutputPage> {
        validate_output_limit(limit, self.process.manager.backend())?;
        self.process
            .manager
            .read_output(&self.process.reference, cursor, limit)
            .await
    }
    pub async fn subscribe(&self) -> Result<ProcessHandle> {
        Ok(ProcessHandle {
            process: self.process.reference.clone(),
            events: self.process.manager.output(&self.process.reference).await?,
        })
    }
}

/// Consumes a subscription to its exit event without inventing a successful exit on disconnection.
pub async fn wait_events(mut events: EventStream, backend: &str) -> Result<ExitStatus> {
    while let Some(event) = events.next().await {
        if let ProcessEvent::Exit(exit) = event? {
            return Ok(exit);
        }
    }
    Err(Error::new(
        ErrorKind::Transport,
        backend,
        "wait",
        "stream ended before exit status",
    ))
}

pub fn validate_timeout(timeout: Option<Duration>, backend: &str, operation: &str) -> Result<()> {
    if timeout.is_some_and(|duration| duration.is_zero()) {
        return Err(Error::invalid(
            backend,
            operation,
            "timeout must be positive or absent",
        ));
    }
    Ok(())
}
