//! Third-party raw implementations exercise the public validation and dispatch boundary.

use std::{
    sync::{Arc, Mutex, Weak},
    time::Duration,
};

use async_trait::async_trait;
use futures::{StreamExt, executor::block_on, stream};
use opensol_core::{
    raw::{
        ConnectionAccess, OutputAccess, ProcessManagerAccess, SandboxManagerAccess, SpawnedProcess,
        TerminalAccess,
    },
    *,
};

#[derive(Clone, Default)]
struct Backend {
    calls: Arc<Mutex<Vec<&'static str>>>,
    reconnect: bool,
    input_constraints: Option<InputConstraints>,
    probe: bool,
}

struct ExtensionContext {
    _token: Arc<()>,
}

#[async_trait]
impl BackendAccess for ExtensionContext {
    fn backend(&self) -> &'static str {
        "extension"
    }
    fn capabilities(&self) -> BackendCapabilities {
        BackendCapabilities {
            sandboxes: Some(TargetRequirement::Forbidden),
            connection: Some(TargetRequirement::Forbidden),
            processes: Some(TargetRequirement::Forbidden),
            connection_dependency: ConnectionDependency::Forbidden,
        }
    }
    fn validate_open(&self, _api: Api, target: Option<&SandboxRef>) -> Result<()> {
        if target.is_some() {
            return Err(Error::invalid("extension", "open", "unexpected target"));
        }
        Ok(())
    }
    fn sandboxes(&self) -> Result<SandboxManager> {
        Ok(SandboxManager::new(Backend::default()))
    }
    async fn connection(&self, _target: Option<&SandboxRef>) -> Result<Connection> {
        Ok(Connection::new(Backend::default()))
    }
    async fn processes(&self, _target: Option<&SandboxRef>) -> Result<ProcessManager> {
        Ok(ProcessManager::new(Backend {
            reconnect: true,
            ..Default::default()
        }))
    }
}

fn context_with_lifetime_probe() -> (opensol_core::Backend, Weak<()>) {
    let token = Arc::new(());
    let weak = Arc::downgrade(&token);
    (
        opensol_core::Backend::new(ExtensionContext { _token: token }),
        weak,
    )
}

// Call only after dropping all other handles for this context. The weak probe observes
// ownership without inspecting client fields or depending on a particular Arc count.
fn assert_retains_context_without_cycle<T>(handle: T, weak: Weak<()>) {
    assert!(
        weak.upgrade().is_some(),
        "the sole handle retains its context"
    );
    drop(handle);
    assert!(
        weak.upgrade().is_none(),
        "dropping the last handle releases the context"
    );
}

#[test]
fn sandbox_manager_retains_its_context_without_cycles() {
    block_on(async {
        let (backend, weak) = context_with_lifetime_probe();
        let manager = SandboxManager::open(&backend).await.unwrap();
        drop(backend);
        assert_retains_context_without_cycle(manager, weak);
    });
}

#[test]
fn connection_retains_its_context_without_cycles() {
    block_on(async {
        let (backend, weak) = context_with_lifetime_probe();
        let connection = Connection::open(&backend, None).await.unwrap();
        drop(backend);
        assert_eq!(
            connection
                .exec(ExecRequest::new(Command::shell("true")))
                .await
                .unwrap()
                .exit
                .code,
            Some(7)
        );
        assert_retains_context_without_cycle(connection, weak);
    });
}

#[test]
fn process_manager_retains_its_context_without_cycles() {
    block_on(async {
        let (backend, weak) = context_with_lifetime_probe();
        let manager = ProcessManager::open(&backend, None, None).await.unwrap();
        drop(backend);
        assert_retains_context_without_cycle(manager, weak);
    });
}

#[test]
fn sandbox_retains_its_context_without_cycles() {
    block_on(async {
        let (backend, weak) = context_with_lifetime_probe();
        let sandbox = SandboxManager::open(&backend)
            .await
            .unwrap()
            .get(SandboxRef::new("extension", "id"));
        drop(backend);
        assert_retains_context_without_cycle(sandbox, weak);
    });
}

#[test]
fn process_retains_its_context_without_cycles() {
    block_on(async {
        let (backend, weak) = context_with_lifetime_probe();
        let process = ProcessManager::open(&backend, None, None)
            .await
            .unwrap()
            .get(ProcessRef::new("extension", "id", Some(false)));
        drop(backend);
        assert_retains_context_without_cycle(process, weak);
    });
}

#[test]
fn output_retains_its_context_without_cycles() {
    block_on(async {
        let (backend, weak) = context_with_lifetime_probe();
        let output = ProcessManager::open(&backend, None, None)
            .await
            .unwrap()
            .get(ProcessRef::new("extension", "id", Some(false)))
            .output();
        drop(backend);
        assert_retains_context_without_cycle(output, weak);
    });
}

#[test]
fn terminal_retains_its_context_without_cycles() {
    block_on(async {
        let (backend, weak) = context_with_lifetime_probe();
        let terminal = ProcessManager::open(&backend, None, None)
            .await
            .unwrap()
            .get(ProcessRef::new("extension", "id", Some(false)))
            .terminal();
        drop(backend);
        assert_retains_context_without_cycle(terminal, weak);
    });
}

#[test]
fn extracted_subscription_stream_retains_its_context_without_cycles() {
    block_on(async {
        let (backend, weak) = context_with_lifetime_probe();
        let mut events = ProcessManager::open(&backend, None, None)
            .await
            .unwrap()
            .get(ProcessRef::new("extension", "id", Some(false)))
            .output()
            .subscribe()
            .await
            .unwrap()
            .events;
        drop(backend);
        assert!(weak.upgrade().is_some());
        assert!(matches!(
            events.next().await,
            Some(Ok(ProcessEvent::Completed(_)))
        ));
        assert_retains_context_without_cycle(events, weak);
    });
}

impl Backend {
    fn entered<T>(&self, operation: &'static str) -> Result<T> {
        self.calls.lock().unwrap().push(operation);
        Err(Error::new(
            ErrorKind::Transport,
            "extension",
            operation,
            "completion unknown",
        ))
    }
}

