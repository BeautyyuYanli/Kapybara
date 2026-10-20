//! Native E2B envd Process ConnectRPC, also usable against a standalone envd.
//!
//! Streams preserve byte stdout/stderr/PTY separately. Dropping a stream disconnects, not kills.
//! Reconnect follows native retention and is not a promise of complete historical output. List/status
//! only sees processes envd currently lists; completed outcomes come from the native stream.
//! Input and terminal controls require a known mode because the RPCs cannot infer it from a PID.
//! Exec tolerates input NotFound only with a valid native End event. Reconciliation without it is
//! bounded by the control-request timeout; other input failures remain errors alongside completion.

use std::{pin::Pin, sync::Arc, time::Duration};

use async_trait::async_trait;
use base64::{Engine, engine::general_purpose::STANDARD};
use bytes::{Buf, BytesMut};
use futures::{Stream, StreamExt};
use opensol_core::{
    Connection, ConnectionCapabilities, Error, ErrorKind, EventStream, ExecRequest, ExecResult,
    ExitStatus, ManagerInfo, OutputCapabilities, OutputChannel, OutputSubscription, ProcessEvent,
    ProcessInfo, ProcessManager, ProcessManagerCapabilities, ProcessOutcome, ProcessRef,
    ProcessState, PtySize, Result, Signal, SpawnRequest,
    raw::{ConnectionAccess, OutputAccess, ProcessManagerAccess, SpawnedProcess},
    wait_events,
};
use prost::Message;
use reqwest::{
    Client, Url,
    header::{HeaderMap, HeaderValue},
};

use crate::{http, secret::Secret, util::deadline};

/// Generated verbatim from the pinned upstream schema; not the portable SDK model.
pub mod protocol {
    include!(concat!(env!("OUT_DIR"), "/process.rs"));
}

/// Explicit envd endpoint, sandbox proxy identity and optional authentication.
#[derive(Clone)]
pub struct EnvdConfig {
    pub endpoint: String,
    pub access_token: Option<Secret>,
    pub user: Option<String>,
    pub sandbox_id: Option<String>,
    pub request_timeout: Duration,
}

impl EnvdConfig {
    pub fn new(endpoint: impl Into<String>) -> Self {
        Self {
            endpoint: endpoint.into(),
            access_token: None,
            user: None,
            sandbox_id: None,
            request_timeout: Duration::from_secs(30),
        }
    }
}

impl std::fmt::Debug for EnvdConfig {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("EnvdConfig")
            .field("endpoint", &"[configured]")
            .field("access_token", &self.access_token)
            .field("user", &self.user)
            .field("sandbox_id", &self.sandbox_id)
            .field("request_timeout", &self.request_timeout)
            .finish()
    }
}

/// A reusable native client. There is no SDK-side process registry or output archive.
#[derive(Clone)]
pub struct Envd {
    client: Client,
    endpoint: Url,
    headers: HeaderMap,
    request_timeout: Duration,
}

impl std::fmt::Debug for Envd {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Envd")
            .field("endpoint", &"[configured]")
            .finish()
    }
}

type RawStream = Pin<Box<dyn Stream<Item = Result<Vec<u8>>> + Send>>;
const MAX_FRAME: usize = 16 * 1024 * 1024;

fn protocol_error(operation: &str) -> Error {
    Error::new(
        ErrorKind::Backend,
        "envd",
        operation,
        "invalid envd ConnectRPC response",
    )
}

fn rpc_error(code: &str, operation: &str) -> Error {
    let kind = match code {
        "invalid_argument" | "out_of_range" => ErrorKind::InvalidArgument,
        "not_found" => ErrorKind::NotFound,
        "already_exists" | "aborted" | "failed_precondition" | "resource_exhausted" => {
            ErrorKind::Conflict
        }
        "permission_denied" | "unauthenticated" => ErrorKind::Permission,
        "deadline_exceeded" => ErrorKind::Timeout,
        "unimplemented" => ErrorKind::Unsupported,
        "unavailable" | "canceled" => ErrorKind::Transport,
        _ => ErrorKind::Backend,
    };
    Error::new(kind, "envd", operation, "envd RPC failed")
}

impl Envd {
    /// Both views share this client's configured endpoint, headers and HTTP pool.
    pub fn connection(&self) -> Connection {
        Connection::new(self.clone())
    }

    pub fn processes(&self) -> ProcessManager {
        ProcessManager::new(self.clone())
    }

