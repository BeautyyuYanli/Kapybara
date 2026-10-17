//! Real tmux, PTY and Docker boundaries. Required by the container check; never host-skipped.

#![cfg(all(feature = "tmux", feature = "local", feature = "docker"))]

use std::{
    panic::AssertUnwindSafe,
    process::Command as NativeCommand,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use async_trait::async_trait;
use futures::{FutureExt, StreamExt};
use opensol_adapters::{
    docker::{DockerConfig, DockerLifecycle},
    local::Local,
    tmux::{Tmux, TmuxConfig},
};
use opensol_core::{
    Command, Connection, ConnectionCapabilities, CreateOptions, ErrorKind, ExecRequest, ExecResult,
    Process, ProcessEvent, ProcessManager, ProcessRef, ProcessState, PtySize, Result, Signal,
    SpawnRequest, raw::ExecAccess,
};
use tempfile::TempDir;

struct Server {
    directory: TempDir,
    socket: String,
}
impl Server {
    fn new() -> Self {
        let directory = TempDir::new().unwrap();
        let socket = directory
            .path()
            .join("tmux.sock")
            .to_str()
            .unwrap()
            .to_string();
        assert!(
            NativeCommand::new("tmux")
                .args([
                    "-S",
                    &socket,
                    "-f",
                    "/dev/null",
                    "new-session",
                    "-d",
                    "-s",
                    "user-session",
                    "sleep",
                    "infinity"
                ])
                .output()
                .unwrap()
                .status
                .success()
        );
        Self { directory, socket }
    }
    fn manager(&self) -> ProcessManager {
        ProcessManager::new(
            Tmux::new(Connection::new(Local), TmuxConfig::new(&self.socket)).unwrap(),
        )
    }
    fn native(&self, args: &[&str]) -> Vec<u8> {
        let result = NativeCommand::new("tmux")
            .args(["-N", "-S", &self.socket])
            .args(args)
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        result.stdout
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        let _ = NativeCommand::new("tmux")
            .args(["-N", "-S", &self.socket, "kill-server"])
            .output();
    }
}
fn terminal(command: Command) -> SpawnRequest {
    SpawnRequest {
        command,
        stdin: true,
        pty: Some(PtySize { cols: 80, rows: 24 }),
    }
}
fn pane_id(reference: &ProcessRef) -> String {
    serde_json::from_str::<serde_json::Value>(&reference.id).unwrap()["pane"]
        .as_str()
        .unwrap()
        .into()
}
async fn screen(process: &Process, needle: &[u8]) -> Vec<u8> {
    let end = tokio::time::Instant::now() + Duration::from_secs(5);
    loop {
        let screen = process.output().snapshot().await.unwrap();
        if screen.windows(needle.len()).any(|bytes| bytes == needle) {
            return screen;
        }
        assert!(
            tokio::time::Instant::now() < end,
            "expected native screen {:?}, got {:?}",
            String::from_utf8_lossy(needle),
            String::from_utf8_lossy(&screen)
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

#[tokio::test]
async fn native_tasks_preserve_argv_cwd_environment_short_exits_and_ownership() {
    let server = Server::new();
    let manager = server.manager();
    let versions = manager.probe().await.unwrap();
    assert!(versions.client_version.is_some());
    assert!(versions.server_version.is_some());
    assert!(
        manager.list().await.unwrap().is_empty(),
        "user session is never adopted"
    );
    let cwd = server.directory.path().join("cwd #{pane_id} ' \n;");
    std::fs::create_dir(&cwd).unwrap();
    let program = cwd.join("run=python3;");
    std::os::unix::fs::symlink("/usr/bin/python3", &program).unwrap();
    let value = "x '$()\n你好;";
    let mut command = Command::new(
        program.to_str().unwrap(),
        [
            "-c",
            "import os,sys; assert os.getcwd()==sys.argv[1]; assert os.environ['SDK_TEST']==sys.argv[2]==os.environ['SDK-TEST']; assert os.environ['PATH']=='/task-only'; assert sys.argv[3:]==[';', 'a;', r'a\\;', '#{pane_id}', 'a=b', '\"']; print('native-screen 你好'); sys.exit(47)",
            cwd.to_str().unwrap(),
            value,
            ";",
            "a;",
            "a\\;",
            "#{pane_id}",
            "a=b",
            "\"",
        ],
    );
    command.cwd = Some(cwd.to_str().unwrap().into());
    command.env.extend([
        ("PATH".into(), "/task-only".into()),
        ("SDK_TEST".into(), value.into()),
        ("SDK-TEST".into(), value.into()),
    ]);
    let mut handle = manager.spawn(terminal(command)).await.unwrap();
    assert_eq!(handle.process.pty, Some(true));
    let process = manager.get(handle.process.clone());
    assert_eq!(
        process
            .wait(Some(Duration::from_secs(5)))
            .await
            .unwrap()
            .exit
            .unwrap()
            .code,
        Some(47)
    );
    assert!(
        screen(&process, "native-screen 你好".as_bytes())
            .await
            .starts_with("native-screen 你好".as_bytes())
    );
    assert!(
        matches!(handle.events.next().await.unwrap().unwrap(), ProcessEvent::Completed(outcome) if outcome.exit.unwrap().code == Some(47))
    );
    assert!(handle.events.next().await.is_none());
    let listed = manager.list().await.unwrap();
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0].process, handle.process);
    assert_eq!(listed[0].state, ProcessState::Exited);
    assert_eq!(
        server
            .manager()
            .get(handle.process.clone())
            .inspect()
            .await
            .unwrap(),
        listed[0]
    );
    for script in ["exit 0", "exit 7", "exit 91"] {
        let handle = manager
            .spawn(terminal(Command::shell(script)))
            .await
            .unwrap();
        let task = manager.get(handle.process);
        assert_eq!(
            task.wait(Some(Duration::from_secs(5)))
                .await
                .unwrap()
                .exit
                .unwrap()
                .code,
            Some(script[5..].parse().unwrap())
        );
        task.remove().await.unwrap();
    }
    let signal = manager
        .spawn(terminal(Command::shell("kill -TERM $$")))
        .await
        .unwrap();
    let task = manager.get(signal.process);
    let outcome = task.wait(Some(Duration::from_secs(5))).await.unwrap();
    assert_eq!(outcome.exit.unwrap().signal, Some(15));
    assert_eq!(outcome.native_result.as_deref(), Some("SIGTERM"));
    task.remove().await.unwrap();
    process.remove().await.unwrap();
    assert!(manager.list().await.unwrap().is_empty());
    assert_eq!(
        server.native(&["list-sessions", "-F", "#{session_name}"]),
        b"user-session\n"
    );
}

#[tokio::test]
async fn explicit_cwd_is_required_on_initial_launch_and_each_native_restart() {
    let server = Server::new();
    let manager = server.manager();
    let cwd = server.directory.path().join("task-cwd");
    let marker = server.directory.path().join("user-command-ran");
    let mut command = Command::new(
        "python3",
        [
            "-c",
            "import os,sys; from pathlib import Path; Path(sys.argv[1]).write_text(os.getcwd()); sys.exit(7)",
            marker.to_str().unwrap(),
        ],
    );
    command.cwd = Some(cwd.to_str().unwrap().into());
    let handle = manager.spawn(terminal(command)).await.unwrap();
    let reference = handle.process;
    let process = manager.get(reference.clone());
    let outcome = process.wait(Some(Duration::from_secs(5))).await.unwrap();
    assert_eq!(outcome.exit.unwrap().code, Some(125));
    assert!(
        !marker.exists(),
        "missing cwd must not run user argv in tmux's fallback directory"
    );

    std::fs::create_dir(&cwd).unwrap();
    // Reconstruct the manager: only tmux retains the launch argv and cwd.
    let process = server.manager().get(reference.clone());
    process.restart().await.unwrap();
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
        std::fs::read_to_string(&marker).unwrap(),
        cwd.to_str().unwrap()
    );
    std::fs::remove_file(&marker).unwrap();
    std::fs::remove_dir(&cwd).unwrap();

    let process = server.manager().get(reference);
    process.restart().await.unwrap();
    assert_eq!(
        process
            .wait(Some(Duration::from_secs(5)))
            .await
            .unwrap()
            .exit
            .unwrap()
            .code,
        Some(125)
    );
    assert!(
        !marker.exists(),
        "restart must not run user argv after its cwd was removed"
    );
    process.remove().await.unwrap();
}

#[tokio::test]
async fn native_input_resize_snapshot_and_explicit_destruction() {
    let server = Server::new();
    let manager = server.manager();
    let handle = manager.spawn(terminal(Command::new("python3", ["-u", "-c",
        "import os,tty; tty.setraw(0); print('READY',flush=True); data=b''\nwhile len(data)<4: data+=os.read(0,4-len(data))\nprint(data.hex(), os.get_terminal_size(0), flush=True)"]))).await.unwrap();
    let process = manager.get(handle.process);
    screen(&process, b"READY").await;
    process.send_input(Vec::new()).await.unwrap();
    assert_eq!(
        process
            .wait(Some(Duration::from_millis(80)))
            .await
            .unwrap_err()
            .kind,
        ErrorKind::Timeout
    );
    assert_eq!(
        process.remove().await.unwrap_err().kind,
        ErrorKind::Conflict
    );
    assert_eq!(
        process.restart().await.unwrap_err().kind,
        ErrorKind::Conflict
    );
    for error in [
        process.pause().await.unwrap_err(),
        process.start().await.unwrap_err(),
        process.signal(Signal::Interrupt).await.unwrap_err(),
        process.close_stdin().await.unwrap_err(),
        process.output().read(None, 32).await.unwrap_err(),
        process.output().stat().await.unwrap_err(),
    ] {
        assert_eq!(error.kind, ErrorKind::Unsupported);
    }
    process
        .resize(PtySize { cols: 93, rows: 31 })
        .await
        .unwrap();
    process.send_input(vec![0, 255, 10, 65]).await.unwrap();
    // Native pane EOF may precede waitpid status. Dead without an OS status is a valid
    // optional outcome; this finite command must still provide its real exit code later.
    let exit = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let outcome = process.wait(None).await.unwrap();
            if let Some(exit) = outcome.exit {
                break exit;
            }
            assert_eq!(outcome.native_result.as_deref(), Some("Dead"));
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .unwrap();
    assert!(exit.success());
    let output = screen(&process, b"00ff0a41").await;
    assert!(
        String::from_utf8(output)
            .unwrap()
            .contains("columns=93, lines=31")
    );
    process.remove().await.unwrap();
    assert_eq!(
        process.inspect().await.unwrap_err().kind,
        ErrorKind::NotFound
    );
    let handle = manager
        .spawn(terminal(Command::new("sleep", ["60"])))
        .await
        .unwrap();
    let process = manager.get(handle.process);
    drop(handle.events);
    assert_eq!(
        process.inspect().await.unwrap().state,
        ProcessState::Running
    );
    process.kill().await.unwrap();
    assert_eq!(
        process.wait(None).await.unwrap_err().kind,
        ErrorKind::NotFound
    );
    assert_eq!(
        process.output().snapshot().await.unwrap_err().kind,
        ErrorKind::NotFound
    );
}

#[tokio::test]
async fn subscriptions_bind_generations_and_full_identity_survives_reconnection_only() {
    let server = Server::new();
    let manager = server.manager();
    let mut initial = manager
        .spawn(terminal(Command::shell("exit 7")))
        .await
        .unwrap();
    let process = manager.get(initial.process.clone());
    process.wait(Some(Duration::from_secs(5))).await.unwrap();
    let mut extra = process.output().subscribe().await.unwrap();
    process.restart().await.unwrap();
    assert_eq!(
        initial.events.next().await.unwrap().unwrap_err().kind,
        ErrorKind::Conflict
    );
    assert_eq!(
        extra.events.next().await.unwrap().unwrap_err().kind,
        ErrorKind::Conflict
    );
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
    let mut external = process.output().subscribe().await.unwrap();
    let pane = pane_id(process.reference());
    server.native(&["respawn-pane", "-t", &pane, "sleep", "30"]);
    assert_eq!(
        external.events.next().await.unwrap().unwrap_err().kind,
        ErrorKind::Conflict
    );
    let mut forged: serde_json::Value = serde_json::from_str(&initial.process.id).unwrap();
    forged["window"] = "@99999999".into();
    let forged = manager.get(ProcessRef::new("tmux", forged.to_string(), Some(true)));
    for error in [
        forged.inspect().await.unwrap_err(),
        forged.kill().await.unwrap_err(),
        forged.output().snapshot().await.unwrap_err(),
    ] {
        assert_eq!(error.kind, ErrorKind::NotFound);
    }
    assert_eq!(
        process.inspect().await.unwrap().state,
        ProcessState::Running
    );
    let mut cleaned = process.output().subscribe().await.unwrap();
    process.kill().await.unwrap();
    assert_eq!(
        cleaned.events.next().await.unwrap().unwrap_err().kind,
        ErrorKind::NotFound
    );
    let mut replaced = manager
        .spawn(terminal(Command::shell("exit 0")))
        .await
        .unwrap();
    manager
        .get(replaced.process.clone())
        .wait(Some(Duration::from_secs(5)))
        .await
        .unwrap();
    let session: serde_json::Value = serde_json::from_str(&replaced.process.id).unwrap();
    server.native(&[
        "rename-session",
        "-t",
        session["session"].as_str().unwrap(),
        "replaced-session",
    ]);
    assert_eq!(
        replaced.events.next().await.unwrap().unwrap_err().kind,
        ErrorKind::Conflict
    );
    server.native(&["kill-session", "-t", "replaced-session"]);
    server.native(&["kill-server"]);
    assert_eq!(
        manager.probe().await.unwrap_err().kind,
        ErrorKind::Transport
    );
    assert_eq!(
        process.send_input(vec![0; 8193]).await.unwrap_err().kind,
        ErrorKind::InvalidArgument
    );
    assert!(
        matches!(manager.spawn(SpawnRequest::new(Command::shell("true"))).await,
        Err(error) if error.kind == ErrorKind::Unsupported)
    );
    assert_eq!(
        manager.probe().await.unwrap_err().kind,
        ErrorKind::Transport,
        "unsupported spawn never starts a server"
    );
}

/// Intercepts a real control or observation boundary to replace an exited pane.
/// The race and all observations still use tmux itself, without fabricated responses.
#[derive(Clone)]
struct RespawnAtBoundary {
    socket: String,
    pane: String,
    command: &'static str,
    observation: usize,
    after: bool,
    observed: Arc<AtomicUsize>,
}
impl RespawnAtBoundary {
    async fn respawn(&self) -> Result<()> {
        let result = Connection::new(Local)
            .exec(ExecRequest::new(Command::new(
                "tmux",
                [
                    "-N",
                    "-S",
                    &self.socket,
                    "respawn-pane",
                    "-t",
                    &self.pane,
                    "sleep",
                    "30",
                ],
            )))
            .await?;
        assert!(result.exit.success());
        Ok(())
    }
}
#[async_trait]
impl ExecAccess for RespawnAtBoundary {
    fn backend(&self) -> &'static str {
        "local"
    }
    fn capabilities(&self) -> ConnectionCapabilities {
        Connection::new(Local).capabilities()
    }
    async fn exec(&self, request: ExecRequest) -> Result<ExecResult> {
        let trigger = request.command.argv.iter().any(|arg| arg == self.command)
            && self.observed.fetch_add(1, Ordering::SeqCst) + 1 == self.observation;
        if trigger && !self.after {
            self.respawn().await?;
        }
        let result = Connection::new(Local).exec(request).await;
        if trigger && self.after {
            self.respawn().await?;
        }
        result
    }
}