#[async_trait]
impl ConnectionAccess for Backend {
    fn backend(&self) -> &'static str {
        "extension"
    }
    fn capabilities(&self) -> ConnectionCapabilities {
        ConnectionCapabilities {
            stdin: self.input_constraints,
            timeout: false,
            separate_stderr: true,
            cancellation: ExecCancellation::NoTargetTerminationGuarantee,
        }
    }
    async fn exec(&self, request: ExecRequest) -> Result<ExecResult> {
        if request.command.argv[0] == "transport" {
            return self.entered("exec");
        }
        self.calls.lock().unwrap().push("exec");
        Ok(ExecResult {
            stdout: vec![0, 0xff],
            stderr: vec![],
            exit: ExitStatus::code(7),
        })
    }
}

#[test]
fn connection_checks_shared_arguments_and_capabilities_before_dispatch() {
    block_on(async {
        let adapter = Backend::default();
        let client = Connection::new(adapter.clone());
        let mut environment = Command::shell("true");
        environment.env.insert("INVALID=KEY".into(), "value".into());
        for command in [
            Command {
                argv: vec![],
                cwd: None,
                env: Default::default(),
            },
            Command::new("", ["arg"]),
            Command::new("printf", ["a\0b"]),
            environment,
        ] {
            assert_eq!(
                client
                    .exec(ExecRequest::new(command))
                    .await
                    .unwrap_err()
                    .kind,
                ErrorKind::InvalidArgument
            );
        }
        let mut request = ExecRequest::new(Command::shell("true"));
        request.timeout = Some(Duration::ZERO);
        assert_eq!(
            client.exec(request.clone()).await.unwrap_err().kind,
            ErrorKind::InvalidArgument
        );
        request.timeout = Some(Duration::from_secs(1));
        assert_eq!(
            client.exec(request).await.unwrap_err().kind,
            ErrorKind::Unsupported
        );
        let mut request = ExecRequest::new(Command::shell("true"));
        request.stdin = vec![1];
        assert_eq!(
            client.exec(request).await.unwrap_err().kind,
            ErrorKind::Unsupported
        );
        assert!(adapter.calls.lock().unwrap().is_empty());
        let result = client
            .clone()
            .exec(ExecRequest::new(Command::shell("exit 7")))
            .await
            .unwrap();
        assert_eq!(
            (result.stdout, result.exit),
            (vec![0, 0xff], ExitStatus::code(7))
        );
        let error = client
            .exec(ExecRequest::new(Command::new("transport", ["secret"])))
            .await
            .unwrap_err();
        assert_eq!(
            error,
            Error::new(
                ErrorKind::Transport,
                "extension",
                "exec",
                "completion unknown"
            )
        );
        assert_eq!(
            *adapter.calls.lock().unwrap(),
            ["exec", "exec"],
            "uncertain operations must never retry"
        );
    });
}

#[async_trait]
impl SandboxManagerAccess for Backend {
    fn backend(&self) -> &'static str {
        "extension"
    }
    fn capabilities(&self) -> SandboxManagerCapabilities {
        SandboxManagerCapabilities {
            create: true,
            sandbox: SandboxCapabilities {
                inspect: true,
                ..Default::default()
            },
            ..Default::default()
        }
    }
    async fn create(&self, _spec: SandboxSpec) -> Result<SandboxRef> {
        self.entered("create")
    }
    async fn inspect(&self, _resource: &SandboxRef) -> Result<SandboxInfo> {
        self.entered("inspect")
    }
    async fn start(&self, _resource: &SandboxRef) -> Result<()> {
        self.entered("start")
    }
    async fn stop(&self, _resource: &SandboxRef) -> Result<()> {
        self.entered("stop")
    }
    async fn delete(&self, _resource: &SandboxRef) -> Result<()> {
        self.entered("delete")
    }
    async fn fork(&self, _resource: &SandboxRef) -> Result<SandboxRef> {
        self.entered("fork")
    }
}

#[test]
fn lifecycle_checks_reference_and_optional_operations_without_native_calls() {
    block_on(async {
        let adapter = Backend::default();
        let client = SandboxManager::new(adapter.clone());
        for resource in [
            SandboxRef::new("other", "id"),
            SandboxRef::new("extension", ""),
            SandboxRef::new("extension", "bad\0id"),
        ] {
            assert_eq!(
                client
                    .get(resource.clone())
                    .inspect()
                    .await
                    .unwrap_err()
                    .kind,
                ErrorKind::InvalidArgument
            );
            assert_eq!(
                client.get(resource.clone()).fork().await.unwrap_err().kind,
                ErrorKind::InvalidArgument
            );
        }
        let resource = SandboxRef::new("extension", "id");
        let sandbox = client.get(resource.clone());
        assert_eq!(sandbox.reference(), &resource);
        for error in [
            client.list().await.unwrap_err(),
            client.get(resource.clone()).start().await.unwrap_err(),
            client.get(resource.clone()).stop().await.unwrap_err(),
            client.get(resource.clone()).delete().await.unwrap_err(),
            client.get(resource.clone()).fork().await.unwrap_err(),
        ] {
            assert_eq!(error.kind, ErrorKind::Unsupported);
        }
        let invalid = CreateOptions {
            name: Some("bad\0name".into()),
            ..Default::default()
        };
        assert_eq!(
            client
                .create(SandboxSpec::new("extension", invalid, ()))
                .await
                .unwrap_err()
                .kind,
            ErrorKind::InvalidArgument
        );
        assert!(adapter.calls.lock().unwrap().is_empty());
        assert_eq!(
            client
                .create(SandboxSpec::new("extension", CreateOptions::default(), ()))
                .await
                .unwrap_err()
                .kind,
            ErrorKind::Transport
        );
        assert_eq!(
            client
                .clone()
                .get(resource.clone())
                .inspect()
                .await
                .unwrap_err()
                .kind,
            ErrorKind::Transport
        );
        assert_eq!(*adapter.calls.lock().unwrap(), ["create", "inspect"]);
    });
}

