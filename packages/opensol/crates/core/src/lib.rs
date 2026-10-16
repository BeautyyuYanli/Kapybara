//! Public clients for resource lifecycle, completed commands, and managed processes.
//!
//! Adapters implement [`raw`] contracts; clients enforce shared validation and capability checks.
//! Dropping clients never destroys environments. References contain identity, not credentials.
//! A transport failure or cancelled call does not imply that a remote side effect was undone.

pub mod raw;

use std::{collections::BTreeMap, fmt, pin::Pin, sync::Arc, time::Duration};

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

/// Explicit resource operations. Start/stop retain native semantics, including memory preservation.
/// Clones share the adapter; dropping a client never deletes its resources.
#[derive(Clone)]
pub struct Lifecycle {
    access: Arc<dyn raw::LifecycleAccess>,
}

impl Lifecycle {
    pub fn new(access: impl raw::LifecycleAccess + 'static) -> Self {
        Self {
            access: Arc::new(access),
        }
    }
    pub fn backend(&self) -> &'static str {
        self.access.backend()
    }
    pub fn capabilities(&self) -> LifecycleCapabilities {
        self.access.capabilities()
    }
    pub async fn create(&self, options: CreateOptions) -> Result<ResourceRef> {
        if options
            .name
            .as_ref()
            .is_some_and(|name| name.is_empty() || name.contains('\0'))
            || options
                .labels
                .iter()
                .any(|(key, value)| key.is_empty() || key.contains('\0') || value.contains('\0'))
        {
            return Err(Error::invalid(
                self.backend(),
                "create",
                "invalid creation metadata",
            ));
        }
        self.require(self.capabilities().create, "create")?;
        self.access.create(options).await
    }
    pub async fn inspect(&self, resource: &ResourceRef) -> Result<ResourceInfo> {
        resource.validate(self.backend(), "inspect")?;
        self.require(self.capabilities().inspect, "inspect")?;
        self.access.inspect(resource).await
    }
    pub async fn start(&self, resource: &ResourceRef) -> Result<()> {
        resource.validate(self.backend(), "start")?;
        self.require(self.capabilities().start, "start")?;
        self.access.start(resource).await
    }
    pub async fn stop(&self, resource: &ResourceRef) -> Result<()> {
        resource.validate(self.backend(), "stop")?;
        self.require(self.capabilities().stop, "stop")?;
        self.access.stop(resource).await
    }
    pub async fn delete(&self, resource: &ResourceRef) -> Result<()> {
        resource.validate(self.backend(), "delete")?;
        self.require(self.capabilities().delete, "delete")?;
        self.access.delete(resource).await
    }
    pub async fn fork(&self, resource: &ResourceRef) -> Result<ResourceRef> {
        resource.validate(self.backend(), "fork")?;
        self.require(self.capabilities().fork, "fork")?;
        self.access.fork(resource).await
    }
    fn require(&self, supported: bool, operation: &str) -> Result<()> {
        require(supported, self.backend(), operation)
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

/// Completed-command client bound to one execution target. Calls are never retried implicitly.
#[derive(Clone)]
pub struct Connection {
    access: Arc<dyn raw::ExecAccess>,
}

impl Connection {
    pub fn new(access: impl raw::ExecAccess + 'static) -> Self {
        Self {
            access: Arc::new(access),
        }
    }
    pub fn backend(&self) -> &'static str {
        self.access.backend()
    }
    pub fn capabilities(&self) -> ConnectionCapabilities {
        self.access.capabilities()
    }
    pub async fn exec(&self, request: ExecRequest) -> Result<ExecResult> {
        request.validate(self.backend())?;
        let capabilities = self.capabilities();
        require(
            request.stdin.is_empty() || capabilities.stdin,
            self.backend(),
            "exec",
        )?;
        require(
            request.timeout.is_none() || capabilities.timeout,
            self.backend(),
            "exec",
        )?;
        self.access.exec(request).await
    }
}

/// Process identity is opaque and scoped to the manager's target, not necessarily an OS PID.
/// `pty` is a mode hint, not identity: `None` means native discovery did not report the mode.
/// Adapters decide which operations require this hint; it is not authoritative cached state.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ProcessRef {
    pub backend: String,
    pub id: String,
    pub pty: Option<bool>,
}