#[tokio::test]
async fn removal_tests_dead_state_inside_the_native_control_queue() {
    let server = Server::new();
    let manager = server.manager();
    let handle = manager
        .spawn(terminal(Command::shell("exit 0")))
        .await
        .unwrap();
    let process = manager.get(handle.process.clone());
    process.wait(Some(Duration::from_secs(5))).await.unwrap();
    let connection = Connection::new(RespawnAtBoundary {
        socket: server.socket.clone(),
        pane: pane_id(&handle.process),
        command: "if-shell",
        observation: 1,
        after: false,
        observed: Arc::new(AtomicUsize::new(0)),
    });
    let racing =
        ProcessManager::new(Tmux::new(connection, TmuxConfig::new(&server.socket)).unwrap());
    assert_eq!(
        racing.get(handle.process).remove().await.unwrap_err().kind,
        ErrorKind::Conflict
    );
    assert_eq!(
        process.inspect().await.unwrap().state,
        ProcessState::Running
    );
    process.kill().await.unwrap();
}

#[tokio::test]
async fn completion_is_rechecked_after_native_terminal_observation() {
    let server = Server::new();
    let manager = server.manager();
    let handle = manager
        .spawn(terminal(Command::shell("exit 7")))
        .await
        .unwrap();
    let process = manager.get(handle.process.clone());
    process.wait(Some(Duration::from_secs(5))).await.unwrap();
    let connection = Connection::new(RespawnAtBoundary {
        socket: server.socket.clone(),
        pane: pane_id(&handle.process),
        command: "display-message",
        observation: 2,
        after: true,
        observed: Arc::new(AtomicUsize::new(0)),
    });
    let racing =
        ProcessManager::new(Tmux::new(connection, TmuxConfig::new(&server.socket)).unwrap());
    let mut handle = racing
        .get(handle.process)
        .output()
        .subscribe()
        .await
        .unwrap();
    assert_eq!(
        handle.events.next().await.unwrap().unwrap_err().kind,
        ErrorKind::Conflict
    );
    assert_eq!(
        process.inspect().await.unwrap().state,
        ProcessState::Running
    );
    process.kill().await.unwrap();
}