    pub fn new(config: EnvdConfig) -> Result<Self> {
        Self::with_client(config, http::client("envd")?)
    }

    /// Composition reuses the HTTP pool; authentication remains specific to each RPC.
    pub(crate) fn with_client(config: EnvdConfig, client: Client) -> Result<Self> {
        if config.request_timeout.is_zero() {
            return Err(Error::invalid(
                "envd",
                "configure",
                "request timeout must be positive",
            ));
        }
        let mut headers = HeaderMap::new();
        headers.insert("connect-protocol-version", HeaderValue::from_static("1"));
        if let Some(token) = config.access_token {
            headers.insert(
                "x-access-token",
                http::secret_header(token.expose(), "envd")?,
            );
        }
        if let Some(user) = &config.user {
            if user.is_empty() || user.contains([':', '\0']) {
                return Err(Error::invalid("envd", "configure", "invalid envd user"));
            }
            headers.insert(
                "authorization",
                http::secret_header(
                    &format!("Basic {}", STANDARD.encode(format!("{user}:"))),
                    "envd",
                )?,
            );
        }
        if let Some(id) = &config.sandbox_id {
            headers.insert(
                "e2b-sandbox-id",
                HeaderValue::from_str(id)
                    .map_err(|_| Error::invalid("envd", "configure", "invalid sandbox id"))?,
            );
            headers.insert("e2b-sandbox-port", HeaderValue::from_static("49983"));
        }
        Ok(Self {
            client,
            endpoint: http::endpoint(&config.endpoint, "envd")?,
            headers,
            request_timeout: config.request_timeout,
        })
    }

    async fn unary<Request: Message, Response: Message + Default>(
        &self,
        method: &str,
        request: Request,
    ) -> Result<Response> {
        let response = self
            .client
            .post(http::url(&self.endpoint, &["process.Process", method]))
            .headers(self.headers.clone())
            .header("content-type", "application/proto")
            .timeout(self.request_timeout)
            .body(request.encode_to_vec())
            .send()
            .await
            .map_err(|error| http::transport(error, "envd", method))?;
        if !response.status().is_success() {
            let kind = http::status_kind(response.status().as_u16());
            let body = response
                .bytes()
                .await
                .map_err(|error| http::transport(error, "envd", method))?;
            if let Ok(value) = serde_json::from_slice::<serde_json::Value>(&body)
                && let Some(code) = value.get("code").and_then(|code| code.as_str())
            {
                return Err(rpc_error(code, method));
            }
            return Err(Error::new(kind, "envd", method, "envd HTTP request failed"));
        }
        let body = response
            .bytes()
            .await
            .map_err(|error| http::transport(error, "envd", method))?;
        Response::decode(body).map_err(|_| protocol_error(method))
    }

    async fn stream<Request: Message>(
        &self,
        method: &'static str,
        request: Request,
    ) -> Result<RawStream> {
        let body = request.encode_to_vec();
        let length = u32::try_from(body.len())
            .map_err(|_| Error::invalid("envd", method, "request exceeds ConnectRPC frame size"))?;
        let mut framed = Vec::with_capacity(body.len() + 5);
        framed.push(0);
        framed.extend(length.to_be_bytes());
        framed.extend(body);
        let response = deadline(Some(self.request_timeout), "envd", method, async {
            let response = self
                .client
                .post(http::url(&self.endpoint, &["process.Process", method]))
                .headers(self.headers.clone())
                .header("content-type", "application/connect+proto")
                .body(framed)
                .send()
                .await
                .map_err(|error| http::transport(error, "envd", method))?;
            http::check(response, "envd", method)
        })
        .await?;
        let mut chunks = response.bytes_stream();
        Ok(Box::pin(async_stream::try_stream! {
            let mut buffer = BytesMut::new();
            let mut finished = false;
            while let Some(chunk) = chunks.next().await {
                buffer.extend_from_slice(&chunk.map_err(|error| http::transport(error, "envd", method))?);
                loop {
                    if buffer.len() < 5 { break; }
                    let flags = buffer[0];
                    let length = u32::from_be_bytes(buffer[1..5].try_into().map_err(|_| protocol_error(method))?) as usize;
                    if length > MAX_FRAME { Err(protocol_error(method))?; }
                    if buffer.len() < length + 5 { break; }
                    buffer.advance(5);
                    let payload = buffer.split_to(length).to_vec();
                    match flags {
                        0 => yield payload,
                        2 => {
                            let trailer: serde_json::Value = serde_json::from_slice(&payload).map_err(|_| protocol_error(method))?;
                            if let Some(error) = trailer.get("error") {
                                let code = error.get("code").and_then(|code| code.as_str()).ok_or_else(|| protocol_error(method))?;
                                Err(rpc_error(code, method))?;
                            }
                            if !buffer.is_empty() { Err(protocol_error(method))?; }
                            finished = true;
                            break;
                        }
                        _ => Err(protocol_error(method))?,
                    }
                }
                if finished { break; }
            }
            if !finished || !buffer.is_empty() {
                Err(Error::new(ErrorKind::Transport, "envd", method, "ConnectRPC stream ended without its trailer"))?;
            }
        }))
    }

