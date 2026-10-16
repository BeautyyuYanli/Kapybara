//! Real native process tests. Run inside Dockerfile.tests, never as host-skipped substitutes.

#![cfg(all(
    feature = "local",
    feature = "ssh",
    feature = "pueue",
    feature = "docker"
))]

use std::{
    collections::BTreeMap,
    panic::AssertUnwindSafe,
    path::{Path, PathBuf},
    process::{Child, Command as NativeCommand, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use async_trait::async_trait;
use futures::{FutureExt, StreamExt};
use opensol_adapters::{
    docker::{DockerConfig, DockerLifecycle},
    local::Local,
    pueue::{Pueue, PueueConfig},
    ssh::{Ssh, SshConfig},
};
use opensol_core::{
    Command, Connection, CreateOptions, ErrorKind, ExecRequest, OutputChannel, OutputCursor,
    Process, ProcessEvent, ProcessManager, ProcessRef, ProcessState, PtySize, ResourceState,
    Signal, SpawnRequest, raw::ExecAccess,
};
use tempfile::TempDir;

/// Real Local execution with controlled target identity/environment. Counting all
/// response bytes catches collect-then-slice without inspecting private scripts or cursors.
#[derive(Clone, Default)]
struct LocalTarget {
    prefix: Vec<String>,
    env: BTreeMap<String, String>,
    received: Arc<Mutex<usize>>,
}

impl LocalTarget {
    fn take_received(&self) -> usize {
        std::mem::take(&mut self.received.lock().unwrap())
    }
}

#[async_trait]
impl ExecAccess for LocalTarget {
    fn backend(&self) -> &'static str {
        Connection::new(Local).backend()
    }
    fn capabilities(&self) -> opensol_core::ConnectionCapabilities {
        Connection::new(Local).capabilities()
    }
    async fn exec(
        &self,
        mut request: ExecRequest,
    ) -> opensol_core::Result<opensol_core::ExecResult> {
        request.command.env.extend(self.env.clone());
        if !self.prefix.is_empty() {
            let mut argv = self.prefix.clone();
            argv.extend(request.command.argv);
            request.command.argv = argv;
        }
        let result = Connection::new(Local).exec(request).await?;
        *self.received.lock().unwrap() += result.stdout.len() + result.stderr.len();
        Ok(result)
    }
}

/// Change a real native task at the Connection boundary of wait/follow. Both the
/// intercepted command and the racing control operation still use actual Pueue.
/// A gated replacement can stay Running until the test explicitly releases stdin.
#[derive(Clone)]
struct PueueChangeDuringCommand {
    process: Process,
    command: &'static str,
    after: bool,
    remove: bool,
    wait_for_completion: bool,
    fired: Arc<AtomicBool>,
}

impl PueueChangeDuringCommand {
    async fn change(&self) -> opensol_core::Result<()> {
        if self.remove {
            self.process.remove().await
        } else {
            self.process.restart().await?;
            if self.wait_for_completion {
                self.process.wait(Some(Duration::from_secs(5))).await?;
            } else {
                tokio::time::timeout(Duration::from_secs(5), async {
                    while self.process.inspect().await?.state != ProcessState::Running {
                        tokio::time::sleep(Duration::from_millis(20)).await;
                    }
                    Ok::<(), opensol_core::Error>(())
                })
                .await
                .expect("gated replacement must start running")?;
            }
            Ok(())
        }
    }
}

#[async_trait]
impl ExecAccess for PueueChangeDuringCommand {
    fn backend(&self) -> &'static str {
        "local"
    }
    fn capabilities(&self) -> opensol_core::ConnectionCapabilities {
        Connection::new(Local).capabilities()
    }
    async fn exec(&self, request: ExecRequest) -> opensol_core::Result<opensol_core::ExecResult> {
        let change = request.command.argv.iter().any(|arg| arg == self.command)
            && !self.fired.swap(true, Ordering::SeqCst);
        if change && !self.after {
            self.change().await?;
        }
        let result = Connection::new(Local).exec(request).await;
        if change && self.after {
            self.change().await?;
        }
        result
    }
}

struct Daemon {
    child: Child,
    _directory: TempDir,
}

impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = nix::sys::signal::killpg(
            nix::unistd::Pid::from_raw(self.child.id() as i32),
            nix::sys::signal::Signal::SIGKILL,
        );
        let _ = self.child.wait();
    }
}

fn start_daemon(mut command: NativeCommand, directory: TempDir) -> Daemon {
    use std::os::unix::process::CommandExt;
    let child = command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .process_group(0)
        .spawn()
        .expect("native daemon must be available in the test container");
    Daemon {
        child,
        _directory: directory,
    }
}

async fn collect(
    mut events: opensol_core::EventStream,
) -> (Vec<(OutputChannel, Vec<u8>)>, opensol_core::ProcessOutcome) {
    let mut output = Vec::new();
    while let Some(event) = events.next().await {
        match event.unwrap() {
            ProcessEvent::Output { channel, data } => output.push((channel, data)),
            ProcessEvent::Completed(outcome) => return (output, outcome),
        }
    }
    panic!("output ended without completion")
}

fn combined_output(output: &[(OutputChannel, Vec<u8>)]) -> Vec<u8> {
    assert!(
        output
            .iter()
            .all(|(channel, _)| *channel == OutputChannel::Combined)
    );
    output
        .iter()
        .flat_map(|(_, data)| data.iter().copied())
        .collect()
}

#[tokio::test]
async fn local_preserves_bytes_argv_environment_cwd_and_nonzero_exit() {
    let directory = TempDir::with_prefix("opensol ' cwd ").unwrap();
    let mut command = Command::new(
        "python3",
        [
            "-c",
            "import os,sys; assert os.getcwd()==sys.argv[1]; assert os.environ['SDK_TEST']==sys.argv[2]; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stderr.buffer.write(b'\\xfferr'); sys.exit(7)",
            directory.path().to_str().unwrap(),
            "'$()\n你好",
        ],
    );
    command.cwd = Some(directory.path().to_string_lossy().into());
    command.env.insert("SDK_TEST".into(), "'$()\n你好".into());
    let input = [0, 0xff, b'\n', 0x80].repeat(40_000);
    let result = Connection::new(Local)
        .exec(ExecRequest {
            command,
            stdin: input.clone(),
            timeout: Some(Duration::from_secs(5)),
        })
        .await
        .unwrap();
    assert_eq!(result.stdout, input);
    assert_eq!(result.stderr, b"\xfferr");
    assert_eq!(result.exit.code, Some(7));
}

