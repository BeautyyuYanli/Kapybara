//! Native generated gRPC fixtures. These validate protocol boundaries, not live provider availability.

#![cfg(all(
    feature = "sandboxd",
    feature = "openshell",
    feature = "protocol-server"
))]

use std::{
    path::Path,
    pin::Pin,
    process::Command as NativeCommand,
    sync::{Arc, Mutex},
    time::Duration,
};

use futures::{Stream, stream};
use opensol_adapters::{
    grpc::GrpcConfig,
    openshell::{
        OpenShellCreate, OpenShellSandboxManager,
        protocol::{datamodel::v1 as datamodel, v1 as openshell},
    },
    sandboxd::{Sandboxd, protocol as sandboxd},
    secret::Secret,
};
use opensol_core::{Command, Connection, CreateOptions, ErrorKind, ExecRequest, SandboxState};
use tempfile::TempDir;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::{
    Request, Response, Status,
    transport::{Certificate, Identity, ServerTlsConfig},
};

type RpcStream<Message> = Pin<Box<dyn Stream<Item = std::result::Result<Message, Status>> + Send>>;

#[derive(Default)]
struct State {
    executes: Vec<sandboxd::ExecuteRequest>,
    creates: Vec<openshell::CreateSandboxRequest>,
    commands: Vec<openshell::ExecSandboxRequest>,
    operations: Vec<String>,
    phase: i32,
}

#[derive(Clone)]
struct NativeServer {
    state: Arc<Mutex<State>>,
}

fn authenticate<Message>(request: &Request<Message>) {
    assert_eq!(
        request.metadata().get("authorization").unwrap(),
        "Bearer grpc-secret"
    );
}

#[tonic::async_trait]
impl sandboxd::process_service_server::ProcessService for NativeServer {
    async fn execute(
        &self,
        request: Request<sandboxd::ExecuteRequest>,
    ) -> std::result::Result<Response<sandboxd::ExecuteResponse>, Status> {
        authenticate(&request);
        let request = request.into_inner();
        let command = request.config.as_ref().unwrap().command.clone();
        self.state.lock().unwrap().executes.push(request);
        match command[0].as_str() {
            "permission" => Err(Status::permission_denied("grpc-secret")),
            "slow" => {
                tokio::time::sleep(Duration::from_secs(1)).await;
                Ok(Response::new(sandboxd::ExecuteResponse::default()))
            }
            _ => Ok(Response::new(sandboxd::ExecuteResponse {
                stdout: vec![0, 0xff],
                stderr: vec![0x80],
                exit_code: 7,
            })),
        }
    }
}

impl NativeServer {
    fn sandbox(&self) -> openshell::SandboxResponse {
        openshell::SandboxResponse {
            sandbox: Some(openshell::Sandbox {
                metadata: Some(datamodel::ObjectMeta {
                    id: "uuid".into(),
                    name: "sandbox".into(),
                    workspace: "team".into(),
                    labels: [("project".into(), "native".into())].into(),
                    ..Default::default()
                }),
                status: Some(openshell::SandboxStatus {
                    phase: self.state.lock().unwrap().phase,
                    ..Default::default()
                }),
                ..Default::default()
            }),
            ..Default::default()
        }
    }
}

#[tonic::async_trait]
impl openshell::open_shell_server::OpenShell for NativeServer {
    async fn list_sandboxes(
        &self,
        request: Request<openshell::ListSandboxesRequest>,
    ) -> std::result::Result<Response<openshell::ListSandboxesResponse>, Status> {
        authenticate(&request);
        let request = request.into_inner();
        assert_eq!(
            request.workspace_scope.unwrap().selection,
            Some(datamodel::workspace_selector::Selection::Workspace(
                "team".into()
            ))
        );
        assert!(request.page_size > 0 && request.label_selector.is_empty());
        let mut sandbox = self.sandbox().sandbox.unwrap();
        let next_page_token = if request.page_token.is_empty() {
            "second-page".into()
        } else {
            assert_eq!(request.page_token, "second-page");
            sandbox.metadata.as_mut().unwrap().name = "stopped".into();
            sandbox.metadata.as_mut().unwrap().labels.clear();
            sandbox.status.as_mut().unwrap().phase = openshell::SandboxPhase::Stopped as i32;
            String::new()
        };
        Ok(Response::new(openshell::ListSandboxesResponse {
            sandboxes: vec![sandbox],
            next_page_token,
        }))
    }

