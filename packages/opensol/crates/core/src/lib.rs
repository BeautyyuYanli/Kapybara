//! Public clients for resource lifecycle, completed commands, and managed processes.
//!
//! Adapters implement [`raw`] contracts; clients enforce shared validation and capability checks.
//! Dropping clients never destroys environments. References contain identity, not credentials.
//! A transport failure or cancelled call does not imply that a remote side effect was undone.

mod backend;
mod capabilities;
pub use capabilities::*;
pub mod raw;
pub use backend::{
    Api, Backend, BackendAccess, BackendConfig, BackendSource, OpenOptions, SandboxSpec,
};

use std::{
    collections::BTreeMap,
    fmt,
    pin::Pin,
    sync::{Arc, Mutex, PoisonError},
    time::Duration,
};

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

/// Opaque identity within the original manager's target, account, endpoint and namespace/workspace.
/// It carries neither credentials nor complete routing; backend validation cannot verify that scope.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct SandboxRef {
    pub backend: String,
    pub id: String,
}

impl SandboxRef {
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
                "invalid sandbox reference",
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SandboxState {
    Pending,
    Running,
    Stopped,
    Completed,
    Failed,
    Unknown,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct SandboxInfo {
    pub sandbox: SandboxRef,
    /// Name and labels from this native response, never SDK creation metadata or a local cache.
    pub name: Option<String>,
    pub labels: BTreeMap<String, String>,
    pub state: SandboxState,
    pub native_state: String,
}

/// Common creation metadata. Provider images, templates and policies belong to per-call specs.
#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
pub struct CreateOptions {
    pub name: Option<String>,
    pub labels: BTreeMap<String, String>,
}

/// Creates, discovers and locally binds native sandboxes within the adapter's configured scope.
/// Clones share the adapter; dropping a client never deletes its sandboxes.
#[derive(Clone)]
pub struct SandboxManager {
    access: Arc<dyn raw::SandboxManagerAccess>,
    owner: Option<Backend>,
}

impl SandboxManager {
    pub fn new(access: impl raw::SandboxManagerAccess + 'static) -> Self {
        Self {
            access: Arc::new(access),
            owner: None,
        }
    }
    pub async fn open(source: impl BackendSource) -> Result<Self> {
        source
            .resolve(Api::Sandboxes, OpenOptions::default())
            .await?
            .sandboxes()
    }
    pub fn backend(&self) -> &'static str {
        self.access.backend()
    }
    pub fn capabilities(&self) -> SandboxManagerCapabilities {
        self.access.capabilities()
    }
    /// Bind locally; existence and permissions are checked by subsequent native operations.
    pub fn get(&self, reference: SandboxRef) -> Sandbox {
        Sandbox::new(self.clone(), reference)
    }
    /// Lists native sandboxes, including stopped sandboxes still retained by the backend.
    pub async fn list(&self) -> Result<Vec<SandboxInfo>> {
        self.require(self.capabilities().list, "list")?;
        self.access.list().await
    }
    /// Confirms the native create request and binds its result; does not promise readiness.
    pub async fn create(&self, spec: impl Into<SandboxSpec>) -> Result<Sandbox> {
        let spec = spec.into();
        let options = &spec.options;
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
        self.access
            .create(spec)
            .await
            .map(|reference| self.get(reference))
    }
    async fn inspect(&self, resource: &SandboxRef) -> Result<SandboxInfo> {
        resource.validate(self.backend(), "inspect")?;
        self.require(self.capabilities().sandbox.inspect, "inspect")?;
        self.access.inspect(resource).await
    }
    async fn start(&self, resource: &SandboxRef) -> Result<()> {
        resource.validate(self.backend(), "start")?;
        self.require(self.capabilities().sandbox.start, "start")?;
        self.access.start(resource).await
    }
    async fn stop(&self, resource: &SandboxRef) -> Result<()> {
        resource.validate(self.backend(), "stop")?;
        self.require(self.capabilities().sandbox.stop.is_some(), "stop")?;
        self.access.stop(resource).await
    }
    async fn delete(&self, resource: &SandboxRef) -> Result<()> {
        resource.validate(self.backend(), "delete")?;
        self.require(self.capabilities().sandbox.delete, "delete")?;
        self.access.delete(resource).await
    }
    async fn fork(&self, resource: &SandboxRef) -> Result<Sandbox> {
        resource.validate(self.backend(), "fork")?;
        self.require(self.capabilities().sandbox.fork, "fork")?;
        self.access
            .fork(resource)
            .await
            .map(|reference| self.get(reference))
    }
    fn require(&self, supported: bool, operation: &str) -> Result<()> {
        require(supported, self.backend(), operation)
    }
}

/// A locally bound sandbox. State and lifetime remain native to the adapter.
/// Mutations confirm successful native requests, not readiness or state convergence.
/// Use inspect for observations; even Running does not guarantee an exec endpoint is reachable.
#[derive(Clone)]
pub struct Sandbox {
    manager: SandboxManager,
    reference: SandboxRef,
}

impl fmt::Debug for Sandbox {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Sandbox")
            .field("reference", &self.reference)
            .finish_non_exhaustive()
    }
}