#[tokio::test]
async fn caller_retained_empty_server_and_namespaces_keep_native_global_options() {
    let server = Server::new();
    // Server lifecycle and global configuration are explicit caller operations in this fixture.
    server.native(&["set-option", "-g", "exit-empty", "off"]);
    server.native(&["kill-session", "-t", "user-session"]);
    let manager = server.manager();
    manager.probe().await.unwrap();
    assert!(manager.list().await.unwrap().is_empty());
    let before = server.native(&["show-options", "-g", "-w", "-v", "remain-on-exit"]);
    let mut config = TmuxConfig::new(&server.socket);
    config.session_prefix = "another-team".into();
    let other = ProcessManager::new(Tmux::new(Connection::new(Local), config).unwrap());
    let a = manager
        .spawn(terminal(Command::shell("exit 0")))
        .await
        .unwrap();
    let b = other
        .spawn(terminal(Command::shell("exit 7")))
        .await
        .unwrap();
    assert_eq!(
        manager
            .list()
            .await
            .unwrap()
            .iter()
            .map(|info| &info.process)
            .collect::<Vec<_>>(),
        [&a.process]
    );
    assert_eq!(
        other
            .list()
            .await
            .unwrap()
            .iter()
            .map(|info| &info.process)
            .collect::<Vec<_>>(),
        [&b.process]
    );
    let a = manager.get(a.process);
    let b = other.get(b.process);
    a.wait(Some(Duration::from_secs(5))).await.unwrap();
    b.wait(Some(Duration::from_secs(5))).await.unwrap();
    a.remove().await.unwrap();
    b.remove().await.unwrap();
    manager.probe().await.unwrap();
    assert!(manager.list().await.unwrap().is_empty());
    assert_eq!(
        server.native(&["show-options", "-g", "-v", "exit-empty"]),
        b"off\n"
    );
    assert_eq!(
        server.native(&["show-options", "-g", "-w", "-v", "remain-on-exit"]),
        before
    );
}