#[async_trait]
impl ProcessManagerAccess for Backend {
    fn backend(&self) -> &'static str {
        "extension"
    }
    fn capabilities(&self) -> ProcessManagerCapabilities {
        ProcessManagerCapabilities {
            probe: self.probe,
            spawn_modes: vec![
                SpawnMode {
                    pty: false,
                    stdin: false,
                },
                SpawnMode {
                    pty: false,
                    stdin: true,
                },
            ],
            process: ProcessCapabilities {
                input: Some(ProcessInputCapabilities {
                    constraints: self.input_constraints.unwrap_or(InputConstraints::BYTES),
                    close: true,
                }),
                resize: true,
                supported_signals: vec![Signal::Kill],
                reconnect: self.reconnect,
                ..Default::default()
            },
            output: OutputCapabilities {
                read: true,
                tail: true,
                read_channels: vec![OutputChannel::Combined],
                subscribe: SubscriptionSources {
                    initial: Some(initial_delivery()),
                    reconnect: self.reconnect.then(reconnect_delivery),
                },
            },
            ..Default::default()
        }
    }
    fn process_capabilities(&self, process: &ProcessRef) -> ProcessCapabilities {
        let mut capabilities = ProcessManagerAccess::capabilities(self).process;
        if process.pty == Some(true) {
            capabilities.input = None;
            capabilities.resize = false;
        }
        capabilities
    }
    fn output_access(&self) -> Option<Arc<dyn OutputAccess>> {
        self.reconnect
            .then(|| Arc::new(self.clone()) as Arc<dyn OutputAccess>)
    }
    async fn spawn(&self, _request: SpawnRequest) -> Result<SpawnedProcess> {
        self.calls.lock().unwrap().push("spawn");
        let reference = ProcessRef::new("extension", "task", Some(false));
        Ok(SpawnedProcess {
            reference: reference.clone(),
            initial_subscription: Some(OutputSubscription::new(
                reference,
                initial_delivery(),
                Box::pin(stream::iter([
                    Ok(ProcessEvent::Output {
                        channel: OutputChannel::Combined,
                        data: vec![0xff],
                    }),
                    Ok(ProcessEvent::Completed(ProcessOutcome {
                        exit: Some(ExitStatus::code(7)),
                        native_result: None,
                    })),
                ])),
            )),
        })
    }
    async fn list(&self) -> Result<Vec<ProcessInfo>> {
        self.entered("list")
    }
    async fn probe(&self) -> Result<ManagerInfo> {
        self.entered("probe")
    }
    async fn status(&self, _process: &ProcessRef) -> Result<ProcessInfo> {
        self.entered("status")
    }
    async fn wait(
        &self,
        _process: &ProcessRef,
        _timeout: Option<Duration>,
    ) -> Result<ProcessOutcome> {
        self.entered("wait")
    }
    async fn pause(&self, _process: &ProcessRef) -> Result<()> {
        self.entered("pause")
    }
    async fn start(&self, _process: &ProcessRef) -> Result<()> {
        self.entered("start")
    }
    async fn restart(&self, _process: &ProcessRef) -> Result<()> {
        self.entered("restart")
    }
    async fn kill(&self, _process: &ProcessRef) -> Result<()> {
        self.entered("kill")
    }
    async fn remove(&self, _process: &ProcessRef) -> Result<()> {
        self.entered("remove")
    }
    async fn send_input(&self, _process: &ProcessRef, _data: Vec<u8>) -> Result<()> {
        self.entered("send_input")
    }
    async fn close_stdin(&self, _process: &ProcessRef) -> Result<()> {
        self.entered("close_stdin")
    }
    async fn signal(&self, _process: &ProcessRef, _signal: Signal) -> Result<()> {
        self.entered("signal")
    }
    async fn resize(&self, _process: &ProcessRef, _size: PtySize) -> Result<()> {
        self.entered("resize")
    }
}

#[async_trait]
impl OutputAccess for Backend {
    fn backend(&self) -> &'static str {
        "extension"
    }
    fn capabilities(&self, _process: &ProcessRef) -> OutputCapabilities {
        OutputCapabilities {
            read: true,
            tail: true,
            read_channels: vec![OutputChannel::Combined],
            subscribe: SubscriptionSources {
                initial: None,
                reconnect: self.reconnect.then(reconnect_delivery),
            },
        }
    }
    async fn read(
        &self,
        _process: &ProcessRef,
        _cursor: Option<&OutputCursor>,
        _direction: ReadDirection,
        _max_bytes: usize,
    ) -> Result<OutputPage> {
        self.entered("read_output")
    }
    async fn tail(&self, _process: &ProcessRef, _max_bytes: usize) -> Result<OutputPage> {
        self.entered("tail_output")
    }
    async fn subscribe(&self, process: &ProcessRef) -> Result<OutputSubscription> {
        self.calls.lock().unwrap().push("reconnect");
        Ok(OutputSubscription::new(
            process.clone(),
            reconnect_delivery(),
            Box::pin(stream::iter([Ok(ProcessEvent::Completed(
                ProcessOutcome {
                    exit: None,
                    native_result: Some("Killed".into()),
                },
            ))])),
        ))
    }
}

#[test]
fn terminal_input_requires_pty_support_without_changing_reference_identity() {
    block_on(async {
        let adapter = Backend::default();
        let manager = ProcessManager::new(adapter.clone());
        let terminal = ProcessRef::new("extension", "task", Some(true));
        let bound = manager.get(terminal.clone());
        assert_eq!(bound.reference(), &terminal);
        let capabilities = bound.capabilities();
        assert!(capabilities.input.is_none() && !capabilities.resize);
        assert!(manager.capabilities().process.input.unwrap().close);
        assert_eq!(
            bound.send_input(b"text".to_vec()).await.unwrap_err().kind,
            ErrorKind::Unsupported
        );
        assert_eq!(
            bound.close_stdin().await.unwrap_err().kind,
            ErrorKind::Unsupported
        );
        assert_eq!(
            bound
                .resize(PtySize { cols: 80, rows: 24 })
                .await
                .unwrap_err()
                .kind,
            ErrorKind::Unsupported
        );
        assert!(
            adapter.calls.lock().unwrap().is_empty(),
            "unsupported PTY input must not enter the adapter"
        );
        assert_eq!(
            bound.inspect().await.unwrap_err().kind,
            ErrorKind::Transport
        );
        let ordinary = ProcessRef::new("extension", "task", Some(false));
        assert_eq!(
            manager
                .get(ordinary)
                .send_input(b"text".to_vec())
                .await
                .unwrap_err()
                .kind,
            ErrorKind::Transport
        );
        assert_eq!(*adapter.calls.lock().unwrap(), ["status", "send_input"]);
    });
}