    async fn create_sandbox(
        &self,
        request: Request<openshell::CreateSandboxRequest>,
    ) -> std::result::Result<Response<openshell::SandboxResponse>, Status> {
        authenticate(&request);
        self.state
            .lock()
            .unwrap()
            .creates
            .push(request.into_inner());
        Ok(Response::new(self.sandbox()))
    }

    async fn get_sandbox(
        &self,
        request: Request<openshell::GetSandboxRequest>,
    ) -> std::result::Result<Response<openshell::SandboxResponse>, Status> {
        authenticate(&request);
        assert_eq!(request.get_ref().name, "sandbox");
        let owner = request.metadata().contains_key("x-openshell-want-owner");
        let mut response = Response::new(self.sandbox());
        if owner {
            response
                .metadata_mut()
                .insert("x-openshell-owner", "replica-a".parse().unwrap());
        }
        Ok(response)
    }

    async fn start_sandbox(
        &self,
        request: Request<openshell::StartSandboxRequest>,
    ) -> std::result::Result<Response<openshell::SandboxResponse>, Status> {
        authenticate(&request);
        let request = request.into_inner();
        assert_eq!(request.name, "sandbox");
        {
            let mut state = self.state.lock().unwrap();
            state.operations.push("start".into());
            state.phase = openshell::SandboxPhase::Ready as i32;
        }
        Ok(Response::new(self.sandbox()))
    }

    async fn stop_sandbox(
        &self,
        request: Request<openshell::StopSandboxRequest>,
    ) -> std::result::Result<Response<openshell::SandboxResponse>, Status> {
        authenticate(&request);
        let request = request.into_inner();
        assert_eq!(request.name, "sandbox");
        {
            let mut state = self.state.lock().unwrap();
            state.operations.push("stop".into());
            state.phase = openshell::SandboxPhase::Stopped as i32;
        }
        Ok(Response::new(self.sandbox()))
    }

    async fn delete_sandbox(
        &self,
        request: Request<openshell::DeleteSandboxRequest>,
    ) -> std::result::Result<Response<openshell::DeleteSandboxResponse>, Status> {
        authenticate(&request);
        let request = request.into_inner();
        assert_eq!(request.name, "sandbox");
        assert!(!request.allow_missing);
        self.state.lock().unwrap().operations.push("delete".into());
        Ok(Response::new(openshell::DeleteSandboxResponse {
            outcome: openshell::DeletionOutcome::Accepted as i32,
            sandbox_id: "uuid".into(),
        }))
    }

    async fn exec_sandbox(
        &self,
        request: Request<openshell::ExecSandboxRequest>,
    ) -> std::result::Result<Response<RpcStream<openshell::ExecSandboxEvent>>, Status> {
        authenticate(&request);
        assert_eq!(
            request.metadata().get("x-openshell-replica").unwrap(),
            "replica-a"
        );
        let request = request.into_inner();
        let program = request.command[0].clone();
        self.state.lock().unwrap().commands.push(request);
        if program == "permission" {
            return Err(Status::permission_denied("grpc-secret"));
        }
        let mut events = vec![
            Ok(openshell::ExecSandboxEvent {
                payload: Some(openshell::exec_sandbox_event::Payload::Stdout(
                    openshell::ExecSandboxStdout {
                        data: vec![0, 0xff],
                    },
                )),
            }),
            Ok(openshell::ExecSandboxEvent {
                payload: Some(openshell::exec_sandbox_event::Payload::Stderr(
                    openshell::ExecSandboxStderr { data: vec![0x80] },
                )),
            }),
        ];
        if program != "no-exit" {
            events.push(Ok(openshell::ExecSandboxEvent {
                payload: Some(openshell::exec_sandbox_event::Payload::Exit(
                    openshell::ExecSandboxExit { exit_code: 7 },
                )),
            }));
        }
        Ok(Response::new(Box::pin(stream::iter(events))))
    }
}