impl Sandbox {
    pub fn new(manager: SandboxManager, reference: SandboxRef) -> Self {
        Self { manager, reference }
    }
    pub fn reference(&self) -> &SandboxRef {
        &self.reference
    }
    pub fn capabilities(&self) -> SandboxCapabilities {
        self.manager.capabilities().sandbox
    }
    pub async fn inspect(&self) -> Result<SandboxInfo> {
        self.manager.inspect(&self.reference).await
    }
    /// Confirms the native start request; inspect separately for the backend's running state.
    pub async fn start(&self) -> Result<()> {
        self.manager.start(&self.reference).await
    }
    /// Confirms the native stop request, without a common state-convergence barrier.
    pub async fn stop(&self) -> Result<()> {
        self.manager.stop(&self.reference).await
    }
    /// Confirms the native delete request; only a later NotFound confirms disappearance.
    pub async fn delete(&self) -> Result<()> {
        self.manager.delete(&self.reference).await
    }
    /// Confirms a native fork and binds the child, without a common readiness guarantee.
    pub async fn fork(&self) -> Result<Sandbox> {
        self.manager.fork(&self.reference).await
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

/// Completed-command client bound to one execution target. Calls are never retried implicitly.
#[derive(Clone)]
pub struct Connection {
    access: Arc<dyn raw::ConnectionAccess>,
    owner: Option<Backend>,
}

impl Connection {
    pub fn new(access: impl raw::ConnectionAccess + 'static) -> Self {
        Self {
            access: Arc::new(access),
            owner: None,
        }
    }
    pub async fn open(source: impl BackendSource, target: Option<SandboxRef>) -> Result<Self> {
        let backend = source
            .resolve(
                Api::Connection,
                OpenOptions {
                    target: target.clone(),
                    connection: None,
                },
            )
            .await?;
        backend.connection(target.as_ref()).await
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
        if !request.stdin.is_empty() {
            let constraints = capabilities
                .stdin
                .ok_or_else(|| Error::unsupported(self.backend(), "exec"))?;
            constraints.validate(&request.stdin, self.backend(), "exec")?;
        }
        require(
            request.timeout.is_none() || capabilities.timeout,
            self.backend(),
            "exec",
        )?;
        self.access.exec(request).await
    }
}

/// Identity within the original manager's target, credentials, Connection and adapter configuration;
/// not a global locator or necessarily an OS PID. Backend validation cannot verify that scope.
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
    /// The backend's managed task completed; OS exit and an exit status may still be unavailable.
    Completed,
    Unknown,
}

/// Confirmed native task completion, including results without an OS exit status.
/// At least one field must be present. Neither a missing exit nor a native result implies success.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ProcessOutcome {
    /// Only native evidence of an OS exit code or signal; absence does not prove OS exit.
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

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OutputChannel {
    Stdout,
    Stderr,
    Terminal,
    Combined,
}

/// Persistable opaque position, scoped to one manager target, task and log version.
/// Adapters validate their encoding; callers must not interpret or modify it.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct OutputCursor(pub String);

/// A terminal-history boundary, distinct from a raw byte-output position.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct TerminalCursor(pub String);

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReadDirection {
    Forward,
    Backward,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct OutputChunk {
    pub channel: OutputChannel,
    pub data: Vec<u8>,
}

/// Boundaries surround this page; backward pages retain native content order.
/// `at_tail` describes this observation, never process completion.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct OutputPage {
    pub chunks: Vec<OutputChunk>,
    pub before: Option<OutputCursor>,
    pub after: Option<OutputCursor>,
    pub at_head: Option<bool>,
    pub at_tail: bool,
}

/// Native rendered history; lines exclude protocol LF separators and retain all other text.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct TerminalPage {
    pub lines: Vec<String>,
    pub before: Option<TerminalCursor>,
    pub after: Option<TerminalCursor>,
    pub at_head: Option<bool>,
    pub at_tail: bool,
}