impl ProcessRef {
    pub fn new(backend: &str, id: impl Into<String>, pty: Option<bool>) -> Self {
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

/// Confirmed native task completion, including results without an OS exit status.
/// At least one field must be present. Neither a missing exit nor a native result implies success.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ProcessOutcome {
    pub exit: Option<ExitStatus>,
    /// Native result name only, never the provider's error body or task environment.
    pub native_result: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ProcessInfo {
    pub process: ProcessRef,
    pub state: ProcessState,
    /// Present only for a confirmed terminal task; absent for nonterminal or unknown state.
    pub outcome: Option<ProcessOutcome>,
    pub native_state: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OutputChannel {
    Stdout,
    Stderr,
    Terminal,
    Combined,
}

/// Retained native output metadata, independent of task state (see `ProcessInfo`).
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct OutputInfo {
    pub channels: Vec<OutputChannel>,
    pub size: u64,
    /// Opaque output generation identifier; None means the backend has no identifier to report.
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
    Completed(ProcessOutcome),
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
    /// True guarantees arbitrary bytes; false leaves native payload restrictions to the adapter.
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

/// Portable signals. Public clients check the adapter's declared supported set before dispatch.
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

/// Finds and creates native tasks; use `get` to bind a `Process` for task operations.
/// Output retention and state remain native to the adapter.
#[derive(Clone)]
pub struct ProcessManager {
    access: Arc<dyn raw::ProcessAccess>,
}

impl ProcessManager {
    pub fn new(access: impl raw::ProcessAccess + 'static) -> Self {
        Self {
            access: Arc::new(access),
        }
    }
    pub fn backend(&self) -> &'static str {
        self.access.backend()
    }
    pub fn capabilities(&self) -> ProcessCapabilities {
        self.access.capabilities()
    }
    /// Bind a reference locally. Subsequent operations check identity, existence and permissions.
    pub fn get(&self, reference: ProcessRef) -> Process {
        Process::new(self.clone(), reference)
    }
    pub async fn probe(&self) -> Result<ManagerInfo> {
        self.access.probe().await
    }
    pub async fn list(&self) -> Result<Vec<ProcessInfo>> {
        self.require(self.capabilities().list, "list")?;
        self.access.list().await
    }
    /// Returns the native initial output subscription; binding a Process does not consume it.
    pub async fn spawn(&self, request: SpawnRequest) -> Result<ProcessHandle> {
        request.validate(self.backend())?;
        let capabilities = self.capabilities();
        self.require(!request.stdin || capabilities.stdin, "spawn")?;
        self.require(request.pty.is_none() || capabilities.pty, "spawn")?;
        self.access.spawn(request).await
    }
    async fn status(&self, process: &ProcessRef) -> Result<ProcessInfo> {
        process.validate(self.backend(), "status")?;
        self.access.status(process).await
    }
    async fn wait(
        &self,
        process: &ProcessRef,
        timeout: Option<Duration>,
    ) -> Result<ProcessOutcome> {
        process.validate(self.backend(), "wait")?;
        validate_timeout(timeout, self.backend(), "wait")?;
        self.access.wait(process, timeout).await
    }
    async fn output_stat(&self, process: &ProcessRef) -> Result<OutputInfo> {
        process.validate(self.backend(), "output_stat")?;
        self.require(self.capabilities().output_resource, "output_stat")?;
        self.access.output_stat(process).await
    }
    /// Reads are bounded; cursor encoding, versions and retention are checked by the adapter.
    async fn read_output(
        &self,
        process: &ProcessRef,
        cursor: Option<&OutputCursor>,
        limit: usize,
    ) -> Result<OutputPage> {
        process.validate(self.backend(), "read_output")?;
        validate_output_limit(limit, self.backend())?;
        self.require(self.capabilities().output_resource, "read_output")?;
        self.access.read_output(process, cursor, limit).await
    }
    async fn pause(&self, process: &ProcessRef) -> Result<()> {
        process.validate(self.backend(), "pause")?;
        self.require(self.capabilities().pause, "pause")?;
        self.access.pause(process).await
    }
    async fn start(&self, process: &ProcessRef) -> Result<()> {
        process.validate(self.backend(), "start")?;
        self.require(self.capabilities().start, "start")?;
        self.access.start(process).await
    }
    async fn restart(&self, process: &ProcessRef) -> Result<()> {
        process.validate(self.backend(), "restart")?;
        self.require(self.capabilities().restart, "restart")?;
        self.access.restart(process).await
    }
    async fn kill(&self, process: &ProcessRef) -> Result<()> {
        process.validate(self.backend(), "kill")?;
        self.require(self.capabilities().kill, "kill")?;
        self.access.kill(process).await
    }
    async fn remove(&self, process: &ProcessRef) -> Result<()> {
        process.validate(self.backend(), "remove")?;
        self.require(self.capabilities().remove, "remove")?;
        self.access.remove(process).await
    }
    async fn send_input(&self, process: &ProcessRef, data: Vec<u8>) -> Result<()> {
        process.validate(self.backend(), "send_input")?;
        let capabilities = self.capabilities();
        self.require(capabilities.stdin, "send_input")?;
        self.require(process.pty != Some(true) || capabilities.pty, "send_input")?;
        self.access.send_input(process, data).await
    }
    async fn close_stdin(&self, process: &ProcessRef) -> Result<()> {
        process.validate(self.backend(), "close_stdin")?;
        let capabilities = self.capabilities();
        self.require(capabilities.close_stdin, "close_stdin")?;
        self.require(process.pty != Some(true) || capabilities.pty, "close_stdin")?;
        self.access.close_stdin(process).await
    }
    async fn signal(&self, process: &ProcessRef, signal: Signal) -> Result<()> {
        process.validate(self.backend(), "signal")?;
        self.require(
            self.capabilities().supported_signals.contains(&signal),
            "signal",
        )?;
        self.access.signal(process, signal).await
    }
    async fn resize(&self, process: &ProcessRef, size: PtySize) -> Result<()> {
        process.validate(self.backend(), "resize")?;
        size.validate(self.backend(), "resize")?;
        let capabilities = self.capabilities();
        self.require(capabilities.resize, "resize")?;
        self.require(process.pty != Some(true) || capabilities.pty, "resize")?;
        self.access.resize(process, size).await
    }
    async fn reconnect(&self, process: &ProcessRef) -> Result<ProcessHandle> {
        process.validate(self.backend(), "reconnect")?;
        self.require(self.capabilities().reconnect, "reconnect")?;
        self.access.reconnect(process).await
    }
    fn require(&self, supported: bool, operation: &str) -> Result<()> {
        require(supported, self.backend(), operation)
    }
}

fn require(supported: bool, backend: &str, operation: &str) -> Result<()> {
    if supported {
        Ok(())
    } else {
        Err(Error::unsupported(backend, operation))
    }
}

/// Bound resource facade; owns neither a process nor authoritative cached state.
#[derive(Clone)]
pub struct Process {
    manager: ProcessManager,
    reference: ProcessRef,
}

impl Process {
    /// Binding is local; existence and access are checked by subsequent native operations.
    pub fn new(manager: ProcessManager, reference: ProcessRef) -> Self {
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
    /// Queries native task completion independently of any output subscription's cached outcome.
    pub async fn wait(&self, timeout: Option<Duration>) -> Result<ProcessOutcome> {
        self.manager.wait(&self.reference, timeout).await
    }
    pub async fn pause(&self) -> Result<()> {
        self.manager.pause(&self.reference).await
    }
    /// Native start/resume; some managers force queued tasks past queue limits.
    pub async fn start(&self) -> Result<()> {
        self.manager.start(&self.reference).await
    }
    /// Restarts the same task identity, replacing the previous execution and its logs.
    pub async fn restart(&self) -> Result<()> {
        self.manager.restart(&self.reference).await
    }
    /// Acceptance does not imply that the process has exited.
    pub async fn kill(&self) -> Result<()> {
        self.manager.kill(&self.reference).await
    }
    /// Removes retained task/logs, never implicitly killing a running task.
    pub async fn remove(&self) -> Result<()> {
        self.manager.remove(&self.reference).await
    }
    pub async fn signal(&self, signal: Signal) -> Result<()> {
        self.manager.signal(&self.reference, signal).await
    }
    /// Native mode requirements and payload restrictions are checked by the adapter.
    /// An explicit terminal hint still requires the manager's declared PTY support.
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

/// Bound output access; subscribing does not advance read cursors or require retained logs.
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
    /// Reads are bounded; cursor versions and retention are checked by the adapter.
    /// Default page size is `DEFAULT_OUTPUT_LIMIT`; callers choose an explicit limit in Rust.
    pub async fn read(&self, cursor: Option<&OutputCursor>, limit: usize) -> Result<OutputPage> {
        self.process
            .manager
            .read_output(&self.process.reference, cursor, limit)
            .await
    }
    /// Opens an independent subscription through native reconnect, requiring that capability.
    pub async fn subscribe(&self) -> Result<ProcessHandle> {
        self.process
            .manager
            .reconnect(&self.process.reference)
            .await
    }
}

/// Consumes a subscription to confirmed completion. Disconnection never invents an outcome.
pub async fn wait_events(mut events: EventStream, backend: &str) -> Result<ProcessOutcome> {
    while let Some(event) = events.next().await {
        if let ProcessEvent::Completed(outcome) = event? {
            return Ok(outcome);
        }
    }
    Err(Error::new(
        ErrorKind::Transport,
        backend,
        "wait",
        "stream ended before process completion",
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