struct Fixture {
    config: GrpcConfig,
    state: Arc<Mutex<State>>,
    task: tokio::task::JoinHandle<()>,
}

impl Drop for Fixture {
    fn drop(&mut self) {
        self.task.abort();
    }
}

impl Fixture {
    async fn start() -> Self {
        Self::serve(None).await
    }

    async fn serve(tls: Option<ServerTlsConfig>) -> Self {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = if tls.is_some() {
            format!(
                "https://localhost:{}",
                listener.local_addr().unwrap().port()
            )
        } else {
            format!("http://{}", listener.local_addr().unwrap())
        };
        let mut config = GrpcConfig::new(endpoint);
        config.bearer_token = Some(Secret::new("grpc-secret"));
        let state = Arc::new(Mutex::new(State {
            phase: openshell::SandboxPhase::Ready as i32,
            ..Default::default()
        }));
        let server = NativeServer {
            state: state.clone(),
        };
        let mut builder = tonic::transport::Server::builder();
        if let Some(tls) = tls {
            builder = builder.tls_config(tls).unwrap();
        }
        let task = tokio::spawn(async move {
            builder
                .add_service(sandboxd::process_service_server::ProcessServiceServer::new(
                    server.clone(),
                ))
                .add_service(openshell::open_shell_server::OpenShellServer::new(server))
                .serve_with_incoming(TcpListenerStream::new(listener))
                .await
                .unwrap();
        });
        Self {
            config,
            state,
            task,
        }
    }
}

struct TlsCertificates {
    ca: String,
    server: Identity,
    client_certificate: String,
    client_key: String,
}