/// The active native screen, including blank lines, with its actual rendered dimensions.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct TerminalScreen {
    pub cols: u32,
    pub rows: u32,
    pub lines: Vec<String>,
}

pub const DEFAULT_OUTPUT_LIMIT: usize = 65_536;
pub const MAX_OUTPUT_LIMIT: usize = 1_048_576;
pub const DEFAULT_TERMINAL_LINES: usize = 200;
pub const MAX_TERMINAL_LINES: usize = 10_000;

/// Enforce a bounded read before doing any target I/O.
pub fn validate_output_limit(limit: usize, backend: &str) -> Result<()> {
    if !(1..=MAX_OUTPUT_LIMIT).contains(&limit) {
        return Err(Error::invalid(
            backend,
            "read_output",
            "max_bytes must be between 1 and 1048576 bytes",
        ));
    }
    Ok(())
}

pub fn validate_terminal_lines(lines: usize, backend: &str) -> Result<()> {
    if !(1..=MAX_TERMINAL_LINES).contains(&lines) {
        return Err(Error::invalid(
            backend,
            "read_terminal",
            "lines must be between 1 and 10000",
        ));
    }
    Ok(())
}

/// Locally bound raw byte view. It neither collects output nor owns a task's lifetime.
#[derive(Clone)]
pub struct Output {
    access: Arc<dyn raw::OutputAccess>,
    reference: ProcessRef,
    initial_subscription: Arc<Mutex<Option<OutputSubscription>>>,
    owner: Option<Backend>,
}

impl Output {
    pub fn new(access: impl raw::OutputAccess + 'static, reference: ProcessRef) -> Self {
        Self {
            access: Arc::new(access),
            reference,
            initial_subscription: Arc::new(Mutex::new(None)),
            owner: None,
        }
    }
    pub fn backend(&self) -> &'static str {
        self.access.backend()
    }
    pub fn capabilities(&self) -> OutputCapabilities {
        let mut capabilities = self.access.capabilities(&self.reference).normalized();
        // Only the shared slot can declare an available initial stream; raw view descriptions
        // describe reconnect. Taking the stream changes all related views atomically.
        capabilities.subscribe.initial = self
            .initial_subscription
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .as_ref()
            .map(|subscription| subscription.capabilities().clone());
        capabilities
    }
    /// Reads immediately. An absent cursor selects the head (forward) or observed tail (backward).
    pub async fn read(
        &self,
        cursor: Option<&OutputCursor>,
        direction: ReadDirection,
        max_bytes: usize,
    ) -> Result<OutputPage> {
        self.reference.validate(self.backend(), "read_output")?;
        validate_output_limit(max_bytes, self.backend())?;
        require(self.capabilities().read, self.backend(), "read_output")?;
        self.access
            .read(&self.reference, cursor, direction, max_bytes)
            .await
    }
    pub async fn tail(&self, max_bytes: usize) -> Result<OutputPage> {
        self.reference.validate(self.backend(), "tail_output")?;
        validate_output_limit(max_bytes, self.backend())?;
        require(self.capabilities().tail, self.backend(), "tail_output")?;
        self.access.tail(&self.reference, max_bytes).await
    }
    /// Binds the native execution observable while this subscription is established.
    /// Only nonreplaceable executions may supply an indispensable spawn stream, handed off once
    /// across Process/Output clones. No output is prefetched or archived; extra calls subscribe anew.
    pub async fn subscribe(&self) -> Result<OutputSubscription> {
        self.reference
            .validate(self.backend(), "subscribe_output")?;
        let initial = self
            .initial_subscription
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        if let Some(subscription) = initial {
            return Ok(subscription.retain(self.owner.clone()));
        }
        let delivery = self
            .capabilities()
            .subscribe
            .reconnect
            .ok_or_else(|| Error::unsupported(self.backend(), "subscribe_output"))?;
        let subscription = self.access.subscribe(&self.reference).await?;
        if !delivery.permits(subscription.capabilities()) {
            return Err(Error::new(
                ErrorKind::Backend,
                self.backend(),
                "subscribe_output",
                "subscription contradicts its declared source",
            ));
        }
        Ok(subscription.retain(self.owner.clone()))
    }
}