    fn selector(&self, process: &ProcessRef, operation: &str) -> Result<protocol::ProcessSelector> {
        let pid = process
            .id
            .parse::<u32>()
            .ok()
            .filter(|pid| *pid > 0)
            .ok_or_else(|| Error::invalid("envd", operation, "invalid envd process id"))?;
        Ok(protocol::ProcessSelector {
            selector: Some(protocol::process_selector::Selector::Pid(pid)),
        })
    }

    async fn handle(
        &self,
        mut raw: RawStream,
        pty: Option<bool>,
        start: bool,
    ) -> Result<(ProcessRef, EventStream)> {
        let method = if start { "Start" } else { "Connect" };
        let first = deadline(Some(self.request_timeout), "envd", method, async {
            raw.next().await.ok_or_else(|| protocol_error(method))?
        })
        .await?;
        let decode = move |data: &[u8]| -> Result<protocol::ProcessEvent> {
            if start {
                protocol::StartResponse::decode(data)
                    .map_err(|_| protocol_error(method))?
                    .event
            } else {
                protocol::ConnectResponse::decode(data)
                    .map_err(|_| protocol_error(method))?
                    .event
            }
            .ok_or_else(|| protocol_error(method))
        };
        let pid = match decode(&first)?.event {
            Some(protocol::process_event::Event::Start(event)) if event.pid > 0 => event.pid,
            _ => return Err(protocol_error(method)),
        };
        let events = Box::pin(async_stream::try_stream! {
            let mut exited = false;
            while let Some(data) = raw.next().await {
                match decode(&data?)?.event {
                    Some(protocol::process_event::Event::Data(event)) => {
                        let (channel, data) = match event.output {
                            Some(protocol::process_event::data_event::Output::Stdout(data)) => (OutputChannel::Stdout, data),
                            Some(protocol::process_event::data_event::Output::Stderr(data)) => (OutputChannel::Stderr, data),
                            Some(protocol::process_event::data_event::Output::Pty(data)) => (OutputChannel::Terminal, data),
                            None => Err(protocol_error(method))?,
                        };
                        yield ProcessEvent::Output { channel, data };
                    }
                    Some(protocol::process_event::Event::End(event)) => {
                        if event.exit_code == 0 && event.error.as_ref().is_some_and(|error| !error.is_empty()) { Err(protocol_error(method))?; }
                        yield ProcessEvent::Completed(ProcessOutcome {
                            exit: Some(ExitStatus::code(event.exit_code)),
                            native_result: None,
                        });
                        exited = true;
                        break;
                    }
                    Some(protocol::process_event::Event::Keepalive(_)) => {},
                    _ => Err(protocol_error(method))?,
                }
            }
            if !exited { Err(Error::new(ErrorKind::Transport, "envd", method, "process stream ended before exit status"))?; }
        });
        Ok((ProcessRef::new("envd", pid.to_string(), pty), events))
    }

    async fn start_process(&self, request: SpawnRequest) -> Result<(ProcessRef, EventStream)> {
        let config = protocol::ProcessConfig {
            cmd: request.command.argv[0].clone(),
            args: request.command.argv[1..].to_vec(),
            envs: request.command.env.into_iter().collect(),
            cwd: request.command.cwd,
        };
        let message = protocol::StartRequest {
            process: Some(config),
            pty: request.pty.map(|size| protocol::Pty {
                size: Some(protocol::pty::Size {
                    cols: size.cols,
                    rows: size.rows,
                }),
            }),
            tag: None,
            stdin: Some(request.stdin),
        };
        self.handle(
            self.stream("Start", message).await?,
            Some(request.pty.is_some()),
            true,
        )
        .await
    }
}