#[test]
fn byte_input_needs_no_mode_hint_unless_the_adapter_requires_one() {
    block_on(async {
        let adapter = Backend::default();
        let manager = ProcessManager::new(adapter.clone());
        assert_eq!(
            manager
                .capabilities()
                .process
                .input
                .unwrap()
                .constraints
                .content,
            InputContent::Bytes
        );
        let process = manager.get(ProcessRef::new("extension", "task", None));
        // This adapter needs no mode hint and may accept payloads that Pueue cannot express.
        for data in [b"text".to_vec(), vec![0xff], vec![0]] {
            assert_eq!(
                process.send_input(data).await.unwrap_err().kind,
                ErrorKind::Transport
            );
        }
        assert_eq!(
            process.close_stdin().await.unwrap_err().kind,
            ErrorKind::Transport
        );
        assert_eq!(
            process
                .resize(PtySize { cols: 80, rows: 24 })
                .await
                .unwrap_err()
                .kind,
            ErrorKind::Transport
        );
        assert_eq!(
            *adapter.calls.lock().unwrap(),
            [
                "send_input",
                "send_input",
                "send_input",
                "close_stdin",
                "resize"
            ]
        );
    });
}

#[test]
fn bound_operations_validate_before_dispatch_and_keep_the_initial_subscription() {
    block_on(async {
        let adapter = Backend::default();
        let manager = ProcessManager::new(adapter.clone());
        let reference = ProcessRef::new("extension", "task", None);
        let unknown = manager.get(reference.clone());
        assert!(
            adapter.calls.lock().unwrap().is_empty(),
            "binding cannot perform I/O"
        );
        let process = manager.get(ProcessRef::new("extension", "task", Some(false)));
        for error in [
            process.pause().await.unwrap_err(),
            process.start().await.unwrap_err(),
            process.restart().await.unwrap_err(),
            process.kill().await.unwrap_err(),
            process.remove().await.unwrap_err(),
            process.output().tail(64).await.unwrap_err(),
            process.terminal().screen().await.unwrap_err(),
            process
                .output()
                .read(None, opensol_core::ReadDirection::Forward, 64)
                .await
                .unwrap_err(),
            process.signal(Signal::Interrupt).await.unwrap_err(),
            manager.list().await.unwrap_err(),
            manager.probe().await.unwrap_err(),
        ] {
            assert_eq!(error.kind, ErrorKind::Unsupported);
        }
        assert!(
            matches!(process.output().subscribe().await, Err(error) if error.kind == ErrorKind::Unsupported)
        );
        for limit in [0, MAX_OUTPUT_LIMIT + 1] {
            assert_eq!(
                process
                    .output()
                    .read(None, opensol_core::ReadDirection::Forward, limit)
                    .await
                    .unwrap_err()
                    .kind,
                ErrorKind::InvalidArgument
            );
        }
        assert_eq!(
            process.wait(Some(Duration::ZERO)).await.unwrap_err().kind,
            ErrorKind::InvalidArgument
        );
        assert_eq!(
            process
                .resize(PtySize { cols: 0, rows: 24 })
                .await
                .unwrap_err()
                .kind,
            ErrorKind::InvalidArgument
        );
        let wrong = manager.get(ProcessRef::new("other", "task", Some(false)));
        assert_eq!(
            wrong.inspect().await.unwrap_err().kind,
            ErrorKind::InvalidArgument
        );
        assert_eq!(
            wrong.signal(Signal::Kill).await.unwrap_err().kind,
            ErrorKind::InvalidArgument
        );
        assert!(
            matches!(wrong.output().subscribe().await, Err(error) if error.kind == ErrorKind::InvalidArgument)
        );
        let mut request = SpawnRequest::new(Command::shell("true"));
        request.pty = Some(PtySize { cols: 0, rows: 24 });
        assert!(
            matches!(manager.spawn(request.clone()).await, Err(error) if error.kind == ErrorKind::InvalidArgument)
        );
        request.pty = Some(PtySize { cols: 80, rows: 24 });
        assert!(
            matches!(manager.spawn(request).await, Err(error) if error.kind == ErrorKind::Unsupported)
        );
        assert!(adapter.calls.lock().unwrap().is_empty());
        let bound = manager
            .spawn(SpawnRequest::new(Command::shell("true")))
            .await
            .unwrap();
        assert_eq!(
            bound.reference(),
            &ProcessRef::new("extension", "task", Some(false))
        );
        assert_eq!(*adapter.calls.lock().unwrap(), ["spawn"]);
        drop(manager);
        let output = bound.output();
        assert_eq!(
            output.capabilities().subscribe.initial,
            Some(initial_delivery())
        );
        let mut subscription = bound.clone().output().subscribe().await.unwrap();
        assert_eq!(subscription.capabilities(), &initial_delivery());
        assert!(output.capabilities().subscribe.initial.is_none());
        assert!(
            matches!(output.subscribe().await, Err(error) if error.kind == ErrorKind::Unsupported)
        );
        assert_eq!(*adapter.calls.lock().unwrap(), ["spawn"]);
        assert_eq!(
            subscription.events.next().await.unwrap().unwrap(),
            ProcessEvent::Output {
                channel: OutputChannel::Combined,
                data: vec![0xff]
            }
        );
        assert_eq!(
            wait_events(subscription.events, "extension").await.unwrap(),
            ProcessOutcome {
                exit: Some(ExitStatus::code(7)),
                native_result: None,
            }
        );
        assert_eq!(
            bound.inspect().await.unwrap_err().kind,
            ErrorKind::Transport
        );
        assert_eq!(
            bound.send_input(b"text".to_vec()).await.unwrap_err().kind,
            ErrorKind::Transport
        );
        assert_eq!(
            unknown.signal(Signal::Kill).await.unwrap_err().kind,
            ErrorKind::Transport
        );
        assert_eq!(
            *adapter.calls.lock().unwrap(),
            ["spawn", "status", "send_input", "signal"]
        );
    });
}

