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
    sync::Arc,
    time::Duration,
};

use agent_sandbox_adapters::{
    docker::{DockerConfig, DockerLifecycle},
    local::Local,
    pueue::{Pueue, PueueConfig},
    ssh::{Ssh, SshConfig},
};
use agent_sandbox_core::{
    Command, Connection, CreateOptions, ErrorKind, ExecRequest, Lifecycle, OutputChannel,
    ProcessEvent, ProcessManager, ProcessRef, PtySize, ResourceState, Signal, SpawnRequest,
};
use futures::{FutureExt, StreamExt};
use tempfile::TempDir;

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
    mut events: agent_sandbox_core::EventStream,
) -> (
    Vec<(OutputChannel, Vec<u8>)>,
    agent_sandbox_core::ExitStatus,
) {
    let mut output = Vec::new();
    while let Some(event) = events.next().await {
        match event.unwrap() {
            ProcessEvent::Output { channel, data } => output.push((channel, data)),
            ProcessEvent::Exit(exit) => return (output, exit),
        }
    }
    panic!("output ended without exit")
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
    let directory = TempDir::with_prefix("agent-sdk ' cwd ").unwrap();
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
    let result = Local
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
    let result = Local
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
        Local
            .exec(ExecRequest::new(Command::new(
                "/no-such-sdk-program",
                Vec::<String>::new()
            )))
            .await
            .unwrap_err()
            .kind,
        ErrorKind::NotFound
    );
    let result = Local
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

async fn ssh_fixture() -> (Daemon, Ssh, PathBuf) {
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
    let ssh = Ssh::new(config).unwrap();
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
    let directory = TempDir::with_prefix("agent-sdk ' cwd $() ").unwrap();
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

async fn pueue_fixture() -> (Daemon, Pueue) {
    let directory = TempDir::new().unwrap();
    let path = directory.path().join("pueue.yml");
    std::fs::write(&path, format!("shared:\n  pueue_directory: {}\n  runtime_directory: {}\nclient:\n  read_local_logs: true\n", directory.path().display(), directory.path().display())).unwrap();
    let config = PueueConfig {
        config_file: Some(path.to_string_lossy().into()),
        ..Default::default()
    };
    let manager = Pueue::new(Arc::new(Local), config).unwrap();
    let mut command = NativeCommand::new("pueued");
    command.arg("--config").arg(&path);
    let daemon = start_daemon(command, directory);
    for _attempt in 0..100 {
        let ready = Local
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
    let (output, exit) = collect(handle.events).await;
    assert_eq!(combined_output(&output), b"\x00\xffout\nerr\n");
    assert_eq!(exit.code, Some(7));
    assert_eq!(
        manager.status(&process).await.unwrap().exit.unwrap().code,
        Some(7)
    );
    let (again, exit) = collect(manager.reconnect(&process).await.unwrap().events).await;
    assert_eq!(combined_output(&again), b"\x00\xffout\nerr\n");
    assert_eq!(exit.code, Some(7));
    let missing = ProcessRef::new("pueue", "999999999", false);
    assert_eq!(
        manager
            .wait(&missing, Some(Duration::from_secs(2)))
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
async fn real_pueue_task_environment_does_not_change_the_client_environment() {
    let (_daemon, manager) = pueue_fixture().await;
    let directory = TempDir::with_prefix("agent-sdk ' cwd $() ").unwrap();
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
    let (output, exit) = collect(handle.events).await;
    assert_eq!(combined_output(&output), value.as_bytes());
    assert!(exit.success());
}

#[tokio::test]
async fn real_pueue_stdin_signal_and_wait_deadline() {
    let (_daemon, manager) = pueue_fixture().await;
    let mut request = SpawnRequest::new(Command::shell("read -r value; printf '%s' \"$value\""));
    request.stdin = true;
    let handle = manager.spawn(request).await.unwrap();
    for _attempt in 0..100 {
        if manager.status(&handle.process).await.unwrap().state
            == agent_sandbox_core::ProcessState::Running
        {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    manager
        .send_input(&handle.process, b"hello ' $()\n".to_vec())
        .await
        .unwrap();
    let (output, exit) = collect(handle.events).await;
    assert!(exit.success());
    assert_eq!(combined_output(&output), b"hello ' $()");
    let handle = manager
        .spawn(SpawnRequest::new(Command::shell("sleep 30")))
        .await
        .unwrap();
    assert_eq!(
        manager
            .wait(&handle.process, Some(Duration::from_millis(50)))
            .await
            .unwrap_err()
            .kind,
        ErrorKind::Timeout
    );
    for _attempt in 0..150 {
        if manager.status(&handle.process).await.unwrap().state
            == agent_sandbox_core::ProcessState::Running
        {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert_eq!(
        manager.status(&handle.process).await.unwrap().state,
        agent_sandbox_core::ProcessState::Running
    );
    manager.signal(&handle.process, Signal::Kill).await.unwrap();
    assert!(
        !manager
            .wait(&handle.process, Some(Duration::from_secs(5)))
            .await
            .unwrap()
            .success()
    );
    assert_eq!(
        manager
            .send_input(&handle.process, vec![0xff])
            .await
            .unwrap_err()
            .kind,
        ErrorKind::Unsupported
    );
}

#[tokio::test]
async fn real_docker_lifecycle_exec_and_in_container_pueue() {
    let image = std::env::var("AGENT_SANDBOX_TEST_IMAGE")
        .expect("run scripts/check.sh in the test container with Docker socket mounted");
    let lifecycle = DockerLifecycle::connect_defaults(DockerConfig {
        image,
        command: vec!["sleep".into(), "infinity".into()],
        ..Default::default()
    })
    .unwrap();
    let resource = lifecycle.create(CreateOptions::default()).await.unwrap();
    let result = AssertUnwindSafe(async {
        assert_eq!(lifecycle.inspect(&resource).await?.state, ResourceState::Stopped);
        lifecycle.start(&resource).await?;
        let connection = Arc::new(lifecycle.connection(&resource)?);
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
        let daemon = connection.exec(ExecRequest::new(Command::new("pueued", ["--daemonize"]))).await?;
        assert!(daemon.exit.success());
        for _attempt in 0..100 {
            if connection.exec(ExecRequest::new(Command::new("pueue", ["status", "--json"]))).await?.exit.success() { break; }
            tokio::time::sleep(Duration::from_millis(30)).await;
        }
        let manager = Pueue::new(connection, PueueConfig::default())?;
        let handle = manager.spawn(SpawnRequest::new(Command::new("printf", ["%s", "Docker + Pueue '$()"]))).await?;
        let (output, exit) = collect(handle.events).await;
        assert_eq!(combined_output(&output), b"Docker + Pueue '$()"); assert!(exit.success());
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