#[async_trait]
impl ProcessManagerAccess for Envd {
    fn backend(&self) -> &'static str {
        "envd"
    }
    fn capabilities(&self) -> ProcessManagerCapabilities {
        ProcessManagerCapabilities {
            list: true,
            kill: true,
            stdin: true,
            close_stdin: true,
            binary_stdin: true,
            pty: true,
            resize: true,
            supported_signals: &[Signal::Terminate, Signal::Kill],
            detached: true,
            reconnect: true,
            ..Default::default()
        }
    }

    fn process_capabilities(&self, process: &ProcessRef) -> ProcessManagerCapabilities {
        let mut capabilities = ProcessManagerAccess::capabilities(self);
        // The protocol cannot infer stdin versus PTY routing from the PID returned by List.
        capabilities.stdin &= process.pty.is_some();
        capabilities.binary_stdin &= process.pty.is_some();
        capabilities.close_stdin &= process.pty == Some(false);
        capabilities.pty &= process.pty == Some(true);
        capabilities.resize &= process.pty == Some(true);
        capabilities
    }

    fn output_access(&self) -> Option<Arc<dyn OutputAccess>> {
        Some(Arc::new(self.clone()))
    }

    async fn probe(&self) -> Result<ManagerInfo> {
        let _: protocol::ListResponse = self.unary("List", protocol::ListRequest {}).await?;
        Ok(ManagerInfo {
            backend: "envd".into(),
            client_version: None,
            server_version: None,
        })
    }

    async fn list(&self) -> Result<Vec<ProcessInfo>> {
        let response: protocol::ListResponse = self.unary("List", protocol::ListRequest {}).await?;
        // List has no PTY metadata; unknown mode still permits identity-only operations.
        response
            .processes
            .into_iter()
            .map(|info| {
                if info.pid == 0 {
                    return Err(Error::new(
                        ErrorKind::Backend,
                        "envd",
                        "list",
                        "native List returned an invalid PID",
                    ));
                }
                Ok(ProcessInfo {
                    process: ProcessRef::new("envd", info.pid.to_string(), None),
                    state: ProcessState::Running,
                    outcome: None,
                    native_state: "listed".into(),
                })
            })
            .collect()
    }

    async fn kill(&self, process: &ProcessRef) -> Result<()> {
        self.signal(process, Signal::Kill).await
    }

    async fn spawn(&self, request: SpawnRequest) -> Result<SpawnedProcess> {
        let (reference, events) = self.start_process(request).await?;
        Ok(SpawnedProcess {
            reference: reference.clone(),
            initial_subscription: Some(OutputSubscription::new(reference, events)),
        })
    }

    async fn status(&self, process: &ProcessRef) -> Result<ProcessInfo> {
        let selector = self.selector(process, "status")?;
        let response: protocol::ListResponse = self.unary("List", protocol::ListRequest {}).await?;
        if response.processes.iter().any(|info| {
            selector.selector == Some(protocol::process_selector::Selector::Pid(info.pid))
        }) {
            Ok(ProcessInfo {
                process: process.clone(),
                state: ProcessState::Running,
                outcome: None,
                native_state: "listed".into(),
            })
        } else {
            Err(Error::new(
                ErrorKind::NotFound,
                "envd",
                "status",
                "process is not listed; completed exits are available on the native stream, not List",
            ))
        }
    }

    async fn wait(
        &self,
        process: &ProcessRef,
        timeout: Option<Duration>,
    ) -> Result<ProcessOutcome> {
        deadline(timeout, "envd", "wait", async {
            wait_events(self.subscribe(process).await?.events, "envd").await
        })
        .await
    }

    async fn send_input(&self, process: &ProcessRef, data: Vec<u8>) -> Result<()> {
        let input = match process.pty {
            Some(true) => protocol::process_input::Input::Pty(data),
            Some(false) => protocol::process_input::Input::Stdin(data),
            None => return Err(Error::unsupported("envd", "send_input")),
        };
        let _: protocol::SendInputResponse = self
            .unary(
                "SendInput",
                protocol::SendInputRequest {
                    process: Some(self.selector(process, "send_input")?),
                    input: Some(protocol::ProcessInput { input: Some(input) }),
                },
            )
            .await?;
        Ok(())
    }

    async fn close_stdin(&self, process: &ProcessRef) -> Result<()> {
        if process.pty != Some(false) {
            return Err(Error::unsupported("envd", "close_pty_stdin"));
        }
        let _: protocol::CloseStdinResponse = self
            .unary(
                "CloseStdin",
                protocol::CloseStdinRequest {
                    process: Some(self.selector(process, "close_stdin")?),
                },
            )
            .await?;
        Ok(())
    }

    async fn signal(&self, process: &ProcessRef, signal: Signal) -> Result<()> {
        let _: protocol::SendSignalResponse = self
            .unary(
                "SendSignal",
                protocol::SendSignalRequest {
                    process: Some(self.selector(process, "signal")?),
                    signal: signal.number(),
                },
            )
            .await?;
        Ok(())
    }

    async fn resize(&self, process: &ProcessRef, size: PtySize) -> Result<()> {
        if process.pty != Some(true) {
            return Err(Error::unsupported("envd", "resize_without_pty"));
        }
        let _: protocol::UpdateResponse = self
            .unary(
                "Update",
                protocol::UpdateRequest {
                    process: Some(self.selector(process, "resize")?),
                    pty: Some(protocol::Pty {
                        size: Some(protocol::pty::Size {
                            cols: size.cols,
                            rows: size.rows,
                        }),
                    }),
                },
            )
            .await?;
        Ok(())
    }
}

