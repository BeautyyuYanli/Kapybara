//! Unified entry points against caller-owned runtimes and pre-I/O binding errors.

#![cfg(feature = "all")]

use futures::FutureExt;
use opensol_adapters::{
    BackendConfig,
    agent_sandbox::AgentSandboxConfig,
    docker::{DockerConfig, DockerSandboxSpec},
    e2b::E2bConfig,
    envd::EnvdConfig,
    local::LocalConfig,
    openshell::{OpenShellConfig, OpenShellSandboxSpec},
    pueue::PueueConfig,
    sandboxd::SandboxdConfig,
    secret::Secret,
    ssh::SshConfig,
    tmux::TmuxConfig,
};
use opensol_core::{
    Backend, Command, Connection, CreateOptions, ErrorKind, ExecRequest, ProcessManager,
    SandboxManager, SandboxRef,
};
use std::panic::AssertUnwindSafe;

#[tokio::test]
async fn missing_targets_and_unsupported_apis_fail_before_network_connections() {
    let unavailable = "http://127.0.0.1:1";
    let cases = [
        BackendConfig::Local(LocalConfig::default()),
        BackendConfig::Ssh(SshConfig::new("worker")),
        BackendConfig::Docker(DockerConfig::default()),
        BackendConfig::E2b(E2bConfig::new(Secret::new("unused"))),
        BackendConfig::Envd(EnvdConfig::new(unavailable)),
        BackendConfig::AgentSandbox(AgentSandboxConfig::new("work")),
        BackendConfig::OpenShell(OpenShellConfig::new(unavailable, "work")),
        BackendConfig::Sandboxd(SandboxdConfig::new(unavailable)),
        BackendConfig::Pueue(PueueConfig::default()),
        BackendConfig::Tmux(TmuxConfig::new("/tmp/caller-owned")),
    ];
    for config in cases {
        let kind = config.backend();
        if !matches!(kind, "docker" | "e2b" | "agent-sandbox" | "openshell") {
            assert_eq!(
                SandboxManager::open(config.clone())
                    .await
                    .err()
                    .unwrap()
                    .kind,
                ErrorKind::Unsupported,
                "{kind}"
            );
        }
        if matches!(kind, "docker" | "e2b" | "openshell") {
            assert_eq!(
                Connection::open(config.clone(), None)
                    .await
                    .err()
                    .unwrap()
                    .kind,
                ErrorKind::InvalidArgument,
                "{kind}"
            );
            assert_eq!(
                Connection::open(config.clone(), Some(SandboxRef::new("other", "id")))
                    .await
                    .err()
                    .unwrap()
                    .kind,
                ErrorKind::InvalidArgument,
                "{kind}"
            );
        }
        if !matches!(kind, "e2b" | "envd" | "pueue" | "tmux") {
            assert_eq!(
                ProcessManager::open(config.clone(), None, None)
                    .await
                    .err()
                    .unwrap()
                    .kind,
                ErrorKind::Unsupported,
                "{kind}"
            );
        }
        if matches!(kind, "pueue" | "tmux") {
            assert_eq!(
                Backend::open(config.clone(), None)
                    .await
                    .err()
                    .unwrap()
                    .kind,
                ErrorKind::InvalidArgument
            );
            assert_eq!(
                ProcessManager::open(config.clone(), None, None)
                    .await
                    .err()
                    .unwrap()
                    .kind,
                ErrorKind::InvalidArgument
            );
        }
        // Configuration serialization is independent of opening the native client.
        let encoded = serde_json::to_string(&config).unwrap();
        let restored: BackendConfig = serde_json::from_str(&encoded).unwrap();
        assert_eq!(restored.backend(), kind);
    }
}

#[tokio::test]
async fn injected_dependencies_are_retained_and_cannot_be_overridden() {
    let connection = Connection::open(LocalConfig::default(), None)
        .await
        .unwrap();
    let backend = Backend::open(
        TmuxConfig::new("/tmp/opensol-never-created.sock"),
        Some(connection.clone()),
    )
    .await
    .unwrap();
    let manager = ProcessManager::open(&backend, None, None).await.unwrap();
    assert!(manager.capabilities().list);
    assert_eq!(
        ProcessManager::open(&backend, None, Some(connection.clone()))
            .await
            .err()
            .unwrap()
            .kind,
        ErrorKind::InvalidArgument
    );
    assert_eq!(
        ProcessManager::open(&backend, Some(SandboxRef::new("docker", "id")), None)
            .await
            .err()
            .unwrap()
            .kind,
        ErrorKind::InvalidArgument
    );
    assert_eq!(
        Backend::open(LocalConfig::default(), Some(connection))
            .await
            .err()
            .unwrap()
            .kind,
        ErrorKind::InvalidArgument
    );
}

#[tokio::test]
async fn one_docker_context_creates_independent_specs_and_binds_existing_targets() {
    let backend = Backend::open(DockerConfig::default(), None).await.unwrap();
    let manager = SandboxManager::open(&backend).await.unwrap();
    let image = std::env::var("OPENSOL_TEST_IMAGE").expect("container checks supply an image");
    let first = manager
        .create(DockerSandboxSpec {
            image: image.clone(),
            command: vec!["sleep".into(), "infinity".into()],
            env: [("SCOPE".into(), "first".into())].into(),
            working_dir: Some("/tmp".into()),
            ..Default::default()
        })
        .await
        .unwrap();
    let result = AssertUnwindSafe(async {
        let second = manager
            .create(DockerSandboxSpec {
                image,
                command: vec!["sleep".into(), "600".into()],
                env: [("SCOPE".into(), "second".into())].into(),
                working_dir: Some("/".into()),
                ..Default::default()
            })
            .await?;
        let execution = AssertUnwindSafe(async {
            first.start().await?;
            second.start().await?;
            for (sandbox, expected) in [
                (&first, b"first:/tmp".as_slice()),
                (&second, b"second:/".as_slice()),
            ] {
                let client = Connection::open(&backend, Some(sandbox.reference().clone())).await?;
                assert_eq!(
                    client
                        .exec(ExecRequest::new(Command::shell(
                            "printf '%s:%s' \"$SCOPE\" \"$PWD\""
                        )))
                        .await?
                        .stdout,
                    expected
                );
            }
            assert_eq!(
                manager
                    .create(opensol_adapters::e2b::E2bSandboxSpec::default())
                    .await
                    .err()
                    .unwrap()
                    .kind,
                ErrorKind::InvalidArgument
            );
            Ok::<_, opensol_core::Error>(())
        })
        .catch_unwind()
        .await;
        let deletion = second.delete().await;
        execution.unwrap()?;
        deletion
    })
    .catch_unwind()
    .await;
    let deletion = first.delete().await;
    result.unwrap().unwrap();
    deletion.unwrap();
}

#[test]
fn native_openshell_spec_is_serializable_without_a_gateway() {
    let spec = OpenShellSandboxSpec {
        options: CreateOptions {
            name: Some("saved".into()),
            ..Default::default()
        },
        spec: Some(Default::default()),
        workload_template: None,
    };
    let encoded = serde_json::to_vec(&spec).unwrap();
    let restored: OpenShellSandboxSpec = serde_json::from_slice(&encoded).unwrap();
    assert_eq!(restored.options, spec.options);
    assert!(restored.spec.is_some());
}