#[test]
fn spawned_process_views_share_the_initial_stream_before_native_reconnect() {
    block_on(async {
        let adapter = Backend {
            reconnect: true,
            ..Default::default()
        };
        let process = ProcessManager::new(adapter.clone())
            .spawn(SpawnRequest::new(Command::shell("true")))
            .await
            .unwrap();
        let first = process.output();
        let second = process.clone().output();
        drop(process);
        let (initial, extra) = futures::join!(first.subscribe(), second.subscribe());
        assert_eq!(
            wait_events(initial.unwrap().events, "extension")
                .await
                .unwrap()
                .exit
                .unwrap()
                .code,
            Some(7)
        );
        assert_eq!(
            wait_events(extra.unwrap().events, "extension")
                .await
                .unwrap()
                .native_result
                .as_deref(),
            Some("Killed")
        );
        assert_eq!(*adapter.calls.lock().unwrap(), ["spawn", "reconnect"]);
    });
}

/// An upper bound may include restart while an individual execution is immutable.
struct MixedExecutionManager {
    immutable: bool,
}

#[async_trait]
impl ProcessManagerAccess for MixedExecutionManager {
    fn backend(&self) -> &'static str {
        "extension"
    }
    fn capabilities(&self) -> ProcessManagerCapabilities {
        let mut capabilities = ProcessManagerAccess::capabilities(&Backend::default());
        capabilities.process.restart = true;
        capabilities
    }
    fn process_capabilities(&self, _process: &ProcessRef) -> ProcessCapabilities {
        let mut capabilities = self.capabilities().process;
        capabilities.restart = !self.immutable;
        capabilities
    }
    async fn spawn(&self, request: SpawnRequest) -> Result<SpawnedProcess> {
        ProcessManagerAccess::spawn(&Backend::default(), request).await
    }
    async fn status(&self, process: &ProcessRef) -> Result<ProcessInfo> {
        ProcessManagerAccess::status(&Backend::default(), process).await
    }
    async fn wait(
        &self,
        process: &ProcessRef,
        timeout: Option<Duration>,
    ) -> Result<ProcessOutcome> {
        ProcessManagerAccess::wait(&Backend::default(), process, timeout).await
    }
}

#[test]
fn initial_stream_requires_an_immutable_execution_within_the_declared_upper_bound() {
    block_on(async {
        for immutable in [true, false] {
            let manager = ProcessManager::new(MixedExecutionManager { immutable });
            assert!(manager.capabilities().process.restart);
            let result = manager
                .spawn(SpawnRequest::new(Command::shell("true")))
                .await;
            if immutable {
                let process = result.unwrap();
                assert!(!process.capabilities().restart);
                assert_eq!(
                    process.output().capabilities().subscribe.initial,
                    Some(initial_delivery())
                );
                let subscription = process.output().subscribe().await.unwrap();
                assert_eq!(subscription.capabilities(), &initial_delivery());
            } else {
                assert!(matches!(result, Err(error) if error.kind == ErrorKind::Backend));
            }
        }
    });
}

/// Supplies an actual native description independently of the advertised source. Deliberate
/// mismatches exercise the public adapter-contract errors, without inspecting private guards.
#[derive(Clone)]
struct SourceDescriptions {
    initial: bool,
    declared: Option<SubscriptionDelivery>,
    actual: SubscriptionDelivery,
    calls: Arc<Mutex<Vec<&'static str>>>,
}

impl SourceDescriptions {
    fn new(
        initial: bool,
        declared: Option<SubscriptionDelivery>,
        actual: SubscriptionDelivery,
    ) -> Self {
        Self {
            initial,
            declared,
            actual,
            calls: Arc::default(),
        }
    }
}

#[async_trait]
impl ProcessManagerAccess for SourceDescriptions {
    fn backend(&self) -> &'static str {
        "extension"
    }
    fn capabilities(&self) -> ProcessManagerCapabilities {
        let mut capabilities = ProcessManagerAccess::capabilities(&Backend::default());
        capabilities.process.reconnect = !self.initial && self.declared.is_some();
        capabilities.output = OutputCapabilities {
            subscribe: SubscriptionSources {
                initial: if self.initial {
                    self.declared.clone()
                } else {
                    None
                },
                reconnect: if self.initial {
                    None
                } else {
                    self.declared.clone()
                },
            },
            ..Default::default()
        };
        capabilities
    }
    fn output_access(&self) -> Option<Arc<dyn OutputAccess>> {
        Some(Arc::new(self.clone()))
    }
    async fn spawn(&self, _request: SpawnRequest) -> Result<SpawnedProcess> {
        self.calls.lock().unwrap().push("spawn");
        let reference = ProcessRef::new("extension", "task", Some(false));
        Ok(SpawnedProcess {
            initial_subscription: self
                .initial
                .then(|| described_subscription(&reference, self.actual.clone())),
            reference,
        })
    }
    async fn status(&self, process: &ProcessRef) -> Result<ProcessInfo> {
        ProcessManagerAccess::status(&Backend::default(), process).await
    }
    async fn wait(
        &self,
        process: &ProcessRef,
        timeout: Option<Duration>,
    ) -> Result<ProcessOutcome> {
        ProcessManagerAccess::wait(&Backend::default(), process, timeout).await
    }
}

#[async_trait]
impl OutputAccess for SourceDescriptions {
    fn backend(&self) -> &'static str {
        "extension"
    }
    fn capabilities(&self, _process: &ProcessRef) -> OutputCapabilities {
        let mut capabilities = ProcessManagerAccess::capabilities(self).output;
        capabilities.subscribe.initial = None;
        capabilities
    }
    async fn subscribe(&self, process: &ProcessRef) -> Result<OutputSubscription> {
        self.calls.lock().unwrap().push("subscribe_output");
        Ok(described_subscription(process, self.actual.clone()))
    }
}

fn described_subscription(
    reference: &ProcessRef,
    delivery: SubscriptionDelivery,
) -> OutputSubscription {
    let mut events = Vec::new();
    if let Some(&channel) = delivery.channels.first() {
        events.push(Ok(ProcessEvent::Output {
            channel,
            data: vec![0xff],
        }));
    }
    events.push(Ok(ProcessEvent::Completed(ProcessOutcome {
        exit: Some(ExitStatus::code(7)),
        native_result: None,
    })));
    OutputSubscription::new(reference.clone(), delivery, Box::pin(stream::iter(events)))
}