fn openssl(directory: &Path, args: &[&str]) {
    let output = NativeCommand::new("openssl")
        .args(args)
        .current_dir(directory)
        .output()
        .expect("OpenSSL is required by Dockerfile.tests");
    assert!(
        output.status.success(),
        "TLS fixture generation failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn tls_certificates() -> TlsCertificates {
    let temporary = TempDir::new().unwrap();
    let directory = temporary.path();
    openssl(
        directory,
        &[
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            "ca.key",
            "-out",
            "ca.pem",
            "-subj",
            "/CN=opensol-test-ca",
            "-days",
            "1",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        ],
    );
    for (name, purpose) in [("server", "serverAuth"), ("client", "clientAuth")] {
        let key = format!("{name}.key");
        let request = format!("{name}.csr");
        let certificate = format!("{name}.pem");
        openssl(
            directory,
            &[
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                &key,
                "-out",
                &request,
                "-subj",
                &format!("/CN={name}"),
                "-addext",
                &format!("extendedKeyUsage={purpose}"),
                "-addext",
                "subjectAltName=DNS:localhost",
            ],
        );
        openssl(
            directory,
            &[
                "x509",
                "-req",
                "-in",
                &request,
                "-CA",
                "ca.pem",
                "-CAkey",
                "ca.key",
                "-CAcreateserial",
                "-out",
                &certificate,
                "-days",
                "1",
                "-copy_extensions",
                "copy",
            ],
        );
    }
    TlsCertificates {
        ca: std::fs::read_to_string(directory.join("ca.pem")).unwrap(),
        server: Identity::from_pem(
            std::fs::read(directory.join("server.pem")).unwrap(),
            std::fs::read(directory.join("server.key")).unwrap(),
        ),
        client_certificate: std::fs::read_to_string(directory.join("client.pem")).unwrap(),
        client_key: std::fs::read_to_string(directory.join("client.key")).unwrap(),
    }
}

async fn execute_with_config(config: GrpcConfig) -> opensol_core::Result<opensol_core::ExecResult> {
    Connection::new(Sandboxd::connect(config).await?)
        .exec(ExecRequest::new(Command::new(
            "program",
            Vec::<String>::new(),
        )))
        .await
}

#[tokio::test]
async fn grpc_tls_verifies_trust_and_server_hostname() {
    let certificates = tls_certificates();
    let fixture = Fixture::serve(Some(ServerTlsConfig::new().identity(certificates.server))).await;
    let mut trusted = fixture.config.clone();
    trusted.ca_certificate = Some(Secret::new(certificates.ca));
    let result = execute_with_config(trusted.clone()).await.unwrap();
    assert_eq!(result.stdout, [0, 0xff]);
    assert_eq!(result.stderr, [0x80]);
    assert_eq!(result.exit.code, Some(7));
    let mut wrong_hostname = trusted;
    wrong_hostname.endpoint = wrong_hostname.endpoint.replace("localhost", "127.0.0.1");
    for config in [fixture.config.clone(), wrong_hostname] {
        assert_eq!(
            execute_with_config(config).await.unwrap_err().kind,
            ErrorKind::Transport
        );
    }
    assert_eq!(fixture.state.lock().unwrap().executes.len(), 1);
}

#[tokio::test]
async fn grpc_mtls_requires_and_accepts_a_trusted_client_identity() {
    let certificates = tls_certificates();
    let fixture = Fixture::serve(Some(
        ServerTlsConfig::new()
            .identity(certificates.server)
            .client_ca_root(Certificate::from_pem(&certificates.ca)),
    ))
    .await;
    let mut config = fixture.config.clone();
    config.ca_certificate = Some(Secret::new(certificates.ca));
    let error = execute_with_config(config.clone())
        .await
        .expect_err("mTLS must reject a client without an identity");
    assert_eq!(error.backend, "sandboxd");
    config.client_identity = Some((
        Secret::new(certificates.client_certificate),
        Secret::new(certificates.client_key),
    ));
    let result = execute_with_config(config).await.unwrap();
    assert_eq!(result.stdout, [0, 0xff]);
    assert_eq!(result.exit.code, Some(7));
    assert_eq!(fixture.state.lock().unwrap().executes.len(), 1);
}

#[tokio::test]
async fn grpc_tls_handshake_deadline_is_distinct_from_transport_failure() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let mut config = GrpcConfig::new(format!("https://{}", listener.local_addr().unwrap()));
    config.request_timeout = Duration::from_millis(50);
    let task = tokio::spawn(async move {
        let (_socket, _address) = listener.accept().await.unwrap();
        std::future::pending::<()>().await;
    });
    let error = Sandboxd::connect(config)
        .await
        .err()
        .expect("TLS handshake must time out");
    task.abort();
    assert_eq!(error.kind, ErrorKind::Timeout);
    assert_eq!(error.operation, "connect");
}

#[tokio::test]
async fn sandboxd_execute_bytes_arguments_deadline_and_unsupported_stdin() {
    let fixture = Fixture::start().await;
    assert!(!format!("{:?}", fixture.config).contains("grpc-secret"));
    let connection = Connection::new(Sandboxd::connect(fixture.config.clone()).await.unwrap());
    let mut command = Command::new("printf", ["%s", "' $()"]);
    command.cwd = Some("/work".into());
    command.env.insert("KEY".into(), "value".into());
    let result = connection.exec(ExecRequest::new(command)).await.unwrap();
    assert_eq!(result.stdout, [0, 0xff]);
    assert_eq!(result.stderr, [0x80]);
    assert_eq!(result.exit.code, Some(7));
    let error = connection
        .exec(ExecRequest {
            command: Command::shell("cat"),
            stdin: vec![1],
            timeout: None,
        })
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::Unsupported);
    let error = connection
        .exec(ExecRequest::new(Command::new(
            "permission",
            Vec::<String>::new(),
        )))
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::Permission);
    assert!(!error.to_string().contains("grpc-secret"));
    let error = connection
        .exec(ExecRequest {
            command: Command::new("slow", Vec::<String>::new()),
            stdin: Vec::new(),
            timeout: Some(Duration::from_millis(80)),
        })
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::Timeout);
    let state = fixture.state.lock().unwrap();
    assert_eq!(state.executes.len(), 3);
    let config = state.executes[0].config.as_ref().unwrap();
    assert_eq!(config.command, ["printf", "%s", "' $()"]);
    assert_eq!(config.cwd.as_deref(), Some("/work"));
    assert_eq!(config.env_vars["KEY"], "value");
}