#[async_trait]
impl OutputAccess for Envd {
    fn backend(&self) -> &'static str {
        "envd"
    }
    fn capabilities(&self) -> OutputCapabilities {
        OutputCapabilities {
            subscribe: true,
            streaming_output: true,
            ..Default::default()
        }
    }
    async fn subscribe(&self, process: &ProcessRef) -> Result<OutputSubscription> {
        let selector = self.selector(process, "reconnect")?;
        let (reference, events) = self
            .handle(
                self.stream(
                    "Connect",
                    protocol::ConnectRequest {
                        process: Some(selector),
                    },
                )
                .await?,
                process.pty,
                false,
            )
            .await?;
        if reference.id != process.id {
            return Err(protocol_error("Connect"));
        }
        Ok(OutputSubscription::new(reference, events))
    }
}

#[async_trait]
impl ConnectionAccess for Envd {
    fn backend(&self) -> &'static str {
        "envd"
    }
    fn capabilities(&self) -> ConnectionCapabilities {
        ConnectionCapabilities {
            stdin: true,
            timeout: true,
            separate_stderr: true,
        }
    }

    async fn exec(&self, request: ExecRequest) -> Result<ExecResult> {
        deadline(request.timeout, "envd", "exec", async {
            let mut spawn = SpawnRequest::new(request.command);
            spawn.stdin = !request.stdin.is_empty();
            let (process, mut events) = self.start_process(spawn).await?;
            let write = async {
                if !request.stdin.is_empty() {
                    self.send_input(&process, request.stdin).await?;
                    self.close_stdin(&process).await?;
                }
                Ok::<_, Error>(())
            };
            let read = async {
                let mut stdout = Vec::new();
                let mut stderr = Vec::new();
                while let Some(event) = events.next().await {
                    match event? {
                        ProcessEvent::Output {
                            channel: OutputChannel::Stdout,
                            data,
                        } => stdout.extend(data),
                        ProcessEvent::Output {
                            channel: OutputChannel::Stderr,
                            data,
                        } => stderr.extend(data),
                        ProcessEvent::Completed(ProcessOutcome {
                            exit: Some(exit), ..
                        }) => {
                            return Ok(ExecResult {
                                stdout,
                                stderr,
                                exit,
                            });
                        }
                        _ => return Err(protocol_error("exec")),
                    }
                }
                Err(Error::new(
                    ErrorKind::Transport,
                    "envd",
                    "exec",
                    "process stream ended before exit status",
                ))
            };
            tokio::pin!(write, read);
            tokio::select! {
                result = &mut read => {
                    let result = result?;
                    match write.await {
                        Ok(()) => Ok(result),
                        Err(error) if error.kind == ErrorKind::NotFound => Ok(result),
                        Err(error) => Err(error),
                    }
                }
                written = &mut write => {
                    match written {
                        Ok(()) => read.await,
                        Err(error) if error.kind == ErrorKind::NotFound => {
                            match tokio::time::timeout(self.request_timeout, read).await {
                                Ok(result) => result,
                                Err(_) => Err(error),
                            }
                        }
                        Err(error) => Err(error),
                    }
                }
            }
        })
        .await
    }
}