#[test]
fn spawn_rejects_an_undeclared_initial_subscription() {
    block_on(async {
        let adapter = SourceDescriptions::new(true, None, initial_delivery());
        let manager = ProcessManager::new(adapter.clone());
        let error = manager
            .spawn(SpawnRequest::new(Command::shell("true")))
            .await
            .err()
            .expect("undeclared initial stream must fail");
        assert_eq!(error.kind, ErrorKind::Backend);
        assert_eq!(error.operation, "spawn");
        assert_eq!(*adapter.calls.lock().unwrap(), ["spawn"]);
    });
}

#[test]
fn subscriptions_reject_delivery_outside_the_declared_source() {
    block_on(async {
        let declared = SubscriptionDelivery {
            channels: vec![OutputChannel::Stdout, OutputChannel::Stderr],
            historical_output: false,
            streaming_output: false,
            completion: CompletionKind::NativeTask,
        };
        let incompatible = [
            (
                "channel",
                SubscriptionDelivery {
                    channels: vec![OutputChannel::Terminal],
                    ..declared.clone()
                },
            ),
            (
                "history",
                SubscriptionDelivery {
                    historical_output: true,
                    ..declared.clone()
                },
            ),
            (
                "streaming",
                SubscriptionDelivery {
                    streaming_output: true,
                    ..declared.clone()
                },
            ),
            (
                "completion",
                SubscriptionDelivery {
                    completion: CompletionKind::OsExit,
                    ..declared.clone()
                },
            ),
        ];
        for initial in [true, false] {
            for (name, actual) in &incompatible {
                let adapter =
                    SourceDescriptions::new(initial, Some(declared.clone()), actual.clone());
                let manager = ProcessManager::new(adapter.clone());
                let spawned = manager
                    .spawn(SpawnRequest::new(Command::shell("true")))
                    .await;
                let error = if initial {
                    spawned
                        .err()
                        .expect("incompatible initial stream must fail")
                } else {
                    spawned
                        .unwrap()
                        .output()
                        .subscribe()
                        .await
                        .err()
                        .expect("incompatible reconnect must fail")
                };
                assert_eq!(error.kind, ErrorKind::Backend, "{name}, initial={initial}");
                assert_eq!(
                    error.operation,
                    if initial { "spawn" } else { "subscribe_output" }
                );
                assert_eq!(
                    *adapter.calls.lock().unwrap(),
                    if initial {
                        vec!["spawn"]
                    } else {
                        vec!["spawn", "subscribe_output"]
                    }
                );
            }
        }
    });
}

#[test]
fn subscriptions_accept_the_declared_delivery_and_legal_subsets() {
    block_on(async {
        let declared = SubscriptionDelivery {
            channels: vec![OutputChannel::Stdout, OutputChannel::Stderr],
            historical_output: true,
            streaming_output: true,
            completion: CompletionKind::NativeTask,
        };
        let deliveries = [
            declared.clone(),
            SubscriptionDelivery {
                channels: vec![OutputChannel::Stdout],
                historical_output: false,
                streaming_output: false,
                completion: CompletionKind::NativeTask,
            },
            reconnect_delivery(),
        ];
        for initial in [true, false] {
            for actual in &deliveries {
                let adapter =
                    SourceDescriptions::new(initial, Some(declared.clone()), actual.clone());
                let process = ProcessManager::new(adapter.clone())
                    .spawn(SpawnRequest::new(Command::shell("true")))
                    .await
                    .unwrap();
                let mut subscription = process.output().subscribe().await.unwrap();
                assert_eq!(subscription.capabilities(), actual);
                if let Some(&channel) = actual.channels.first() {
                    assert_eq!(
                        subscription.events.next().await.unwrap().unwrap(),
                        ProcessEvent::Output {
                            channel,
                            data: vec![0xff]
                        }
                    );
                }
                assert_eq!(
                    subscription.events.next().await.unwrap().unwrap(),
                    ProcessEvent::Completed(ProcessOutcome {
                        exit: Some(ExitStatus::code(7)),
                        native_result: None
                    })
                );
                assert_eq!(subscription.capabilities(), actual);
                assert_eq!(
                    *adapter.calls.lock().unwrap(),
                    if initial {
                        vec!["spawn"]
                    } else {
                        vec!["spawn", "subscribe_output"]
                    }
                );
            }
        }
    });
}

#[test]
fn additional_subscriptions_use_native_reconnect_and_preserve_identity() {
    block_on(async {
        let adapter = Backend {
            reconnect: true,
            ..Default::default()
        };
        let manager = ProcessManager::new(adapter.clone());
        let reference = ProcessRef::new("extension", "task", None);
        let process = manager.get(reference.clone());
        drop(manager);
        assert!(adapter.calls.lock().unwrap().is_empty());
        let handle = process.output().subscribe().await.unwrap();
        assert_eq!(process.reference(), &reference);
        assert_eq!(
            wait_events(handle.events, "extension").await.unwrap(),
            ProcessOutcome {
                exit: None,
                native_result: Some("Killed".into()),
            }
        );
        assert_eq!(*adapter.calls.lock().unwrap(), ["reconnect"]);
    });
}

#[test]
fn input_domains_and_limits_guard_both_exec_and_process_calls() {
    block_on(async {
        for content in [InputContent::Bytes, InputContent::Utf8WithoutNul] {
            let adapter = Backend {
                input_constraints: Some(InputConstraints {
                    content,
                    max_bytes: std::num::NonZeroUsize::new(3),
                }),
                probe: true,
                ..Default::default()
            };
            let manager = ProcessManager::new(adapter.clone());
            let process = manager.get(ProcessRef::new("extension", "task", Some(false)));
            let connection = Connection::new(adapter.clone());
            for data in [vec![0xff], vec![0], b"abc".to_vec(), b"abcd".to_vec()] {
                let invalid_content =
                    content == InputContent::Utf8WithoutNul && data != b"abc" && data != b"abcd";
                let expected = if data.len() > 3 {
                    ErrorKind::InvalidArgument
                } else if invalid_content {
                    ErrorKind::Unsupported
                } else {
                    ErrorKind::Transport
                };
                let before = adapter.calls.lock().unwrap().len();
                assert_eq!(
                    process.send_input(data.clone()).await.unwrap_err().kind,
                    expected
                );
                let mut request = ExecRequest::new(Command::new("transport", ["secret"]));
                request.stdin = data;
                assert_eq!(connection.exec(request).await.unwrap_err().kind, expected);
                assert_eq!(
                    adapter.calls.lock().unwrap().len() - before,
                    if expected == ErrorKind::Transport {
                        2
                    } else {
                        0
                    }
                );
            }
            assert_eq!(
                manager.probe().await.unwrap_err().kind,
                ErrorKind::Transport
            );
            assert_eq!(adapter.calls.lock().unwrap().last(), Some(&"probe"));
        }
    });
}