#[tokio::test]
async fn local_timeout_cancels_the_process_group_and_classifies_missing_program() {
    let directory = TempDir::new().unwrap();
    let marker = directory.path().join("child-survived");
    let script = format!("(sleep 0.4; touch '{}') & wait", marker.display());
    let result = Connection::new(Local)
        .exec(ExecRequest {
            command: Command::shell(script),
            stdin: Vec::new(),
            timeout: Some(Duration::from_millis(60)),
        })
        .await
        .unwrap_err();
    assert_eq!(result.kind, ErrorKind::Timeout);
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert!(!marker.exists());
    assert_eq!(
        Connection::new(Local)
            .exec(ExecRequest::new(Command::new(
                "/no-such-sdk-program",
                Vec::<String>::new()
            )))
            .await
            .unwrap_err()
            .kind,
        ErrorKind::NotFound
    );
    let result = Connection::new(Local)
        .exec(ExecRequest::new(Command::shell("kill -TERM $$")))
        .await
        .unwrap();
    assert_eq!(result.exit.signal, Some(15));
    assert_eq!(result.exit.code, None);
}

fn keygen(path: &Path) {
    assert!(
        NativeCommand::new("ssh-keygen")
            .args(["-q", "-t", "ed25519", "-N", "", "-f"])
            .arg(path)
            .status()
            .unwrap()
            .success()
    );
}

async fn ssh_fixture() -> (Daemon, Connection, PathBuf) {
    let directory = TempDir::new().unwrap();
    let host = directory.path().join("host");
    let identity = directory.path().join("identity");
    keygen(&host);
    keygen(&identity);
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let server_config = directory.path().join("sshd_config");
    std::fs::write(&server_config, format!("Port {port}\nListenAddress 127.0.0.1\nHostKey {}\nPidFile {}\nAuthorizedKeysFile {}\nStrictModes no\nPermitRootLogin yes\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\nLogLevel ERROR\n", host.display(), directory.path().join("sshd.pid").display(), identity.with_extension("pub").display())).unwrap();
    let known_hosts = directory.path().join("known_hosts");
    let public_key = std::fs::read_to_string(host.with_extension("pub")).unwrap();
    std::fs::write(&known_hosts, format!("[127.0.0.1]:{port} {public_key}")).unwrap();
    let client_config = directory.path().join("ssh_config");
    std::fs::write(&client_config, format!("Host *\n  User root\n  StrictHostKeyChecking yes\n  UserKnownHostsFile {}\n  IdentitiesOnly yes\n", known_hosts.display())).unwrap();
    let mut config = SshConfig::new("127.0.0.1");
    config.port = Some(port);
    config.identity_file = Some(identity);
    config.config_file = Some(client_config);
    let ssh = Connection::new(Ssh::new(config).unwrap());
    let mut command = NativeCommand::new("/usr/sbin/sshd");
    command.args(["-D", "-e", "-f"]).arg(&server_config);
    let daemon = start_daemon(command, directory);
    for _attempt in 0..100 {
        if ssh
            .exec(ExecRequest {
                command: Command::new("true", Vec::<String>::new()),
                stdin: Vec::new(),
                timeout: Some(Duration::from_secs(2)),
            })
            .await
            .is_ok()
        {
            return (daemon, ssh, known_hosts);
        }
        tokio::time::sleep(Duration::from_millis(30)).await;
    }
    panic!("sshd failed to become ready")
}

#[tokio::test]
async fn real_ssh_preserves_executable_arguments_input_and_host_verification() {
    let (_daemon, ssh, known_hosts) = ssh_fixture().await;
    let directory = TempDir::with_prefix("opensol ' cwd $() ").unwrap();
    let program = directory.path().join("run=python3 '$()");
    std::os::unix::fs::symlink("/usr/bin/python3", &program).unwrap();
    let mut command = Command::new(
        program.to_str().unwrap(),
        [
            "-c",
            "import os,sys; assert sys.argv[1] == os.environ['SDK_TEST'] == os.environ['SDK-TEST']; assert os.environ['PATH']=='/sdk-task-only-path'; assert os.getcwd()==sys.argv[2]; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stderr.buffer.write(b'\\xff'); sys.exit(7)",
            "x '$()\n你好",
            directory.path().to_str().unwrap(),
        ],
    );
    command.cwd = Some(directory.path().to_string_lossy().into());
    command.env = BTreeMap::from([
        ("SDK_TEST".into(), "x '$()\n你好".into()),
        ("SDK-TEST".into(), "x '$()\n你好".into()),
        ("PATH".into(), "/sdk-task-only-path".into()),
    ]);
    let input = [0, 0xff, b'\n'].repeat(30_000);
    let result = ssh
        .exec(ExecRequest {
            command,
            stdin: input.clone(),
            timeout: Some(Duration::from_secs(5)),
        })
        .await
        .unwrap();
    assert_eq!(result.stdout, input);
    assert_eq!(result.stderr, b"\xff");
    assert_eq!(result.exit.code, Some(7));
    let result = ssh
        .exec(ExecRequest::new(Command::shell("exit 255")))
        .await
        .unwrap_err();
    assert_eq!(result.kind, ErrorKind::Transport);
    std::fs::write(known_hosts, "").unwrap();
    assert_eq!(
        ssh.exec(ExecRequest::new(Command::new("true", Vec::<String>::new())))
            .await
            .unwrap_err()
            .kind,
        ErrorKind::Transport
    );
}

async fn pueue_fixture() -> (Daemon, ProcessManager) {
    let directory = TempDir::with_prefix("opensol pueue ' $() ").unwrap();
    let path = directory.path().join("pueue.yml");
    std::fs::write(&path, format!("shared:\n  pueue_directory: {}\n  runtime_directory: {}\nclient:\n  read_local_logs: true\n", directory.path().display(), directory.path().display())).unwrap();
    let config = PueueConfig {
        config_file: Some(path.to_string_lossy().into()),
        pueue_directory: Some(directory.path().to_string_lossy().into()),
        ..Default::default()
    };
    let manager = Pueue::new(Connection::new(Local), config)
        .map(ProcessManager::new)
        .unwrap();
    let mut command = NativeCommand::new("pueued");
    command.arg("--config").arg(&path);
    let daemon = start_daemon(command, directory);
    for _attempt in 0..100 {
        let ready = Connection::new(Local)
            .exec(ExecRequest::new(Command::new(
                "pueue",
                ["--config", path.to_str().unwrap(), "status", "--json"],
            )))
            .await
            .unwrap();
        if ready.exit.success() {
            return (daemon, manager);
        }
        tokio::time::sleep(Duration::from_millis(30)).await;
    }
    panic!("pueued failed to become ready")
}