/// Locally bound native rendered text. This view never interprets raw output bytes.
#[derive(Clone)]
pub struct Terminal {
    access: Arc<dyn raw::TerminalAccess>,
    reference: ProcessRef,
    _owner: Option<Backend>,
}

impl Terminal {
    pub fn new(access: impl raw::TerminalAccess + 'static, reference: ProcessRef) -> Self {
        Self {
            access: Arc::new(access),
            reference,
            _owner: None,
        }
    }
    pub fn backend(&self) -> &'static str {
        self.access.backend()
    }
    pub fn capabilities(&self) -> TerminalCapabilities {
        self.access.capabilities(&self.reference)
    }
    pub async fn read(
        &self,
        cursor: Option<&TerminalCursor>,
        direction: ReadDirection,
        lines: usize,
    ) -> Result<TerminalPage> {
        self.reference.validate(self.backend(), "read_terminal")?;
        validate_terminal_lines(lines, self.backend())?;
        require(self.capabilities().read, self.backend(), "read_terminal")?;
        self.access
            .read(&self.reference, cursor, direction, lines)
            .await
    }
    pub async fn tail(&self, lines: usize) -> Result<TerminalPage> {
        self.reference.validate(self.backend(), "tail_terminal")?;
        validate_terminal_lines(lines, self.backend())?;
        require(self.capabilities().tail, self.backend(), "tail_terminal")?;
        self.access.tail(&self.reference, lines).await
    }
    pub async fn screen(&self) -> Result<TerminalScreen> {
        self.reference.validate(self.backend(), "terminal_screen")?;
        require(
            self.capabilities().screen,
            self.backend(),
            "terminal_screen",
        )?;
        self.access.screen(&self.reference).await
    }
}

struct UnavailableView(&'static str);
impl raw::OutputAccess for UnavailableView {
    fn backend(&self) -> &'static str {
        self.0
    }
    fn capabilities(&self, _process: &ProcessRef) -> OutputCapabilities {
        OutputCapabilities::default()
    }
}
impl raw::TerminalAccess for UnavailableView {
    fn backend(&self) -> &'static str {
        self.0
    }
    fn capabilities(&self, _process: &ProcessRef) -> TerminalCapabilities {
        TerminalCapabilities::default()
    }
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
pub struct OutputSubscription {
    reference: ProcessRef,
    delivery: SubscriptionDelivery,
    pub events: EventStream,
}

impl OutputSubscription {
    /// Adapter constructor; the task identity is retained only for internal binding.
    pub fn new(reference: ProcessRef, delivery: SubscriptionDelivery, events: EventStream) -> Self {
        Self {
            reference,
            delivery: delivery.normalized(),
            events,
        }
    }
    /// The actual source contract, fixed even after completion or subscription closure.
    pub fn capabilities(&self) -> &SubscriptionDelivery {
        &self.delivery
    }
    pub fn backend(&self) -> &str {
        &self.reference.backend
    }
    // Keep context ownership with the stream even when a language bridge extracts `events`.
    fn retain(mut self, owner: Option<Backend>) -> Self {
        self.events = Box::pin(futures::stream::unfold(
            (self.events, owner),
            |(mut events, owner)| async move {
                events.next().await.map(|event| (event, (events, owner)))
            },
        ));
        self
    }
}