#[tokio::test]
async fn openshell_native_lifecycle_exec_routing_and_failure_boundaries() {
    let fixture = Fixture::start().await;
    let backend = OpenShellSandboxManager::connect(
        fixture.config.clone(),
        "team",
        OpenShellCreate {
            workload_template: Some("python".into()),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    let lifecycle = backend.sandboxes();
    let listed = lifecycle.list().await.unwrap();
    assert_eq!(listed.len(), 2);
    assert_eq!(listed[0].name.as_deref(), Some("sandbox"));
    assert_eq!(listed[0].labels["project"], "native");
    assert_eq!(listed[1].name.as_deref(), Some("stopped"));
    assert!(listed[1].labels.is_empty());
    assert_eq!(
        (&*listed[0].sandbox.id, listed[0].state),
        ("sandbox", SandboxState::Running)
    );
    assert_eq!(
        (&*listed[1].sandbox.id, listed[1].state),
        ("stopped", SandboxState::Stopped)
    );
    let resource = lifecycle
        .create(CreateOptions {
            name: Some("sandbox".into()),
            ..Default::default()
        })
        .await
        .unwrap();
    assert_eq!(resource.reference().id, "sandbox");
    assert_eq!(resource.inspect().await.unwrap(), listed[0]);
    assert_eq!(
        resource.inspect().await.unwrap().state,
        SandboxState::Running
    );
    resource.stop().await.unwrap();
    assert_eq!(
        resource.inspect().await.unwrap().state,
        SandboxState::Stopped
    );
    resource.start().await.unwrap();
    let connection = backend.connection(resource.reference()).unwrap();
    let mut command = Command::new("printf", ["%s", "' $()"]);
    command.cwd = Some("/work".into());
    command.env.insert("KEY".into(), "value".into());
    let result = connection
        .exec(ExecRequest {
            command,
            stdin: vec![0xff],
            timeout: Some(Duration::from_secs(2)),
        })
        .await
        .unwrap();
    assert_eq!(result.stdout, [0, 0xff]);
    assert_eq!(result.stderr, [0x80]);
    assert_eq!(result.exit.code, Some(7));
    assert_eq!(
        connection
            .exec(ExecRequest::new(Command::new(
                "no-exit",
                Vec::<String>::new()
            )))
            .await
            .unwrap_err()
            .kind,
        ErrorKind::Transport
    );
    let error = connection
        .exec(ExecRequest::new(Command::new(
            "permission",
            Vec::<String>::new(),
        )))
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::Permission);
    assert!(!error.to_string().contains("grpc-secret"));
    assert_eq!(
        resource.fork().await.unwrap_err().kind,
        ErrorKind::Unsupported
    );
    resource.delete().await.unwrap();
    let state = fixture.state.lock().unwrap();
    assert_eq!(state.creates.len(), 1);
    assert_eq!(state.commands.len(), 3);
    assert_eq!(state.operations, ["stop", "start", "delete"]);
    let create = &state.creates[0];
    assert_eq!(create.workload_template, "python");
    assert!(create.spec.is_none());
    assert_eq!(
        create.workspace_scope.as_ref().unwrap().selection,
        Some(datamodel::workspace_selector::Selection::Workspace(
            "team".into()
        ))
    );
    let command = &state.commands[0];
    assert_eq!(command.command, ["printf", "%s", "' $()"]);
    assert_eq!(command.stdin, [0xff]);
    assert!(command.no_login_shell);
    assert!(!command.tty);
    assert_eq!(command.execution_timeout.unwrap().seconds, 2);
}