#[tokio::test]
async fn real_pueue_retains_merged_binary_logs_nonzero_exit_and_task_identity() {
    let (_daemon, manager) = pueue_fixture().await;
    let mut request = SpawnRequest::new(Command::new(
        "python3",
        [
            "-c",
            "import sys; sys.stdout.buffer.write(b'\\x00\\xffout\\n'); sys.stdout.buffer.flush(); sys.stderr.buffer.write(b'err\\n'); sys.exit(7)",
        ],
    ));
    request
        .command
        .env
        .insert("SDK_TEST".into(), "value' $()".into());
    let handle = manager.spawn(request).await.unwrap();
    let process = handle.process.clone();
    let (output, outcome) = collect(handle.events).await;
    assert_eq!(combined_output(&output), b"\x00\xffout\nerr\n");
    assert_eq!(outcome.exit.unwrap().code, Some(7));
    assert_eq!(outcome.native_result.as_deref(), Some("Failed"));
    assert_eq!(
        manager
            .get(process.clone())
            .inspect()
            .await
            .unwrap()
            .outcome,
        Some(outcome.clone())
    );
    let (again, replay_outcome) = collect(
        manager
            .get(process.clone())
            .output()
            .subscribe()
            .await
            .unwrap()
            .events,
    )
    .await;
    assert_eq!(combined_output(&again), b"\x00\xffout\nerr\n");
    assert_eq!(replay_outcome, outcome);
    let missing = ProcessRef::new("pueue", "999999999", Some(false));
    assert_eq!(
        manager
            .get(missing.clone())
            .wait(Some(Duration::from_secs(2)))
            .await
            .unwrap_err()
            .kind,
        ErrorKind::NotFound
    );
    let mut request = SpawnRequest::new(Command::shell("true"));
    request.pty = Some(PtySize { cols: 80, rows: 24 });
    assert!(
        matches!(manager.spawn(request).await, Err(error) if error.kind == ErrorKind::Unsupported)
    );
}

#[tokio::test]
async fn real_pueue_delayed_subscriptions_reject_restarts_and_native_cleanup() {
    let (daemon, manager) = pueue_fixture().await;
    let script = daemon._directory.path().join("command.sh");
    std::fs::write(&script, "printf 7; exit 7").unwrap();
    let initial = manager
        .spawn(SpawnRequest::new(Command::new(
            "sh",
            [script.to_str().unwrap()],
        )))
        .await
        .unwrap();
    let process = manager.get(initial.process.clone());
    assert_eq!(
        process
            .wait(Some(Duration::from_secs(5)))
            .await
            .unwrap()
            .exit
            .unwrap()
            .code,
        Some(7)
    );
    let replay = process.output().subscribe().await.unwrap();
    std::fs::write(&script, "printf 9; exit 9").unwrap();
    process.restart().await.unwrap();
    assert_eq!(
        process
            .wait(Some(Duration::from_secs(5)))
            .await
            .unwrap()
            .exit
            .unwrap()
            .code,
        Some(9)
    );
    for mut handle in [initial, replay] {
        let error = tokio::time::timeout(Duration::from_secs(5), handle.events.next())
            .await
            .unwrap()
            .unwrap()
            .unwrap_err();
        assert_eq!(error.kind, ErrorKind::Conflict);
    }
    let (output, outcome) = collect(process.output().subscribe().await.unwrap().events).await;
    assert_eq!(combined_output(&output), b"9");
    assert_eq!(outcome.exit.unwrap().code, Some(9));
    let mut replaced = process.output().subscribe().await.unwrap();
    // A fresh native daemon reuses ID 0. Replacing the CLI configuration must
    // not let the old subscription accept that other task's created_at.
    let (replacement_daemon, replacement_manager) = pueue_fixture().await;
    let replacement = replacement_manager
        .spawn(SpawnRequest::new(Command::new(
            "sh",
            [script.to_str().unwrap()],
        )))
        .await
        .unwrap();
    assert_eq!(replacement.process.id, process.reference().id);
    std::fs::copy(
        replacement_daemon._directory.path().join("pueue.yml"),
        daemon._directory.path().join("pueue.yml"),
    )
    .unwrap();
    process.wait(Some(Duration::from_secs(5))).await.unwrap();
    assert_eq!(
        replaced.events.next().await.unwrap().unwrap_err().kind,
        ErrorKind::Conflict
    );
    let mut removed = process.output().subscribe().await.unwrap();
    process.remove().await.unwrap();
    assert_eq!(
        removed.events.next().await.unwrap().unwrap_err().kind,
        ErrorKind::NotFound
    );
    assert!(
        matches!(process.output().subscribe().await, Err(error) if error.kind == ErrorKind::NotFound)
    );
}

#[tokio::test]
async fn real_pueue_subscriptions_guard_wait_and_follow_against_native_changes() {
    let (daemon, manager) = pueue_fixture().await;
    let script = daemon._directory.path().join("command.sh");
    for (command, after, remove) in [
        ("wait", true, false),
        ("follow", false, false),
        ("follow", true, false),
        ("follow", false, true),
        ("follow", true, true),
    ] {
        std::fs::write(&script, "printf 7; exit 7").unwrap();
        let initial = manager
            .spawn(SpawnRequest::new(Command::new(
                "sh",
                [script.to_str().unwrap()],
            )))
            .await
            .unwrap();
        let process = manager.get(initial.process.clone());
        process.wait(Some(Duration::from_secs(5))).await.unwrap();
        let racing = ProcessManager::new(
            Pueue::new(
                Connection::new(PueueChangeDuringCommand {
                    process,
                    command,
                    after,
                    remove,
                    wait_for_completion: true,
                    fired: Arc::new(AtomicBool::new(false)),
                }),
                PueueConfig {
                    config_file: Some(
                        daemon
                            ._directory
                            .path()
                            .join("pueue.yml")
                            .to_string_lossy()
                            .into(),
                    ),
                    ..Default::default()
                },
            )
            .unwrap(),
        );
        let mut subscription = racing
            .get(initial.process)
            .output()
            .subscribe()
            .await
            .unwrap();
        std::fs::write(&script, "printf 9; exit 9").unwrap();
        let error = tokio::time::timeout(Duration::from_secs(10), subscription.events.next())
            .await
            .unwrap()
            .unwrap()
            .unwrap_err();
        assert_eq!(
            error.kind,
            if remove {
                ErrorKind::NotFound
            } else {
                ErrorKind::Conflict
            },
            "command={command}, after={after}, remove={remove}"
        );
    }
}