/// A third party can expose native terminal text without implementing task control or bytes.
#[derive(Clone, Default)]
struct TerminalOnly(Arc<Mutex<Vec<&'static str>>>);
#[async_trait]
impl TerminalAccess for TerminalOnly {
    fn backend(&self) -> &'static str {
        "terminal-only"
    }
    fn capabilities(&self, _process: &ProcessRef) -> TerminalCapabilities {
        TerminalCapabilities {
            tail: true,
            screen: true,
            ..Default::default()
        }
    }
    async fn tail(&self, _process: &ProcessRef, lines: usize) -> Result<TerminalPage> {
        self.0.lock().unwrap().push("tail");
        Ok(TerminalPage {
            lines: vec!["kept  \r\t\u{2028}".into(); lines.min(2)],
            before: None,
            after: None,
            at_head: Some(lines >= 2),
            at_tail: true,
        })
    }
}

fn initial_delivery() -> SubscriptionDelivery {
    SubscriptionDelivery {
        channels: vec![OutputChannel::Combined],
        historical_output: false,
        streaming_output: false,
        completion: CompletionKind::NativeTask,
    }
}

fn reconnect_delivery() -> SubscriptionDelivery {
    SubscriptionDelivery {
        channels: vec![],
        historical_output: false,
        streaming_output: false,
        completion: CompletionKind::NativeTask,
    }
}

/// Native view methods accept both identities; only the local capability hook restricts one.
/// Recording the reference at I/O distinguishes public guard checks from adapter rejection.
#[derive(Clone, Default)]
struct ReferenceViews(Arc<Mutex<Vec<(&'static str, ProcessRef)>>>);

impl ReferenceViews {
    fn record(&self, operation: &'static str, process: &ProcessRef) {
        self.0.lock().unwrap().push((operation, process.clone()));
    }
    fn output_page() -> OutputPage {
        OutputPage {
            chunks: vec![OutputChunk {
                channel: OutputChannel::Combined,
                data: vec![0xff],
            }],
            before: None,
            after: None,
            at_head: Some(true),
            at_tail: true,
        }
    }
    fn terminal_page() -> TerminalPage {
        TerminalPage {
            lines: vec!["view".into()],
            before: None,
            after: None,
            at_head: Some(true),
            at_tail: true,
        }
    }
}

#[async_trait]
impl ProcessManagerAccess for ReferenceViews {
    fn backend(&self) -> &'static str {
        "extension"
    }
    fn capabilities(&self) -> ProcessManagerCapabilities {
        let mut capabilities = ProcessManagerAccess::capabilities(&Backend {
            reconnect: true,
            ..Default::default()
        });
        capabilities.terminal = TerminalCapabilities {
            read: true,
            tail: true,
            screen: true,
        };
        capabilities
    }
    fn output_access(&self) -> Option<Arc<dyn OutputAccess>> {
        Some(Arc::new(self.clone()))
    }
    fn terminal_access(&self) -> Option<Arc<dyn TerminalAccess>> {
        Some(Arc::new(self.clone()))
    }
    async fn spawn(&self, request: SpawnRequest) -> Result<SpawnedProcess> {
        ProcessManagerAccess::spawn(&Backend::default(), request).await
    }
    async fn status(&self, process: &ProcessRef) -> Result<ProcessInfo> {
        ProcessManagerAccess::status(&Backend::default(), process).await
    }
    async fn wait(
        &self,
        process: &ProcessRef,
        timeout: Option<Duration>,
    ) -> Result<ProcessOutcome> {
        ProcessManagerAccess::wait(&Backend::default(), process, timeout).await
    }
}

#[async_trait]
impl OutputAccess for ReferenceViews {
    fn backend(&self) -> &'static str {
        "extension"
    }
    fn capabilities(&self, process: &ProcessRef) -> OutputCapabilities {
        if process.id != "available" {
            return OutputCapabilities::default();
        }
        let mut capabilities = ProcessManagerAccess::capabilities(self).output;
        capabilities.subscribe.initial = None;
        capabilities
    }
    async fn read(
        &self,
        process: &ProcessRef,
        _cursor: Option<&OutputCursor>,
        _direction: ReadDirection,
        _max_bytes: usize,
    ) -> Result<OutputPage> {
        self.record("read_output", process);
        Ok(Self::output_page())
    }
    async fn tail(&self, process: &ProcessRef, _max_bytes: usize) -> Result<OutputPage> {
        self.record("tail_output", process);
        Ok(Self::output_page())
    }
    async fn subscribe(&self, process: &ProcessRef) -> Result<OutputSubscription> {
        self.record("subscribe_output", process);
        Ok(described_subscription(process, reconnect_delivery()))
    }
}

#[async_trait]
impl TerminalAccess for ReferenceViews {
    fn backend(&self) -> &'static str {
        "extension"
    }
    fn capabilities(&self, process: &ProcessRef) -> TerminalCapabilities {
        if process.id == "available" {
            ProcessManagerAccess::capabilities(self).terminal
        } else {
            TerminalCapabilities::default()
        }
    }
    async fn read(
        &self,
        process: &ProcessRef,
        _cursor: Option<&TerminalCursor>,
        _direction: ReadDirection,
        _lines: usize,
    ) -> Result<TerminalPage> {
        self.record("read_terminal", process);
        Ok(Self::terminal_page())
    }
    async fn tail(&self, process: &ProcessRef, _lines: usize) -> Result<TerminalPage> {
        self.record("tail_terminal", process);
        Ok(Self::terminal_page())
    }
    async fn screen(&self, process: &ProcessRef) -> Result<TerminalScreen> {
        self.record("terminal_screen", process);
        Ok(TerminalScreen {
            cols: 4,
            rows: 1,
            lines: vec!["view".into()],
        })
    }
}

