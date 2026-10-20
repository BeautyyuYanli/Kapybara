//! Third-party raw implementations exercise the public validation and dispatch boundary.

use std::{
    sync::{Arc, Mutex},
    time::Duration,
};

use async_trait::async_trait;
use futures::{StreamExt, executor::block_on, stream};
use opensol_core::{
    raw::{
        ConnectionAccess, OutputAccess, ProcessManagerAccess, SandboxManagerAccess, TerminalAccess,
    },
    *,
};

#[derive(Clone, Default)]
struct Backend {
    calls: Arc<Mutex<Vec<&'static str>>>,
    reconnect: bool,
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
            stdin: false,
            timeout: false,
            separate_stderr: true,
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
            inspect: true,
            ..Default::default()
        }
    }
    async fn create(&self, _options: CreateOptions) -> Result<SandboxRef> {
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
            client.create(invalid).await.unwrap_err().kind,
            ErrorKind::InvalidArgument
        );
        assert!(adapter.calls.lock().unwrap().is_empty());
        assert_eq!(
            client
                .create(CreateOptions::default())
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
            stdin: true,
            close_stdin: true,
            resize: true,
            supported_signals: &[Signal::Kill],
            reconnect: self.reconnect,
            ..Default::default()
        }
    }
    fn output_access(&self) -> Option<Arc<dyn OutputAccess>> {
        self.reconnect
            .then(|| Arc::new(self.clone()) as Arc<dyn OutputAccess>)
    }
    async fn spawn(&self, _request: SpawnRequest) -> Result<OutputSubscription> {
        self.calls.lock().unwrap().push("spawn");
        Ok(OutputSubscription::new(
            ProcessRef::new("extension", "task", Some(false)),
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
        ))
    }
    async fn list(&self) -> Result<Vec<ProcessInfo>> {
        self.entered("list")
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
    fn capabilities(&self) -> OutputCapabilities {
        OutputCapabilities {
            read: true,
            tail: true,
            subscribe: self.reconnect,
            ..Default::default()
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
fn native_input_restrictions_are_not_inferred_from_mode_or_binary_guarantees() {
    block_on(async {
        let adapter = Backend::default();
        let manager = ProcessManager::new(adapter.clone());
        assert!(!manager.capabilities().binary_stdin);
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
        assert!(output.capabilities().subscribe);
        let mut subscription = bound.clone().output().subscribe().await.unwrap();
        assert!(!output.capabilities().subscribe);
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

/// A third party can expose native terminal text without implementing task control or bytes.
#[derive(Clone, Default)]
struct TerminalOnly(Arc<Mutex<Vec<&'static str>>>);
#[async_trait]
impl TerminalAccess for TerminalOnly {
    fn backend(&self) -> &'static str {
        "terminal-only"
    }
    fn capabilities(&self) -> TerminalCapabilities {
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