#[tokio::test]
async fn docker_connection_uses_target_program_socket_environment_and_screen() {
    let server = Server::new();
    let image =
        std::env::var("OPENSOL_TEST_IMAGE").expect("container checks provide the test image");
    let backend = DockerLifecycle::connect_defaults(DockerConfig {
        image,
        command: vec!["sleep".into(), "infinity".into()],
        ..Default::default()
    })
    .unwrap();
    let lifecycle = backend.lifecycle();
    let resource = lifecycle.create(CreateOptions::default()).await.unwrap();
    let result = AssertUnwindSafe(async {
        lifecycle.start(&resource).await?;
        let connection = backend.connection(&resource)?;
        let directory = server.directory.path().to_str().unwrap();
        assert!(
            connection
                .exec(ExecRequest::new(Command::new("mkdir", ["-p", directory])))
                .await?
                .exit
                .success()
        );
        assert!(
            connection
                .exec(ExecRequest::new(Command::new(
                    "tmux",
                    [
                        "-S",
                        &server.socket,
                        "-f",
                        "/dev/null",
                        "new-session",
                        "-d",
                        "-s",
                        "remote-user",
                        "sleep",
                        "infinity"
                    ]
                )))
                .await?
                .exit
                .success()
        );
        let program = format!("{directory}/target-only-tmux");
        assert!(
            connection
                .exec(ExecRequest::new(Command::new(
                    "ln",
                    ["-s", "/usr/bin/tmux", &program]
                )))
                .await?
                .exit
                .success()
        );
        assert!(!std::path::Path::new(&program).exists());
        let mut config = TmuxConfig::new(&server.socket);
        config.program = program.into();
        let manager = ProcessManager::new(Tmux::new(connection.clone(), config)?);
        manager.probe().await?;
        assert!(manager.list().await?.is_empty());
        let mut command = Command::new(
            "python3",
            [
                "-c",
                "import os; assert os.environ['SDK_TEST']=='remote'; print('remote-screen')",
            ],
        );
        command.env.insert("SDK_TEST".into(), "remote".into());
        let handle = manager.spawn(terminal(command)).await?;
        let process = manager.get(handle.process);
        assert!(
            process
                .wait(Some(Duration::from_secs(5)))
                .await?
                .exit
                .unwrap()
                .success()
        );
        let snapshot = String::from_utf8(process.output().snapshot().await?).unwrap();
        assert!(
            snapshot.contains("remote-screen"),
            "native snapshot: {snapshot:?}"
        );
        assert!(
            server.manager().list().await?.is_empty(),
            "same socket path on SDK host must remain untouched"
        );
        process.remove().await?;
        Ok::<(), opensol_core::Error>(())
    })
    .catch_unwind()
    .await;
    lifecycle.delete(&resource).await.unwrap();
    result.unwrap().unwrap();
}