#[test]
fn bound_and_independent_views_use_reference_capabilities_before_native_io() {
    block_on(async {
        for independent in [false, true] {
            let adapter = ReferenceViews::default();
            let manager = ProcessManager::new(adapter.clone());
            for (id, supported) in [("available", true), ("unavailable", false)] {
                let reference = ProcessRef::new("extension", id, None);
                let before = adapter.0.lock().unwrap().len();
                let (output, terminal) = if independent {
                    (
                        Output::new(adapter.clone(), reference.clone()),
                        Terminal::new(adapter.clone(), reference.clone()),
                    )
                } else {
                    let process = manager.get(reference.clone());
                    (process.output(), process.terminal())
                };
                assert_eq!(
                    output.capabilities(),
                    OutputCapabilities {
                        read: supported,
                        tail: supported,
                        read_channels: if supported {
                            vec![OutputChannel::Combined]
                        } else {
                            vec![]
                        },
                        subscribe: SubscriptionSources {
                            initial: None,
                            reconnect: supported.then(reconnect_delivery)
                        },
                    }
                );
                assert_eq!(
                    terminal.capabilities(),
                    TerminalCapabilities {
                        read: supported,
                        tail: supported,
                        screen: supported
                    }
                );
                assert_eq!(
                    adapter.0.lock().unwrap().len(),
                    before,
                    "binding and capability queries must stay local"
                );
                if supported {
                    assert_eq!(
                        output
                            .read(None, ReadDirection::Forward, 64)
                            .await
                            .unwrap()
                            .chunks[0]
                            .data,
                        [0xff]
                    );
                    assert_eq!(
                        output.tail(64).await.unwrap().chunks[0].channel,
                        OutputChannel::Combined
                    );
                    let subscription = output.subscribe().await.unwrap();
                    assert_eq!(
                        wait_events(subscription.events, "extension")
                            .await
                            .unwrap()
                            .exit
                            .unwrap()
                            .code,
                        Some(7)
                    );
                    assert_eq!(
                        terminal
                            .read(None, ReadDirection::Forward, 1)
                            .await
                            .unwrap()
                            .lines,
                        ["view"]
                    );
                    assert_eq!(terminal.tail(1).await.unwrap().lines, ["view"]);
                    let screen = terminal.screen().await.unwrap();
                    assert_eq!(
                        (screen.cols, screen.rows, screen.lines),
                        (4, 1, vec!["view".into()])
                    );
                    let expected: Vec<_> = [
                        "read_output",
                        "tail_output",
                        "subscribe_output",
                        "read_terminal",
                        "tail_terminal",
                        "terminal_screen",
                    ]
                    .into_iter()
                    .map(|operation| (operation, reference.clone()))
                    .collect();
                    assert_eq!(&adapter.0.lock().unwrap()[before..], expected.as_slice());
                } else {
                    for error in [
                        output
                            .read(None, ReadDirection::Forward, 64)
                            .await
                            .unwrap_err(),
                        output.tail(64).await.unwrap_err(),
                        output
                            .subscribe()
                            .await
                            .err()
                            .expect("disabled subscribe must fail"),
                        terminal
                            .read(None, ReadDirection::Forward, 1)
                            .await
                            .unwrap_err(),
                        terminal.tail(1).await.unwrap_err(),
                        terminal.screen().await.unwrap_err(),
                    ] {
                        assert_eq!(
                            error.kind,
                            ErrorKind::Unsupported,
                            "{id}, independent={independent}"
                        );
                    }
                    assert_eq!(
                        adapter.0.lock().unwrap().len(),
                        before,
                        "disabled operations must not enter raw I/O"
                    );
                }
            }
        }
    });
}

#[test]
fn representation_extensions_bind_and_validate_independently_of_task_control() {
    block_on(async {
        let adapter = TerminalOnly::default();
        let terminal = Terminal::new(
            adapter.clone(),
            ProcessRef::new("terminal-only", "task", None),
        );
        assert!(terminal.clone().capabilities().tail);
        assert!(!terminal.capabilities().read);
        assert!(adapter.0.lock().unwrap().is_empty());
        for lines in [0, MAX_TERMINAL_LINES + 1] {
            assert_eq!(
                terminal.tail(lines).await.unwrap_err().kind,
                ErrorKind::InvalidArgument
            );
            assert_eq!(
                terminal
                    .read(None, ReadDirection::Backward, lines)
                    .await
                    .unwrap_err()
                    .kind,
                ErrorKind::InvalidArgument
            );
        }
        assert_eq!(
            terminal
                .read(None, ReadDirection::Backward, 1)
                .await
                .unwrap_err()
                .kind,
            ErrorKind::Unsupported
        );
        assert!(adapter.0.lock().unwrap().is_empty());
        let page = terminal.tail(1).await.unwrap();
        assert_eq!(page.lines, ["kept  \r\t\u{2028}"]);
        assert_eq!(
            (page.before, page.after, page.at_head, page.at_tail),
            (None, None, Some(false), true)
        );
        assert_eq!(*adapter.0.lock().unwrap(), ["tail"]);

        let bytes = Backend::default();
        let output = Output::new(bytes.clone(), ProcessRef::new("extension", "task", None));
        assert!(output.capabilities().read);
        assert!(bytes.calls.lock().unwrap().is_empty());
        for max_bytes in [0, MAX_OUTPUT_LIMIT + 1] {
            assert_eq!(
                output
                    .read(None, ReadDirection::Forward, max_bytes)
                    .await
                    .unwrap_err()
                    .kind,
                ErrorKind::InvalidArgument
            );
            assert_eq!(
                output.tail(max_bytes).await.unwrap_err().kind,
                ErrorKind::InvalidArgument
            );
        }
        assert!(bytes.calls.lock().unwrap().is_empty());
        assert_eq!(
            output
                .read(None, ReadDirection::Backward, 1)
                .await
                .unwrap_err()
                .kind,
            ErrorKind::Transport
        );
        assert_eq!(output.tail(1).await.unwrap_err().kind, ErrorKind::Transport);
        assert_eq!(*bytes.calls.lock().unwrap(), ["read_output", "tail_output"]);
    });
}