#[tokio::test]
async fn real_pueue_old_subscriptions_conflict_while_restarted_execution_is_running() {
    let (daemon, manager) = pueue_fixture().await;
    let script = daemon._directory.path().join("command.sh");
    std::fs::write(&script, "printf 7; exit 7").unwrap();
    let mut request = SpawnRequest::new(Command::new("sh", [script.to_str().unwrap()]));
    request.stdin = true;
    let initial = manager.spawn(request).await.unwrap();
    let process = manager.get(initial.process.clone());
    assert_eq!(
        process
            .wait(Some(Duration::from_secs(5)))
            .await
            .unwrap()
            .exit
            .unwrap()
            .code,
        Some(7)
    );
    let replay = process.output().subscribe().await.unwrap();
    std::fs::write(&script, "read -r gate; printf 9; exit 9").unwrap();
    process.restart().await.unwrap();
    await_state(&manager, process.reference(), ProcessState::Running).await;

    // Keep the replacement blocked throughout both reads. A guard deferred until
    // native wait returns cannot pass merely because the new execution finishes.
    let mut first_events = Vec::new();
    for mut handle in [initial, replay] {
        first_events.push(tokio::time::timeout(Duration::from_secs(2), handle.events.next()).await);
    }
    let before_release = process.inspect().await.unwrap();
    process.send_input(b"release\n".to_vec()).await.unwrap();
    let completed = process.wait(Some(Duration::from_secs(5))).await.unwrap();
    process.remove().await.unwrap();

    // Clean up even when a bounded read exposed a regression, then assert the
    // observations made before releasing the gate.
    assert_eq!(before_release.state, ProcessState::Running);
    assert!(before_release.outcome.is_none());
    assert_eq!(completed.exit.unwrap().code, Some(9));
    for event in first_events {
        assert_eq!(
            event
                .expect("old subscription must reject before the gate is released")
                .expect("subscription must report an error")
                .unwrap_err()
                .kind,
            ErrorKind::Conflict
        );
    }
}

#[tokio::test]
async fn real_pueue_post_wait_restart_conflicts_without_a_current_outcome() {
    let (daemon, manager) = pueue_fixture().await;
    let script = daemon._directory.path().join("command.sh");
    std::fs::write(&script, "printf 7; exit 7").unwrap();
    let mut request = SpawnRequest::new(Command::new("sh", [script.to_str().unwrap()]));
    request.stdin = true;
    let initial = manager.spawn(request).await.unwrap();
    let process = manager.get(initial.process.clone());
    assert_eq!(
        process
            .wait(Some(Duration::from_secs(5)))
            .await
            .unwrap()
            .exit
            .unwrap()
            .code,
        Some(7)
    );
    let racing = ProcessManager::new(
        Pueue::new(
            Connection::new(PueueChangeDuringCommand {
                process: process.clone(),
                command: "wait",
                after: true,
                remove: false,
                wait_for_completion: false,
                fired: Arc::new(AtomicBool::new(false)),
            }),
            PueueConfig {
                config_file: Some(
                    daemon
                        ._directory
                        .path()
                        .join("pueue.yml")
                        .to_string_lossy()
                        .into(),
                ),
                ..Default::default()
            },
        )
        .unwrap(),
    );
    let mut subscription = racing
        .get(initial.process)
        .output()
        .subscribe()
        .await
        .unwrap();
    std::fs::write(&script, "read -r gate; printf 9; exit 9").unwrap();

    // The original native wait has returned when the fixture starts the gated
    // replacement. Post-wait status now has no outcome; normalize that to Conflict.
    let first_event =
        tokio::time::timeout(Duration::from_secs(2), subscription.events.next()).await;
    let before_release = process.inspect().await.unwrap();
    process.send_input(b"release\n".to_vec()).await.unwrap();
    let completed = process.wait(Some(Duration::from_secs(5))).await.unwrap();
    process.remove().await.unwrap();

    assert_eq!(before_release.state, ProcessState::Running);
    assert!(before_release.outcome.is_none());
    assert_eq!(completed.exit.unwrap().code, Some(9));
    assert_eq!(
        first_event
            .expect("post-wait restart must reject before the gate is released")
            .expect("subscription must report an error")
            .unwrap_err()
            .kind,
        ErrorKind::Conflict
    );
}