/// Portable signals. Public clients check the adapter's declared supported set before dispatch.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(into = "i32")]
pub enum Signal {
    Interrupt,
    Terminate,
    Kill,
}

impl From<Signal> for i32 {
    fn from(signal: Signal) -> Self {
        signal.number()
    }
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
    access: Arc<dyn raw::ProcessManagerAccess>,
    owner: Option<Backend>,
}

impl ProcessManager {
    pub fn new(access: impl raw::ProcessManagerAccess + 'static) -> Self {
        Self {
            access: Arc::new(access),
            owner: None,
        }
    }
    pub async fn open(
        source: impl BackendSource,
        target: Option<SandboxRef>,
        connection: Option<Connection>,
    ) -> Result<Self> {
        let backend = source
            .resolve(
                Api::Processes,
                OpenOptions {
                    target: target.clone(),
                    connection,
                },
            )
            .await?;
        backend.processes(target.as_ref()).await
    }
    pub fn backend(&self) -> &'static str {
        self.access.backend()
    }
    /// Configured backend upper bound, not a guarantee for every discovered ProcessRef.
    pub fn capabilities(&self) -> ProcessManagerCapabilities {
        self.access.capabilities().normalized()
    }
    /// Bind a reference locally. Subsequent operations check identity, existence and permissions.
    pub fn get(&self, reference: ProcessRef) -> Process {
        Process::new(self.clone(), reference)
    }
    pub async fn probe(&self) -> Result<ManagerInfo> {
        self.require(self.capabilities().probe, "probe")?;
        self.access.probe().await
    }
    pub async fn list(&self) -> Result<Vec<ProcessInfo>> {
        self.require(self.capabilities().list, "list")?;
        self.access.list().await
    }
    /// Binds the new task, retaining only an indispensable, nonreplaceable native Start stream.
    pub async fn spawn(&self, request: SpawnRequest) -> Result<Process> {
        request.validate(self.backend())?;
        let capabilities = self.capabilities();
        self.require(
            capabilities.spawn_modes.contains(&SpawnMode {
                pty: request.pty.is_some(),
                stdin: request.stdin,
            }),
            "spawn",
        )?;
        let spawned = self.access.spawn(request).await?;
        if let Some(subscription) = &spawned.initial_subscription {
            let allowed = !self.access.process_capabilities(&spawned.reference).restart
                && capabilities
                    .output
                    .subscribe
                    .initial
                    .as_ref()
                    .is_some_and(|delivery| delivery.permits(subscription.capabilities()));
            if !allowed {
                return Err(Error::new(
                    ErrorKind::Backend,
                    self.backend(),
                    "spawn",
                    "initial subscription contradicts manager capabilities",
                ));
            }
        }
        Ok(Process {
            manager: self.clone(),
            reference: spawned.reference,
            initial_subscription: Arc::new(Mutex::new(spawned.initial_subscription)),
        })
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
    async fn pause(&self, process: &ProcessRef) -> Result<()> {
        process.validate(self.backend(), "pause")?;
        self.require(self.access.process_capabilities(process).pause, "pause")?;
        self.access.pause(process).await
    }
    async fn start(&self, process: &ProcessRef) -> Result<()> {
        process.validate(self.backend(), "start")?;
        self.require(self.access.process_capabilities(process).start, "start")?;
        self.access.start(process).await
    }
    async fn restart(&self, process: &ProcessRef) -> Result<()> {
        process.validate(self.backend(), "restart")?;
        self.require(self.access.process_capabilities(process).restart, "restart")?;
        self.access.restart(process).await
    }
    async fn kill(&self, process: &ProcessRef) -> Result<()> {
        process.validate(self.backend(), "kill")?;
        self.require(
            self.access.process_capabilities(process).kill.is_some(),
            "kill",
        )?;
        self.access.kill(process).await
    }
    async fn remove(&self, process: &ProcessRef) -> Result<()> {
        process.validate(self.backend(), "remove")?;
        self.require(self.access.process_capabilities(process).remove, "remove")?;
        self.access.remove(process).await
    }
    async fn send_input(&self, process: &ProcessRef, data: Vec<u8>) -> Result<()> {
        process.validate(self.backend(), "send_input")?;
        let capabilities = self.access.process_capabilities(process);
        let input = capabilities
            .input
            .ok_or_else(|| Error::unsupported(self.backend(), "send_input"))?;
        input
            .constraints
            .validate(&data, self.backend(), "send_input")?;
        self.access.send_input(process, data).await
    }
    async fn close_stdin(&self, process: &ProcessRef) -> Result<()> {
        process.validate(self.backend(), "close_stdin")?;
        let capabilities = self.access.process_capabilities(process);
        self.require(
            capabilities.input.is_some_and(|input| input.close),
            "close_stdin",
        )?;
        self.access.close_stdin(process).await
    }
    async fn signal(&self, process: &ProcessRef, signal: Signal) -> Result<()> {
        process.validate(self.backend(), "signal")?;
        self.require(
            self.access
                .process_capabilities(process)
                .supported_signals
                .contains(&signal),
            "signal",
        )?;
        self.access.signal(process, signal).await
    }
    async fn resize(&self, process: &ProcessRef, size: PtySize) -> Result<()> {
        process.validate(self.backend(), "resize")?;
        size.validate(self.backend(), "resize")?;
        let capabilities = self.access.process_capabilities(process);
        self.require(capabilities.resize, "resize")?;
        self.access.resize(process, size).await
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

/// Bound task operations and output access; owns neither the task nor authoritative cached state.
/// Log reads and subscriptions have independent positions; only subscriptions own delivery state.
#[derive(Clone)]
pub struct Process {
    manager: ProcessManager,
    reference: ProcessRef,
    initial_subscription: Arc<Mutex<Option<OutputSubscription>>>,
}

impl Process {
    /// Binding is local; existence and access are checked by subsequent native operations.
    pub fn new(manager: ProcessManager, reference: ProcessRef) -> Self {
        Self {
            manager,
            reference,
            initial_subscription: Arc::new(Mutex::new(None)),
        }
    }
    pub fn reference(&self) -> &ProcessRef {
        &self.reference
    }
    /// Local mode-aware subset of the manager's capabilities, without probing native state.
    /// Support does not promise existence, permission, a running task or an open stdin channel.
    pub fn capabilities(&self) -> ProcessCapabilities {
        self.manager
            .access
            .process_capabilities(&self.reference)
            .normalized()
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
    /// Confirms the native control request, not OS exit or continued task retention.
    /// Pueue/envd retain results only within native retention windows. Tmux destroys its pane;
    /// subsequent inspect/wait/subscribe return NotFound, without a synthesized outcome.
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
    /// Checks local process capabilities before dispatch; native payload restrictions still apply.
    pub async fn send_input(&self, data: Vec<u8>) -> Result<()> {
        self.manager.send_input(&self.reference, data).await
    }
    pub async fn close_stdin(&self) -> Result<()> {
        self.manager.close_stdin(&self.reference).await
    }
    pub async fn resize(&self, size: PtySize) -> Result<()> {
        self.manager.resize(&self.reference, size).await
    }
    /// View binding and capability queries are local, including for unsupported representations.
    pub fn output(&self) -> Output {
        Output {
            access: self
                .manager
                .access
                .output_access()
                .unwrap_or_else(|| Arc::new(UnavailableView(self.manager.backend()))),
            reference: self.reference.clone(),
            initial_subscription: self.initial_subscription.clone(),
            owner: self.manager.owner.clone(),
        }
    }
    pub fn terminal(&self) -> Terminal {
        Terminal {
            access: self
                .manager
                .access
                .terminal_access()
                .unwrap_or_else(|| Arc::new(UnavailableView(self.manager.backend()))),
            reference: self.reference.clone(),
            _owner: self.manager.owner.clone(),
        }
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