async fn await_state(manager: &ProcessManager, process: &ProcessRef, state: ProcessState) {
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            if manager.get(process.clone()).inspect().await.unwrap().state == state {
                return;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("native task must reach the expected state");
}

fn page_bytes(page: &opensol_core::OutputPage) -> Vec<u8> {
    assert!(
        page.chunks
            .iter()
            .all(|chunk| chunk.channel == OutputChannel::Combined)
    );
    page.chunks
        .iter()
        .flat_map(|chunk| chunk.data.iter().copied())
        .collect()
}

#[tokio::test]
async fn real_pueue_output_resources_bound_native_transfer_at_initial_and_resumed_offsets() {
    let (daemon, manager) = pueue_fixture().await;
    let handle = manager
        .spawn(SpawnRequest::new(Command::new(
            "python3",
            [
                "-c",
                "import sys; sys.stdout.buffer.write(b'\\x00\\xffABCDEF' * 1048576)",
            ],
        )))
        .await
        .unwrap();
    manager
        .get(handle.process.clone())
        .wait(Some(Duration::from_secs(5)))
        .await
        .unwrap();
    let connection = LocalTarget::default();
    let directory = daemon._directory.path();
    let recorded = Pueue::new(
        Connection::new(connection.clone()),
        PueueConfig {
            config_file: Some(directory.join("pueue.yml").to_string_lossy().into()),
            pueue_directory: Some(directory.to_string_lossy().into()),
            ..Default::default()
        },
    )
    .map(ProcessManager::new)
    .unwrap();
    // Allow fixed CLI/metadata overhead, orders of magnitude below the real 8 MiB native log.
    const METADATA_BUDGET: usize = 65_536;
    let info = recorded
        .get(handle.process.clone())
        .output()
        .stat()
        .await
        .unwrap();
    assert_eq!(info.size, 8 * 1024 * 1024);
    assert!(
        connection.take_received() <= METADATA_BUDGET,
        "stat must not transfer log payload"
    );
    let limit = 31;
    let first = recorded
        .get(handle.process.clone())
        .output()
        .read(None, limit)
        .await
        .unwrap();
    assert!(
        connection.take_received() <= METADATA_BUDGET + limit,
        "read must bound native transfer before returning a page"
    );
    assert_eq!(page_bytes(&first), b"\x00\xffABCDEF".repeat(4)[..limit]);
    assert!(!first.at_end);
    let advance = METADATA_BUDGET * 2 + 1;
    let prepared = recorded
        .get(handle.process.clone())
        .output()
        .read(Some(&first.next_cursor), advance)
        .await
        .unwrap();
    assert_eq!(page_bytes(&prepared).len(), advance);
    let resumed_offset = page_bytes(&first).len() + page_bytes(&prepared).len();
    assert!(resumed_offset > METADATA_BUDGET);
    // Exclude preparation from the measured request; transferring this prefix alone
    // would exceed the entire metadata allowance on the small resumed read.
    let _ = connection.take_received();
    let second = recorded
        .get(handle.process.clone())
        .output()
        .read(Some(&prepared.next_cursor), limit)
        .await
        .unwrap();
    assert!(
        connection.take_received() <= METADATA_BUDGET + limit,
        "resumed reads must not transfer the native log prefix"
    );
    let expected: Vec<u8> = (resumed_offset..resumed_offset + limit)
        .map(|index| b"\x00\xffABCDEF"[index % 8])
        .collect();
    assert_eq!(page_bytes(&second), expected);
    assert!(!second.at_end);
}

#[tokio::test]
async fn real_pueue_probe_and_log_access_failures_preserve_classification_and_redact_diagnostics() {
    use std::os::unix::fs::PermissionsExt;
    let (mut daemon, manager) = pueue_fixture().await;
    let directory = daemon._directory.path();
    let config_file = directory.join("pueue.yml");
    let installed = NativeCommand::new("sh")
        .args(["-c", "command -v pueue"])
        .output()
        .unwrap();
    assert!(installed.status.success());
    let program = PathBuf::from(String::from_utf8(installed.stdout).unwrap().trim());
    let handle = manager
        .spawn(SpawnRequest::new(Command::new(
            "printf",
            ["%s", "native log"],
        )))
        .await
        .unwrap();
    manager
        .get(handle.process.clone())
        .wait(Some(Duration::from_secs(5)))
        .await
        .unwrap();
    let config = PueueConfig {
        program: program.clone(),
        config_file: Some(config_file.to_string_lossy().into()),
        pueue_directory: Some(directory.to_string_lossy().into()),
        ..Default::default()
    };
    let tools = TempDir::new().unwrap();
    for tool in ["sh", "uname", "stat"] {
        std::os::unix::fs::symlink(format!("/usr/bin/{tool}"), tools.path().join(tool)).unwrap();
    }
    let restricted_path = LocalTarget {
        env: BTreeMap::from([("PATH".into(), tools.path().to_string_lossy().into())]),
        ..Default::default()
    };
    let missing_tool = Pueue::new(Connection::new(restricted_path), config.clone())
        .map(ProcessManager::new)
        .unwrap();
    assert_eq!(
        missing_tool.probe().await.unwrap_err().kind,
        ErrorKind::Unsupported
    );
    assert_eq!(
        missing_tool
            .get(handle.process.clone())
            .output()
            .read(None, 32)
            .await
            .unwrap_err()
            .kind,
        ErrorKind::Unsupported
    );
    // A native executable shadowing dd, but lacking its contract, is incompatible too.
    std::os::unix::fs::symlink("/usr/bin/true", tools.path().join("dd")).unwrap();
    assert_eq!(
        missing_tool.probe().await.unwrap_err().kind,
        ErrorKind::Unsupported
    );

    // Give nobody access to the real management socket/config, then deny directory
    // listing on that same native data directory. Named control files remain readable.
    let socket = directory.join("pueue_root.socket");
    let yaml = format!(
        "shared:\n  pueue_directory: {}\n  runtime_directory: {}\n  unix_socket_path: {}\nclient:\n  read_local_logs: true\n",
        serde_json::to_string(&directory).unwrap(),
        serde_json::to_string(&directory).unwrap(),
        serde_json::to_string(&socket).unwrap()
    );
    std::fs::write(&config_file, yaml).unwrap();
    for (path, mode) in [
        (directory.to_path_buf(), 0o755),
        (config_file.clone(), 0o644),
        (directory.join("shared_secret"), 0o644),
        (socket, 0o666),
    ] {
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode)).unwrap();
    }
    let unprivileged = LocalTarget {
        prefix: [
            "/usr/bin/setpriv",
            "--reuid",
            "65534",
            "--regid",
            "65534",
            "--clear-groups",
            "--",
        ]
        .map(Into::into)
        .to_vec(),
        ..Default::default()
    };
    let uid = Connection::new(unprivileged.clone())
        .exec(ExecRequest::new(Command::new("id", ["-u"])))
        .await
        .unwrap();
    assert!(uid.exit.success());
    assert_eq!(uid.stdout, b"65534\n");
    let accessible = Pueue::new(
        Connection::new(unprivileged.clone()),
        PueueConfig {
            pueue_directory: None,
            ..config.clone()
        },
    )
    .map(ProcessManager::new)
    .unwrap();
    accessible
        .probe()
        .await
        .expect("the unprivileged client must really reach the native daemon");
    std::fs::set_permissions(directory, std::fs::Permissions::from_mode(0o711)).unwrap();
    let denied_logs = Pueue::new(Connection::new(unprivileged), config.clone())
        .map(ProcessManager::new)
        .unwrap();
    for error in [
        denied_logs.probe().await.unwrap_err(),
        denied_logs
            .get(handle.process.clone())
            .output()
            .read(None, 32)
            .await
            .unwrap_err(),
    ] {
        assert_eq!(error.kind, ErrorKind::Permission);
        assert_eq!(error.backend, "pueue");
        assert!(!error.to_string().contains(directory.to_str().unwrap()));
    }

    let absent_program = directory.join("private-missing-executable-diagnostic");
    let non_executable = directory.join("private-permission-executable-diagnostic");
    std::fs::write(&non_executable, b"not executable").unwrap();
    std::fs::set_permissions(&non_executable, std::fs::Permissions::from_mode(0o600)).unwrap();
    for (path, kind) in [
        (absent_program, ErrorKind::NotFound),
        (non_executable, ErrorKind::Permission),
    ] {
        let manager = Pueue::new(
            Connection::new(Local),
            PueueConfig {
                program: path.clone(),
                ..config.clone()
            },
        )
        .map(ProcessManager::new)
        .unwrap();
        let error = manager.probe().await.unwrap_err();
        assert_eq!(error.kind, kind);
        assert_eq!(
            (error.backend.as_str(), error.operation.as_str()),
            ("pueue", "probe")
        );
        assert!(!error.to_string().contains(path.to_str().unwrap()));
    }
    nix::sys::signal::killpg(
        nix::unistd::Pid::from_raw(daemon.child.id() as i32),
        nix::sys::signal::Signal::SIGKILL,
    )
    .unwrap();
    daemon.child.wait().unwrap();
    let diagnostic = NativeCommand::new(&program)
        .args([
            "--config",
            config_file.to_str().unwrap(),
            "status",
            "--json",
        ])
        .output()
        .unwrap();
    assert!(!diagnostic.status.success());
    let private_directory = directory.to_str().unwrap();
    assert!(
        String::from_utf8_lossy(&diagnostic.stderr).contains(private_directory),
        "fixture must produce an actual native path diagnostic"
    );
    let error = manager.probe().await.unwrap_err();
    assert_eq!(error.kind, ErrorKind::Backend);
    assert_eq!(error.backend, "pueue");
    assert!(!error.to_string().contains(private_directory));
}

#[tokio::test]
async fn real_pueue_resource_reads_running_binary_pages_and_detects_log_loss() {
    let (daemon, manager) = pueue_fixture().await;
    assert!(
        manager
            .probe()
            .await
            .unwrap()
            .client_version
            .unwrap()
            .starts_with("4.")
    );
    assert!(manager.capabilities().output_resource);
    let handle = manager.spawn(SpawnRequest::new(Command::new("python3", ["-c", "import sys; sys.stdin.readline(); sys.stdout.buffer.write(b'\\x00\\xffA'*3000); sys.stdout.buffer.flush(); sys.stdin.readline(); sys.stdout.buffer.write(b'\\x80tail'); sys.exit(7)"]))).await.unwrap();
    await_state(&manager, &handle.process, ProcessState::Running).await;
    let process = Process::new(manager.clone(), handle.process.clone());
    drop(handle);
    assert!(
        manager
            .list()
            .await
            .unwrap()
            .iter()
            .any(|info| info.process == *process.reference())
    );
    let output = process.output();
    let empty = output.read(None, 32).await.unwrap();
    assert!(empty.chunks.is_empty() && empty.at_end);
    assert_eq!(output.stat().await.unwrap().size, 0);
    process.send_input(b"write\n".to_vec()).await.unwrap();
    tokio::time::timeout(Duration::from_secs(5), async {
        while output.stat().await.unwrap().size < 9000 {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .unwrap();
    let first = output.read(Some(&empty.next_cursor), 7).await.unwrap();
    assert_eq!(page_bytes(&first), b"\x00\xffA\x00\xffA\x00");
    assert!(!first.at_end);
    let resumed = OutputCursor(first.next_cursor.0.clone());
    let second = output.read(Some(&resumed), 4096).await.unwrap();
    assert_eq!(page_bytes(&second).len(), 4096);
    let replay = output.read(Some(&resumed), 4096).await.unwrap();
    assert_eq!(page_bytes(&replay), page_bytes(&second));
    assert_eq!(replay.at_end, second.at_end);
    let last = output
        .read(Some(&second.next_cursor), 1_048_576)
        .await
        .unwrap();
    assert!(last.at_end);
    assert_eq!(
        [page_bytes(&first), page_bytes(&second), page_bytes(&last)].concat(),
        b"\x00\xffA".repeat(3000)
    );
    assert_eq!(
        process.inspect().await.unwrap().state,
        ProcessState::Running,
        "observed EOF is not completion"
    );
    let no_new_bytes = output.read(Some(&last.next_cursor), 64).await.unwrap();
    assert!(no_new_bytes.at_end && no_new_bytes.chunks.is_empty());
    process.send_input(b"finish\n".to_vec()).await.unwrap();
    assert_eq!(
        process
            .wait(Some(Duration::from_secs(5)))
            .await
            .unwrap()
            .exit
            .unwrap()
            .code,
        Some(7)
    );
    assert_eq!(
        process
            .inspect()
            .await
            .unwrap()
            .outcome
            .unwrap()
            .native_result
            .as_deref(),
        Some("Failed")
    );
    let appended = output.read(Some(&last.next_cursor), 64).await.unwrap();
    assert_eq!(page_bytes(&appended), b"\x80tail");
    assert!(appended.at_end);
    for limit in [0, 1_048_577] {
        assert_eq!(
            output.read(None, limit).await.unwrap_err().kind,
            ErrorKind::InvalidArgument
        );
    }
    assert_eq!(
        output
            .read(Some(&OutputCursor("not a cursor".into())), 64)
            .await
            .unwrap_err()
            .kind,
        ErrorKind::InvalidArgument
    );
    let path = daemon
        ._directory
        .path()
        .join("task_logs")
        .join(format!("{}.log", process.reference().id));
    std::fs::write(&path, b"short").unwrap();
    assert_eq!(
        output
            .read(Some(&appended.next_cursor), 64)
            .await
            .unwrap_err()
            .kind,
        ErrorKind::Conflict
    );
    let replacement = path.with_extension("replacement");
    std::fs::write(&replacement, vec![b'R'; 9005]).unwrap();
    std::fs::rename(replacement, &path).unwrap();
    assert_eq!(
        output
            .read(Some(&first.next_cursor), 64)
            .await
            .unwrap_err()
            .kind,
        ErrorKind::Conflict
    );
    std::fs::remove_file(&path).unwrap();
    assert_eq!(output.stat().await.unwrap_err().kind, ErrorKind::NotFound);
    let mut subscription = output.subscribe().await.unwrap();
    assert_eq!(
        subscription.events.next().await.unwrap().unwrap_err().kind,
        ErrorKind::Backend,
        "known completion must not hide a failure to read the retained log"
    );
    process.remove().await.unwrap();
    assert_eq!(
        process.inspect().await.unwrap_err().kind,
        ErrorKind::NotFound
    );
}

#[tokio::test]
async fn real_pueue_controls_use_native_task_states_and_restart_replaces_execution() {
    let (daemon, manager) = pueue_fixture().await;
    let handle = manager
        .spawn(SpawnRequest::new(Command::shell("printf ready; sleep 30")))
        .await
        .unwrap();
    await_state(&manager, &handle.process, ProcessState::Running).await;
    tokio::time::timeout(Duration::from_secs(5), async {
        while manager
            .get(handle.process.clone())
            .output()
            .stat()
            .await
            .unwrap()
            .size
            == 0
        {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .unwrap();
    let process = manager.get(handle.process.clone());
    let old = process.output().read(None, 64).await.unwrap();
    assert_eq!(page_bytes(&old), b"ready");
    process.pause().await.unwrap();
    assert_eq!(process.inspect().await.unwrap().state, ProcessState::Paused);
    assert_eq!(process.pause().await.unwrap_err().kind, ErrorKind::Conflict);
    process.start().await.unwrap();
    assert_eq!(
        process.inspect().await.unwrap().state,
        ProcessState::Running
    );
    assert_eq!(
        process.restart().await.unwrap_err().kind,
        ErrorKind::Conflict
    );
    assert_eq!(
        process.remove().await.unwrap_err().kind,
        ErrorKind::Conflict
    );
    process.kill().await.unwrap();
    let outcome = process.wait(Some(Duration::from_secs(5))).await.unwrap();
    assert!(outcome.exit.is_none());
    assert_eq!(outcome.native_result.as_deref(), Some("Killed"));
    assert_eq!(
        process.inspect().await.unwrap().outcome,
        Some(outcome.clone())
    );
    let (retained, completed) = collect(handle.events).await;
    assert_eq!(combined_output(&retained), b"ready");
    assert_eq!(completed, outcome);
    let config = daemon._directory.path().join("pueue.yml");
    assert!(
        NativeCommand::new("pueue")
            .args([
                "--config",
                config.to_str().unwrap(),
                "pause",
                "--group",
                "default"
            ])
            .status()
            .unwrap()
            .success()
    );
    process.restart().await.unwrap();
    assert_eq!(process.inspect().await.unwrap().state, ProcessState::Queued);
    assert_eq!(
        process
            .output()
            .read(Some(&old.next_cursor), 64)
            .await
            .unwrap_err()
            .kind,
        ErrorKind::Conflict
    );
    let queued = process.output().read(None, 64).await.unwrap();
    assert!(queued.at_end && queued.chunks.is_empty());
    let info = process.output().stat().await.unwrap();
    assert_eq!((info.size, info.version), (0, None));
    let queued_subscription = process.output().subscribe().await.unwrap();
    process.start().await.unwrap(); // explicit native start bypasses the paused queue.
    assert_eq!(
        process.inspect().await.unwrap().state,
        ProcessState::Running
    );
    let first_run = process
        .output()
        .read(Some(&queued.next_cursor), 64)
        .await
        .unwrap();
    assert!(page_bytes(&first_run).is_empty() || page_bytes(&first_run) == b"ready");
    process.kill().await.unwrap();
    let outcome = process.wait(Some(Duration::from_secs(5))).await.unwrap();
    assert!(outcome.exit.is_none());
    assert_eq!(outcome.native_result.as_deref(), Some("Killed"));
    let (queued_output, queued_outcome) = collect(queued_subscription.events).await;
    let queued_bytes = combined_output(&queued_output);
    assert!(queued_bytes.is_empty() || queued_bytes == b"ready");
    assert_eq!(queued_outcome, outcome);
    // A dependent retained task prevents removing its dependency; SDK never broadens the IDs.
    let child = NativeCommand::new("pueue")
        .args([
            "--config",
            config.to_str().unwrap(),
            "add",
            "--print-task-id",
            "--after",
            &process.reference().id,
            "--",
            "true",
        ])
        .output()
        .unwrap();
    assert!(child.status.success());
    assert!(process.remove().await.is_err());
    let child = ProcessRef::new(
        "pueue",
        String::from_utf8(child.stdout).unwrap().trim(),
        Some(false),
    );
    manager.get(child.clone()).remove().await.unwrap();
    process.remove().await.unwrap();
    assert!(manager.list().await.unwrap().is_empty());
    assert_eq!(
        process
            .output()
            .read(Some(&old.next_cursor), 64)
            .await
            .unwrap_err()
            .kind,
        ErrorKind::NotFound
    );
}

#[tokio::test]
async fn real_pueue_native_spawn_failure_has_a_result_name_without_a_fabricated_exit() {
    let (daemon, manager) = pueue_fixture().await;
    let config = daemon._directory.path().join("pueue.yml");
    assert!(
        NativeCommand::new("pueue")
            .args([
                "--config",
                config.to_str().unwrap(),
                "pause",
                "--group",
                "default"
            ])
            .output()
            .unwrap()
            .status
            .success()
    );
    let cwd = TempDir::with_prefix("opensol-private-native-error-").unwrap();
    let mut command = Command::new("true", Vec::<String>::new());
    command.cwd = Some(cwd.path().to_string_lossy().into_owned());
    let handle = manager.spawn(SpawnRequest::new(command)).await.unwrap();
    drop(cwd); // Native Pueue can no longer spawn in the original, valid-at-add cwd.
    manager.get(handle.process.clone()).start().await.unwrap();
    await_state(&manager, &handle.process, ProcessState::Exited).await;
    let info = manager.get(handle.process.clone()).inspect().await.unwrap();
    let outcome = info.outcome.unwrap();
    assert_eq!(outcome.native_result.as_deref(), Some("FailedToSpawn"));
    assert!(outcome.exit.is_none());
    let waited = manager
        .get(handle.process.clone())
        .wait(Some(Duration::from_secs(5)))
        .await
        .unwrap();
    assert_eq!(waited, outcome);
    let retained = std::fs::read(
        daemon
            ._directory
            .path()
            .join("task_logs")
            .join(format!("{}.log", handle.process.id)),
    )
    .unwrap();
    let (output, completed) = collect(handle.events).await;
    assert_eq!(combined_output(&output), retained);
    assert_eq!(completed, outcome);
}

#[tokio::test]
async fn real_ssh_pueue_log_resources_use_the_injected_execution_target() {
    let (daemon, _) = pueue_fixture().await;
    let (_ssh_daemon, ssh, _) = ssh_fixture().await;
    let directory = daemon._directory.path();
    let program = NativeCommand::new("sh")
        .args(["-c", "command -v pueue"])
        .output()
        .unwrap();
    assert!(program.status.success());
    let manager = Pueue::new(
        ssh,
        PueueConfig {
            program: String::from_utf8(program.stdout).unwrap().trim().into(),
            config_file: Some(directory.join("pueue.yml").to_string_lossy().into()),
            pueue_directory: Some(directory.to_string_lossy().into()),
            ..Default::default()
        },
    )
    .map(ProcessManager::new)
    .unwrap();
    manager.probe().await.unwrap();
    let handle = manager
        .spawn(SpawnRequest::new(Command::new(
            "python3",
            [
                "-c",
                "import sys; sys.stdout.buffer.write(b'\\x00\\xffSSH')",
            ],
        )))
        .await
        .unwrap();
    manager
        .get(handle.process.clone())
        .wait(Some(Duration::from_secs(5)))
        .await
        .unwrap();
    assert_eq!(
        page_bytes(
            &manager
                .get(handle.process.clone())
                .output()
                .read(None, 3)
                .await
                .unwrap()
        ),
        b"\x00\xffS"
    );
}

#[tokio::test]
async fn real_pueue_task_environment_does_not_change_the_client_environment() {
    let (_daemon, manager) = pueue_fixture().await;
    let directory = TempDir::with_prefix("opensol ' cwd $() ").unwrap();
    let program = directory.path().join("run=python3 '$()");
    std::os::unix::fs::symlink("/usr/bin/python3", &program).unwrap();
    let value = "value '\" $()\n你好";
    let mut command = Command::new(
        program.to_str().unwrap(),
        [
            "-c",
            "import os,sys; assert os.getcwd()==sys.argv[2]; assert os.environ['PATH']=='/sdk-task-only-path'; assert os.environ['HOME']==sys.argv[1]; assert os.environ['PUEUE_CONFIG_PATH']==sys.argv[1]; assert os.environ['SDK-TEST']==sys.argv[1]; sys.stdout.buffer.write(os.environ['SDK_TEST'].encode())",
            value,
            directory.path().to_str().unwrap(),
        ],
    );
    command.cwd = Some(directory.path().to_string_lossy().into());
    command.env = BTreeMap::from([
        ("PATH".into(), "/sdk-task-only-path".into()),
        ("HOME".into(), value.into()),
        ("PUEUE_CONFIG_PATH".into(), value.into()),
        ("SDK_TEST".into(), value.into()),
        ("SDK-TEST".into(), value.into()),
    ]);
    let handle = manager.spawn(SpawnRequest::new(command)).await.unwrap();
    let (output, outcome) = collect(handle.events).await;
    assert_eq!(combined_output(&output), value.as_bytes());
    assert!(outcome.exit.unwrap().success());
}

#[tokio::test]
async fn real_pueue_stdin_signal_and_wait_deadline() {
    let (_daemon, manager) = pueue_fixture().await;
    let mut request = SpawnRequest::new(Command::shell("read -r value; printf '%s' \"$value\""));
    request.stdin = true;
    let handle = manager.spawn(request).await.unwrap();
    for _attempt in 0..100 {
        if manager
            .get(handle.process.clone())
            .inspect()
            .await
            .unwrap()
            .state
            == opensol_core::ProcessState::Running
        {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let restored = ProcessRef::new("pueue", &handle.process.id, None);
    manager
        .get(restored.clone())
        .send_input(b"hello ' $()\n".to_vec())
        .await
        .unwrap();
    let (output, outcome) = collect(handle.events).await;
    assert!(outcome.exit.unwrap().success());
    assert_eq!(combined_output(&output), b"hello ' $()");
    let handle = manager
        .spawn(SpawnRequest::new(Command::shell("sleep 30")))
        .await
        .unwrap();
    assert_eq!(
        manager
            .get(handle.process.clone())
            .wait(Some(Duration::from_millis(50)))
            .await
            .unwrap_err()
            .kind,
        ErrorKind::Timeout
    );
    for _attempt in 0..150 {
        if manager
            .get(handle.process.clone())
            .inspect()
            .await
            .unwrap()
            .state
            == opensol_core::ProcessState::Running
        {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert_eq!(
        manager
            .get(handle.process.clone())
            .inspect()
            .await
            .unwrap()
            .state,
        opensol_core::ProcessState::Running
    );
    manager
        .get(handle.process.clone())
        .signal(Signal::Kill)
        .await
        .unwrap();
    let outcome = manager
        .get(handle.process.clone())
        .wait(Some(Duration::from_secs(5)))
        .await
        .unwrap();
    assert_eq!(outcome.native_result.as_deref(), Some("Killed"));
    assert!(outcome.exit.is_none());
    assert_eq!(
        manager
            .get(handle.process.clone())
            .inspect()
            .await
            .unwrap()
            .outcome,
        Some(outcome.clone())
    );
    let (output, completed) = collect(
        manager
            .get(handle.process.clone())
            .output()
            .subscribe()
            .await
            .unwrap()
            .events,
    )
    .await;
    assert!(output.is_empty());
    assert_eq!(completed, outcome);
    assert_eq!(
        manager
            .get(handle.process.clone())
            .send_input(vec![0xff])
            .await
            .unwrap_err()
            .kind,
        ErrorKind::Unsupported
    );
}

#[tokio::test]
async fn real_docker_lifecycle_exec_and_in_container_pueue() {
    let image = std::env::var("OPENSOL_TEST_IMAGE")
        .expect("run scripts/check.sh in the test container with Docker socket mounted");
    let backend = DockerLifecycle::connect_defaults(DockerConfig {
        image,
        command: vec!["sleep".into(), "infinity".into()],
        ..Default::default()
    })
    .unwrap();
    let lifecycle = backend.lifecycle();
    let resource = lifecycle.create(CreateOptions::default()).await.unwrap();
    let result = AssertUnwindSafe(async {
        assert_eq!(lifecycle.inspect(&resource).await?.state, ResourceState::Stopped);
        lifecycle.start(&resource).await?;
        let connection = backend.connection(&resource)?;
        let result = connection.exec(ExecRequest { command: Command::new("python3", ["-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stderr.buffer.write(b'\\xff'); sys.exit(7)"]), stdin: b"\x00\xffinput".to_vec(), timeout: Some(Duration::from_secs(5)) }).await?;
        assert_eq!(result.stdout, b"\x00\xffinput"); assert_eq!(result.stderr, b"\xff"); assert_eq!(result.exit.code, Some(7));
        for (command, stdout, code) in [
            (Command::new("true", Vec::<String>::new()), Vec::new(), 0),
            (Command::shell("exit 7"), Vec::new(), 7),
            (Command::new("head", ["-c", "1"]), vec![0xff], 0),
        ] {
            let result = tokio::time::timeout(Duration::from_secs(5), connection.exec(ExecRequest {
                command,
                stdin: vec![0xff; 10_000_000],
                timeout: None,
            })).await.expect("native exit must cancel unconsumed Docker stdin")?;
            assert_eq!(result.stdout, stdout);
            assert!(result.stderr.is_empty());
            assert_eq!(result.exit.code, Some(code));
        }
        let directory = "/tmp/opensol pueue ' $()";
        let config_path = format!("{directory}/pueue.yml");
        assert!(connection.exec(ExecRequest::new(Command::new("mkdir", ["-p", directory]))).await?.exit.success());
        let config = format!("shared:\n  pueue_directory: {directory}\n  runtime_directory: {directory}\nclient:\n  read_local_logs: true\n");
        assert!(connection.exec(ExecRequest { command: Command::new("tee", [&config_path]), stdin: config.into_bytes(), timeout: Some(Duration::from_secs(5)) }).await?.exit.success());
        let daemon = connection.exec(ExecRequest::new(Command::new("pueued", ["--config", config_path.as_str(), "--daemonize"]))).await?;
        assert!(daemon.exit.success());
        for _attempt in 0..100 {
            if connection.exec(ExecRequest::new(Command::new("pueue", ["--config", config_path.as_str(), "status", "--json"]))).await?.exit.success() { break; }
            tokio::time::sleep(Duration::from_millis(30)).await;
        }
        let manager = Pueue::new(connection, PueueConfig { config_file: Some(config_path), pueue_directory: Some(directory.into()), ..Default::default() }).map(ProcessManager::new)?;
        manager.probe().await?;
        let handle = manager.spawn(SpawnRequest::new(Command::new("printf", ["%s", "Docker + Pueue '$()"]))).await?;
        let (output, outcome) = collect(handle.events).await;
        assert_eq!(combined_output(&output), b"Docker + Pueue '$()"); assert!(outcome.exit.unwrap().success());
        assert_eq!(page_bytes(&manager.get(handle.process.clone()).output().read(None, 8).await?), b"Docker +");
        assert_eq!(lifecycle.fork(&resource).await.unwrap_err().kind, ErrorKind::Unsupported);
        lifecycle.stop(&resource).await?; assert_eq!(lifecycle.inspect(&resource).await?.state, ResourceState::Stopped);
        lifecycle.start(&resource).await?; assert_eq!(lifecycle.inspect(&resource).await?.state, ResourceState::Running);
        Ok::<(), Box<dyn std::error::Error>>(())
    }).catch_unwind().await;
    lifecycle.delete(&resource).await.unwrap();
    result.unwrap().unwrap();
    assert_eq!(
        lifecycle.inspect(&resource).await.unwrap_err().kind,
        ErrorKind::NotFound
    );
}
