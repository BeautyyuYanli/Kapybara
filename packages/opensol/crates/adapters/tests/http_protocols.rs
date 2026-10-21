//! Schema-faithful HTTP/ConnectRPC fixtures, not live E2B/envd end-to-end tests.

#![cfg(all(feature = "e2b", feature = "envd"))]

use std::{
    convert::Infallible,
    sync::{Arc, Mutex},
    time::Duration,
};

use axum::{
    Router,
    body::{Body, to_bytes},
    http::{HeaderMap, Request, Response, StatusCode},
};
use bytes::Bytes;
use futures::{StreamExt, stream};
use opensol_adapters::{
    e2b::{E2bConfig, E2bSandboxManager},
    envd::{Envd, EnvdConfig, protocol},
    secret::Secret,
};
use opensol_core::{
    Command, CreateOptions, ErrorKind, ExecRequest, OutputChannel, Process, ProcessEvent,
    ProcessRef, PtySize, SandboxRef, SandboxState, Signal, SpawnRequest,
};
use prost::Message;
use serde_json::{Value, json};
use tokio::sync::Notify;

#[derive(Clone)]
struct Recorded {
    method: String,
    path: String,
    query: String,
    headers: HeaderMap,
    body: Vec<u8>,
}

struct Fixture {
    endpoint: String,
    records: Arc<Mutex<Vec<Recorded>>>,
    task: tokio::task::JoinHandle<()>,
}

impl Drop for Fixture {
    fn drop(&mut self) {
        self.task.abort();
    }
}

impl Fixture {
    async fn start() -> Self {
        Self::with_input_error(None).await
    }

    async fn with_input_error(input_error: Option<(&'static str, &'static str)>) -> Self {
        let records = Arc::new(Mutex::new(Vec::new()));
        let stored = records.clone();
        let completion = Arc::new(Notify::new());
        let router = Router::new().fallback(move |request: Request<Body>| {
            let records = stored.clone();
            let completion = completion.clone();
            async move {
                let (parts, body) = request.into_parts();
                let body = to_bytes(body, 1024 * 1024).await.unwrap().to_vec();
                let record = Recorded {
                    method: parts.method.to_string(),
                    path: parts.uri.path().into(),
                    query: parts.uri.query().unwrap_or_default().into(),
                    headers: parts.headers,
                    body,
                };
                let response = reply(&record, input_error, completion);
                records.lock().unwrap().push(record);
                response
            }
        });
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = format!("http://{}", listener.local_addr().unwrap());
        let task = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        Self {
            endpoint,
            records,
            task,
        }
    }

    fn envd(&self) -> Envd {
        self.envd_with_timeout(Duration::from_secs(30))
    }

    fn envd_with_timeout(&self, timeout: Duration) -> Envd {
        let mut config = EnvdConfig::new(&self.endpoint);
        config.access_token = Some(Secret::new("envd-secret"));
        config.user = Some("user".into());
        config.request_timeout = timeout;
        Envd::new(config).unwrap()
    }
}

fn json_response(status: StatusCode, value: Value) -> Response<Body> {
    Response::builder()
        .status(status)
        .header("content-type", "application/json")
        .body(Body::from(value.to_string()))
        .unwrap()
}

fn sandbox(id: &str) -> Value {
    json!({ "sandboxID": id, "templateID": "base", "clientID": "client", "envdVersion": "1.0.0", "envdAccessToken": "envd-secret", "domain": null, "state": "running", "metadata": {"name": "native-name", "project": "native-project"} })
}

fn frame(flags: u8, payload: &[u8]) -> Vec<u8> {
    let mut framed = vec![flags];
    framed.extend((payload.len() as u32).to_be_bytes());
    framed.extend(payload);
    framed
}

fn process_event(event: protocol::process_event::Event) -> Vec<u8> {
    frame(
        0,
        &protocol::StartResponse {
            event: Some(protocol::ProcessEvent { event: Some(event) }),
        }
        .encode_to_vec(),
    )
}

fn output_response(program: &str, pty: bool, completion: Option<Arc<Notify>>) -> Response<Body> {
    let mut bytes = process_event(protocol::process_event::Event::Start(
        protocol::process_event::StartEvent { pid: 42 },
    ));
    if program == "bad-first" {
        bytes.clear();
    }
    let data = if pty != (program == "wrong-channel") {
        protocol::process_event::data_event::Output::Pty(vec![0xff, 0, b'T'])
    } else {
        protocol::process_event::data_event::Output::Stdout(vec![0xff, 0, b'O'])
    };
    bytes.extend(process_event(protocol::process_event::Event::Data(
        protocol::process_event::DataEvent { output: Some(data) },
    )));
    let prefix_length = bytes.len();
    if program == "never" {
        let chunks = stream::once(async move { Ok::<_, Infallible>(Bytes::from(bytes)) })
            .chain(stream::pending());
        return Response::builder()
            .header("content-type", "application/connect+proto")
            .body(Body::from_stream(chunks))
            .unwrap();
    }
    if program == "large-frame" {
        bytes.push(0);
        bytes.extend((17_u32 * 1024 * 1024).to_be_bytes());
    } else if program == "rpc-error" {
        bytes.extend(frame(
            2,
            br#"{"error":{"code":"permission_denied","message":"envd-secret"}}"#,
        ));
    } else {
        if program != "no-exit" && program != "bad-first" {
            if !pty {
                bytes.extend(process_event(protocol::process_event::Event::Data(
                    protocol::process_event::DataEvent {
                        output: Some(protocol::process_event::data_event::Output::Stderr(
                            b"error".to_vec(),
                        )),
                    },
                )));
            }
            bytes.extend(process_event(protocol::process_event::Event::End(
                protocol::process_event::EndEvent {
                    exit_code: if program == "invalid-exit" { 0 } else { 7 },
                    exited: true,
                    status: "exit status 7".into(),
                    error: (program == "invalid-exit").then(|| "envd-secret".into()),
                },
            )));
        }
        bytes.extend(frame(2, b"{}"));
    }
    let suffix = bytes.split_off(prefix_length);
    let prefix = stream::iter(
        bytes
            .into_iter()
            .map(|byte| Ok::<_, Infallible>(Bytes::from(vec![byte]))),
    );
    let suffix = stream::once(async move {
        if let Some(completion) = completion {
            completion.notified().await;
        }
        Ok::<_, Infallible>(Bytes::from(suffix))
    });
    Response::builder()
        .header("content-type", "application/connect+proto")
        .body(Body::from_stream(prefix.chain(suffix)))
        .unwrap()
}

fn assert_selector(process: Option<protocol::ProcessSelector>) {
    assert_eq!(
        process.unwrap().selector,
        Some(protocol::process_selector::Selector::Pid(42))
    );
}

fn reply(
    record: &Recorded,
    input_error: Option<(&str, &str)>,
    completion: Arc<Notify>,
) -> Response<Body> {
    if record.path.starts_with("/process.Process/") {
        assert_eq!(record.method, "POST");
        assert_eq!(record.headers["connect-protocol-version"], "1");
        assert_eq!(record.headers["x-access-token"], "envd-secret");
        assert!(!record.headers.contains_key("x-api-key"));
        let method = record.path.rsplit('/').next().unwrap();
        if method == "Start" || method == "Connect" {
            assert_eq!(record.headers["content-type"], "application/connect+proto");
            assert_eq!(record.body[0], 0);
            let length = u32::from_be_bytes(record.body[1..5].try_into().unwrap()) as usize;
            assert_eq!(record.body.len(), length + 5);
            if method == "Start" {
                let request = protocol::StartRequest::decode(&record.body[5..]).unwrap();
                let program = &request.process.unwrap().cmd;
                if program == "http-permission" {
                    return json_response(
                        StatusCode::FORBIDDEN,
                        json!({ "message": "envd-secret" }),
                    );
                }
                return output_response(
                    program,
                    request.pty.is_some(),
                    input_error.map(|_| completion),
                );
            }
            let request = protocol::ConnectRequest::decode(&record.body[5..]).unwrap();
            assert_eq!(
                request.process.unwrap().selector,
                Some(protocol::process_selector::Selector::Pid(42))
            );
            return output_response("connected", false, None);
        }
        assert_eq!(record.headers["content-type"], "application/proto");
        let bytes = match method {
            "List" => {
                protocol::ListRequest::decode(record.body.as_slice()).unwrap();
                protocol::ListResponse {
                    processes: vec![protocol::ProcessInfo {
                        config: None,
                        pid: 42,
                        tag: None,
                    }],
                }
                .encode_to_vec()
            }
            "SendInput" => {
                let request = protocol::SendInputRequest::decode(record.body.as_slice()).unwrap();
                assert_selector(request.process);
                assert!(matches!(
                    request.input.unwrap().input,
                    Some(
                        protocol::process_input::Input::Stdin(_)
                            | protocol::process_input::Input::Pty(_)
                    )
                ));
                protocol::SendInputResponse {}.encode_to_vec()
            }
            "CloseStdin" => {
                let request = protocol::CloseStdinRequest::decode(record.body.as_slice()).unwrap();
                assert_selector(request.process);
                protocol::CloseStdinResponse {}.encode_to_vec()
            }
            "SendSignal" => {
                let request = protocol::SendSignalRequest::decode(record.body.as_slice()).unwrap();
                assert_selector(request.process);
                assert!(matches!(request.signal, 9 | 15));
                protocol::SendSignalResponse {}.encode_to_vec()
            }
            "Update" => {
                let request = protocol::UpdateRequest::decode(record.body.as_slice()).unwrap();
                assert_selector(request.process);
                let size = request.pty.unwrap().size.unwrap();
                assert!(size.cols > 0 && size.rows > 0);
                protocol::UpdateResponse {}.encode_to_vec()
            }
            _ => {
                return json_response(StatusCode::NOT_FOUND, json!({ "code": "unimplemented" }));
            }
        };
        if let Some((failed_method, code)) = input_error
            && method == failed_method
        {
            completion.notify_one();
            let status = match code {
                "not_found" => StatusCode::NOT_FOUND,
                "permission_denied" => StatusCode::FORBIDDEN,
                "unavailable" => StatusCode::SERVICE_UNAVAILABLE,
                _ => StatusCode::BAD_REQUEST,
            };
            return json_response(status, json!({ "code": code, "message": "envd-secret" }));
        }
        return Response::builder()
            .header("content-type", "application/proto")
            .body(Body::from(bytes))
            .unwrap();
    }
    assert_eq!(record.headers["x-api-key"], "api-secret");
    assert!(!record.headers.contains_key("x-access-token"));
    let body: Value = if record.body.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&record.body).unwrap()
    };
    match (record.method.as_str(), record.path.as_str()) {
        ("GET", "/v2/sandboxes") => {
            let url = reqwest::Url::parse(&format!("http://fixture/?{}", record.query)).unwrap();
            let query = url
                .query_pairs()
                .collect::<std::collections::HashMap<_, _>>();
            assert_eq!(
                query.get("state").map(|value| value.as_ref()),
                Some("running,paused")
            );
            assert_eq!(query.get("limit").map(|value| value.as_ref()), Some("100"));
            if let Some(token) = query.get("nextToken") {
                assert_eq!(token, "page/2+token");
                let mut value = sandbox("paused");
                value["state"] = "paused".into();
                value.as_object_mut().unwrap().remove("metadata");
                json_response(StatusCode::OK, json!([value]))
            } else {
                let mut response = json_response(StatusCode::OK, json!([sandbox("parent")]));
                response
                    .headers_mut()
                    .insert("x-next-token", "page/2+token".parse().unwrap());
                response
            }
        }
        ("POST", "/v2/sandboxes") if body["templateID"] == "body-timeout" => {
            let chunks = stream::once(async {
                tokio::time::sleep(Duration::from_secs(1)).await;
                Ok::<_, Infallible>(Bytes::from(sandbox("parent").to_string()))
            });
            Response::builder()
                .status(StatusCode::CREATED)
                .header("content-type", "application/json")
                .body(Body::from_stream(chunks))
                .unwrap()
        }
        ("POST", "/v2/sandboxes") if body["templateID"] == "http-fail" => json_response(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({ "message": "api-secret" }),
        ),
        ("POST", "/v2/sandboxes") => json_response(StatusCode::CREATED, sandbox("parent")),
        ("GET", "/sandboxes/parent") => json_response(StatusCode::OK, sandbox("parent")),
        ("GET", "/sandboxes/paused") => {
            let mut value = sandbox("paused");
            value["state"] = "paused".into();
            json_response(StatusCode::OK, value)
        }
        ("GET", "/sandboxes/bad-metadata") => {
            let mut value = sandbox("bad-metadata");
            value["metadata"] = json!({"name": 12});
            json_response(StatusCode::OK, value)
        }
        ("POST", "/v2/sandboxes/parent/connect") => {
            json_response(StatusCode::CREATED, sandbox("parent"))
        }
        ("POST", "/sandboxes/parent/fork") => json_response(
            StatusCode::CREATED,
            json!([{ "sandbox": sandbox("child") }]),
        ),
        ("POST", "/sandboxes/fork-fail/fork") => json_response(
            StatusCode::CREATED,
            json!([{ "error": { "code": 503, "message": "api-secret" } }]),
        ),
        ("POST", "/sandboxes/parent/pause") | ("DELETE", "/sandboxes/parent") => {
            Response::builder()
                .status(StatusCode::NO_CONTENT)
                .body(Body::empty())
                .unwrap()
        }
        _ => json_response(StatusCode::NOT_FOUND, json!({ "message": "api-secret" })),
    }
}

#[tokio::test]
async fn e2b_body_deadline_is_timeout_not_malformed_json() {
    let fixture = Fixture::start().await;
    let mut config = E2bConfig::new(Secret::new("api-secret"));
    config.api_endpoint = fixture.endpoint.clone();
    config.request_timeout = Duration::from_millis(50);
    let error = E2bSandboxManager::new(config)
        .unwrap()
        .sandboxes()
        .create(opensol_adapters::e2b::E2bSandboxSpec {
            template: "body-timeout".into(),
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::Timeout);
    assert_eq!(error.operation, "create");
    assert_eq!(fixture.records.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn unified_e2b_entries_share_native_envd_without_caching_create_requests() {
    use opensol_adapters::e2b::E2bSandboxSpec;
    use opensol_core::{Backend, Connection, ProcessManager, SandboxManager};
    let fixture = Fixture::start().await;
    let mut config = E2bConfig::new(Secret::new("api-secret"));
    config.api_endpoint = fixture.endpoint.clone();
    config.envd_endpoint = Some(fixture.endpoint.clone());
    let backend = Backend::open(config.clone(), None).await.unwrap();
    let manager = SandboxManager::open(&backend).await.unwrap();
    assert!(
        fixture.records.lock().unwrap().is_empty(),
        "open does not probe or create"
    );
    let sandbox = manager
        .create(E2bSandboxSpec {
            template: "first".into(),
            sandbox_timeout: 17,
            auto_pause: true,
            env: [("ITEM".into(), "first".into())].into(),
            ..Default::default()
        })
        .await
        .unwrap();
    manager
        .create(E2bSandboxSpec {
            template: "second".into(),
            sandbox_timeout: 41,
            ..Default::default()
        })
        .await
        .unwrap();
    sandbox.start().await.unwrap();
    sandbox.fork().await.unwrap();
    let reference = sandbox.reference().clone();
    let before = fixture.records.lock().unwrap().len();
    let (connection, processes) = tokio::try_join!(
        Connection::open(&backend, Some(reference.clone())),
        ProcessManager::open(&backend, Some(reference.clone()), None),
    )
    .unwrap();
    assert_eq!(
        fixture.records.lock().unwrap().len(),
        before,
        "binding performs no discovery"
    );
    drop((backend, manager, sandbox));
    let (result, listed) = tokio::try_join!(
        connection.exec(ExecRequest::new(Command::shell("true"))),
        processes.list(),
    )
    .unwrap();
    assert!(!listed.is_empty());
    assert_eq!(result.exit.code, Some(7));
    let process = processes
        .spawn(SpawnRequest::new(Command::shell("true")))
        .await
        .unwrap();
    let subscription = process.output().subscribe().await.unwrap();
    assert_eq!(
        opensol_core::wait_events(subscription.events, "envd")
            .await
            .unwrap()
            .exit
            .unwrap()
            .code,
        Some(7)
    );
    {
        let records = fixture.records.lock().unwrap();
        assert_eq!(
            records
                .iter()
                .filter(|r| r.path == "/sandboxes/parent" && r.method == "GET")
                .count(),
            1,
            "both views reuse one per-target native client, including concurrent first I/O"
        );
        let creates: Vec<Value> = records
            .iter()
            .filter(|r| r.path == "/v2/sandboxes")
            .map(|r| serde_json::from_slice(&r.body).unwrap())
            .collect();
        assert_eq!(creates[0]["templateID"], "first");
        assert_eq!(creates[0]["timeout"], 17);
        assert_eq!(creates[0]["envVars"]["ITEM"], "first");
        assert_eq!(creates[1]["templateID"], "second");
        assert_eq!(creates[1]["timeout"], 41);
        assert_eq!(creates[1]["envVars"], json!({}));
        assert_eq!(creates[1]["autoPause"], false);
        for record in records
            .iter()
            .filter(|r| r.path.ends_with("/connect") || r.path.ends_with("/fork"))
        {
            let body: Value = serde_json::from_slice(&record.body).unwrap();
            assert_eq!(
                body["timeout"], 300,
                "operation timeout is independent of both create requests"
            );
        }
        assert!(
            records
                .iter()
                .filter(|r| r.path.starts_with("/process.Process/"))
                .all(|r| r.headers["e2b-sandbox-id"] == "parent"
                    && r.headers["e2b-sandbox-port"] == "49983")
        );
    }
    // Equal configuration values do not select a global native-client cache.
    Connection::open(config, Some(reference))
        .await
        .unwrap()
        .exec(ExecRequest::new(Command::shell("true")))
        .await
        .unwrap();
    assert_eq!(
        fixture
            .records
            .lock()
            .unwrap()
            .iter()
            .filter(|r| r.path == "/sandboxes/parent" && r.method == "GET")
            .count(),
        2
    );
}

#[tokio::test]
async fn unified_e2b_binding_is_local_and_first_io_does_not_resume_paused_targets() {
    use opensol_core::{Backend, Connection, ProcessManager};
    let fixture = Fixture::start().await;
    let mut config = E2bConfig::new(Secret::new("api-secret"));
    config.api_endpoint = fixture.endpoint.clone();
    config.envd_endpoint = Some(fixture.endpoint.clone());
    let backend = Backend::open(config, None).await.unwrap();
    let reference = SandboxRef::new("e2b", "paused");
    let connection = Connection::open(&backend, Some(reference.clone()))
        .await
        .unwrap();
    let processes = ProcessManager::open(&backend, Some(reference), None)
        .await
        .unwrap();
    assert!(fixture.records.lock().unwrap().is_empty());
    assert!(connection.capabilities().stdin.is_some());
    processes.list().await.unwrap();
    let records = fixture.records.lock().unwrap();
    assert_eq!(records.len(), 2);
    assert_eq!(
        (&*records[0].method, &*records[0].path),
        ("GET", "/sandboxes/paused")
    );
    assert_eq!(records[1].path, "/process.Process/List");
}

#[tokio::test]
async fn envd_resources_use_native_list_and_kill_without_simulating_missing_operations() {
    let fixture = Fixture::start().await;
    let envd = fixture.envd().processes();
    let info = envd.probe().await.unwrap();
    assert_eq!(info.backend, "envd");
    assert!(info.client_version.is_none() && info.server_version.is_none());
    let listed = envd.list().await.unwrap();
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0].process.id, "42");
    assert_eq!(listed[0].process.pty, None);
    assert!(listed[0].outcome.is_none());
    let process = Process::new(envd.clone(), listed[0].process.clone());
    assert_eq!(process.inspect().await.unwrap().process.id, "42");
    let handle = process.output().subscribe().await.unwrap();
    assert_eq!(process.reference().pty, None);
    assert_eq!(
        opensol_core::wait_events(handle.events, "envd")
            .await
            .unwrap()
            .exit
            .unwrap()
            .code,
        Some(7)
    );
    let outcome = process.wait(None).await.unwrap();
    assert_eq!(outcome.exit.unwrap().code, Some(7));
    assert!(outcome.native_result.is_none());
    process.kill().await.unwrap();
    for error in [
        process.pause().await.unwrap_err(),
        process.start().await.unwrap_err(),
        process.restart().await.unwrap_err(),
        process.remove().await.unwrap_err(),
        process.output().tail(64).await.unwrap_err(),
        process
            .output()
            .read(None, opensol_core::ReadDirection::Forward, 64)
            .await
            .unwrap_err(),
    ] {
        assert_eq!(error.kind, ErrorKind::Unsupported);
    }
    let missing = ProcessRef::new("envd", "999", Some(false));
    assert_eq!(
        envd.get(missing.clone()).inspect().await.unwrap_err().kind,
        ErrorKind::NotFound
    );
    let records = fixture.records.lock().unwrap();
    assert!(records.iter().all(|record| record.path.ends_with("/List")
        || record.path.ends_with("/SendSignal")
        || record.path.ends_with("/Connect")));
    let killed = records
        .iter()
        .find(|record| record.path.ends_with("/SendSignal"))
        .unwrap();
    let request = protocol::SendSignalRequest::decode(killed.body.as_slice()).unwrap();
    assert_eq!(request.signal, 9);
    assert_selector(request.process);
}

#[tokio::test]
async fn cancelled_first_e2b_discovery_can_be_retried_without_remote_side_effects() {
    use opensol_core::{Backend, ProcessManager};
    use std::sync::atomic::{AtomicUsize, Ordering};

    let attempts = Arc::new(AtomicUsize::new(0));
    let started = Arc::new(Notify::new());
    let release = Arc::new(Notify::new());
    let router = Router::new()
        .route(
            "/sandboxes/parent",
            axum::routing::get({
                let attempts = attempts.clone();
                let started = started.clone();
                let release = release.clone();
                move || {
                    let attempts = attempts.clone();
                    let started = started.clone();
                    let release = release.clone();
                    async move {
                        if attempts.fetch_add(1, Ordering::SeqCst) == 0 {
                            started.notify_one();
                            release.notified().await;
                        }
                        json_response(StatusCode::OK, sandbox("parent"))
                    }
                }
            }),
        )
        .route(
            "/process.Process/List",
            axum::routing::post(|| async {
                Response::builder()
                    .header("content-type", "application/proto")
                    .body(Body::empty())
                    .unwrap()
            }),
        );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    let mut config = E2bConfig::new(Secret::new("api-secret"));
    config.api_endpoint = endpoint.clone();
    config.envd_endpoint = Some(endpoint);
    let backend = Backend::open(config, None).await.unwrap();
    let manager = ProcessManager::open(&backend, Some(SandboxRef::new("e2b", "parent")), None)
        .await
        .unwrap();
    let first = tokio::spawn({
        let manager = manager.clone();
        async move { manager.probe().await }
    });
    tokio::time::timeout(Duration::from_secs(2), started.notified())
        .await
        .unwrap();
    first.abort();
    assert!(first.await.unwrap_err().is_cancelled());
    release.notify_one();
    tokio::time::timeout(Duration::from_secs(2), manager.probe())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(attempts.load(Ordering::SeqCst), 2);
    server.abort();
}

#[tokio::test]
async fn envd_probe_preserves_native_permission_errors_without_exposing_bodies() {
    let fixture = Fixture::with_input_error(Some(("List", "permission_denied"))).await;
    let error = fixture.envd().processes().probe().await.unwrap_err();
    assert_eq!(error.kind, ErrorKind::Permission);
    assert!(!error.to_string().contains("envd-secret"));
}

#[tokio::test]
async fn envd_native_framing_bytes_and_finite_stdin() {
    let fixture = Fixture::start().await;
    let envd = fixture.envd();
    let mut command = Command::new("program", ["' $()", ""]);
    command.cwd = Some("/work".into());
    command.env.insert("KEY".into(), "value".into());
    let result = envd
        .connection()
        .exec(ExecRequest {
            command,
            stdin: vec![0, 0xff, 7],
            timeout: Some(Duration::from_secs(2)),
        })
        .await
        .unwrap();
    assert_eq!(result.stdout, [0xff, 0, b'O']);
    assert_eq!(result.stderr, b"error");
    assert_eq!(result.exit.code, Some(7));
    let records = fixture.records.lock().unwrap();
    let start = records
        .iter()
        .find(|record| record.path.ends_with("/Start"))
        .unwrap();
    let message = protocol::StartRequest::decode(&start.body[5..]).unwrap();
    let config = message.process.unwrap();
    assert_eq!(config.cmd, "program");
    assert_eq!(config.args, ["' $()", ""]);
    assert_eq!(config.cwd.as_deref(), Some("/work"));
    assert_eq!(config.envs["KEY"], "value");
    assert_eq!(message.stdin, Some(true));
    assert_eq!(start.headers["authorization"], "Basic dXNlcjo=");
    let input = records
        .iter()
        .find(|record| record.path.ends_with("/SendInput"))
        .unwrap();
    let input = protocol::SendInputRequest::decode(input.body.as_slice()).unwrap();
    assert_selector(input.process);
    assert_eq!(
        input.input.unwrap().input,
        Some(protocol::process_input::Input::Stdin(vec![0, 0xff, 7]))
    );
    let close = records
        .iter()
        .find(|record| record.path.ends_with("/CloseStdin"))
        .unwrap();
    let close = protocol::CloseStdinRequest::decode(close.body.as_slice()).unwrap();
    assert_selector(close.process);
}

#[tokio::test]
async fn envd_clones_hand_off_start_once_without_prefetch_or_replacing_it_with_connect() {
    let fixture = Fixture::start().await;
    let manager = fixture.envd().processes();
    let process = manager
        .spawn(SpawnRequest::new(Command::shell("true")))
        .await
        .unwrap();
    let first = process.output();
    let second = process.clone().output();
    let control = process.capabilities();
    assert_eq!(
        first.capabilities().subscribe.initial.unwrap().channels,
        [OutputChannel::Stdout, OutputChannel::Stderr]
    );
    assert!(
        manager
            .get(process.reference().clone())
            .output()
            .capabilities()
            .subscribe
            .initial
            .is_none()
    );
    assert_eq!(fixture.records.lock().unwrap().len(), 1);
    let (a, b) = futures::join!(first.subscribe(), second.subscribe());
    let mut a = a.unwrap();
    let mut b = b.unwrap();
    assert_eq!(
        a.capabilities().channels,
        [OutputChannel::Stdout, OutputChannel::Stderr]
    );
    assert_eq!(
        b.capabilities().channels,
        [
            OutputChannel::Stdout,
            OutputChannel::Stderr,
            OutputChannel::Terminal
        ]
    );
    assert!(first.capabilities().subscribe.initial.is_none());
    assert!(second.capabilities().subscribe.initial.is_none());
    assert_eq!(process.capabilities(), control);
    assert!(matches!(
        a.events.next().await.unwrap().unwrap(),
        ProcessEvent::Output { .. }
    ));
    assert!(matches!(
        b.events.next().await.unwrap().unwrap(),
        ProcessEvent::Output { .. }
    ));
    assert_eq!(
        opensol_core::wait_events(a.events, "envd")
            .await
            .unwrap()
            .exit
            .unwrap()
            .code,
        Some(7)
    );
    assert_eq!(
        opensol_core::wait_events(b.events, "envd")
            .await
            .unwrap()
            .exit
            .unwrap()
            .code,
        Some(7)
    );
    let records = fixture.records.lock().unwrap();
    assert_eq!(records.len(), 2);
    assert_eq!(
        records
            .iter()
            .filter(|record| record.path.ends_with("/Start"))
            .count(),
        1
    );
    assert_eq!(
        records
            .iter()
            .filter(|record| record.path.ends_with("/Connect"))
            .count(),
        1
    );
}

#[tokio::test]
async fn envd_start_rejects_channels_outside_the_actual_spawn_mode() {
    let fixture = Fixture::start().await;
    for pty in [None, Some(PtySize { cols: 80, rows: 24 })] {
        let process = fixture
            .envd()
            .processes()
            .spawn(SpawnRequest {
                command: Command::new("wrong-channel", Vec::<String>::new()),
                stdin: false,
                pty,
            })
            .await
            .unwrap();
        let mut subscription = process.output().subscribe().await.unwrap();
        assert_eq!(
            subscription.events.next().await.unwrap().unwrap_err().kind,
            ErrorKind::Backend
        );
    }
}

#[tokio::test]
async fn envd_pty_control_reconnect_and_capabilities_are_independent() {
    let fixture = Fixture::start().await;
    let envd = fixture.envd().processes();
    let mut request = SpawnRequest::new(Command::new("program", Vec::<String>::new()));
    request.stdin = true;
    request.pty = Some(PtySize { cols: 90, rows: 30 });
    let spawned_handle = envd.spawn(request).await.unwrap();
    let mut handle = spawned_handle.output().subscribe().await.unwrap();
    assert_eq!(spawned_handle.reference().pty, Some(true));
    let listed = envd.list().await.unwrap().remove(0).process;
    assert_eq!(listed.id, spawned_handle.reference().id);
    assert_eq!(listed.pty, None);
    let unknown = envd.get(listed);
    let before = fixture.records.lock().unwrap().len();
    assert!(envd.capabilities().process.input.unwrap().close);
    let unknown_caps = unknown.capabilities();
    assert!(unknown_caps.input.is_none() && !unknown_caps.resize);
    assert!(unknown_caps.kill.is_some() && unknown_caps.reconnect);
    let terminal_caps = spawned_handle.capabilities();
    assert!(terminal_caps.resize);
    assert!(!terminal_caps.input.unwrap().close);
    assert_eq!(
        envd.get(spawned_handle.reference().clone()).capabilities(),
        terminal_caps
    );
    let plain = envd.get(ProcessRef::new("envd", "42", Some(false)));
    let plain_caps = plain.capabilities();
    assert!(plain_caps.input.unwrap().close);
    assert!(!plain_caps.resize);
    for error in [
        unknown.send_input(vec![1]).await.unwrap_err(),
        unknown.close_stdin().await.unwrap_err(),
        unknown
            .resize(PtySize { cols: 80, rows: 24 })
            .await
            .unwrap_err(),
        plain
            .resize(PtySize { cols: 80, rows: 24 })
            .await
            .unwrap_err(),
    ] {
        assert_eq!(error.kind, ErrorKind::Unsupported);
    }
    assert_eq!(fixture.records.lock().unwrap().len(), before);
    assert!(
        matches!(handle.events.next().await.unwrap().unwrap(), ProcessEvent::Output { channel: OutputChannel::Terminal, data } if data == [0xff, 0, b'T'])
    );
    envd.get(spawned_handle.reference().clone())
        .send_input(vec![0xff, 4])
        .await
        .unwrap();
    envd.get(spawned_handle.reference().clone())
        .resize(PtySize {
            cols: 100,
            rows: 40,
        })
        .await
        .unwrap();
    envd.get(spawned_handle.reference().clone())
        .signal(Signal::Kill)
        .await
        .unwrap();
    envd.get(spawned_handle.reference().clone())
        .signal(Signal::Terminate)
        .await
        .unwrap();
    assert_eq!(
        envd.get(spawned_handle.reference().clone())
            .close_stdin()
            .await
            .unwrap_err()
            .kind,
        ErrorKind::Unsupported
    );
    assert_eq!(
        envd.get(spawned_handle.reference().clone())
            .signal(Signal::Interrupt)
            .await
            .unwrap_err()
            .kind,
        ErrorKind::Unsupported
    );
    assert!(
        !unknown
            .output()
            .capabilities()
            .subscribe
            .reconnect
            .unwrap()
            .historical_output
    );
    assert!(envd.capabilities().process.reconnect);
    let process = ProcessRef::new("envd", "42", Some(false));
    assert_eq!(
        envd.get(process.clone()).inspect().await.unwrap().state,
        opensol_core::ProcessState::Running
    );
    assert_eq!(
        envd.get(process.clone())
            .wait(Some(Duration::from_secs(2)))
            .await
            .unwrap()
            .exit
            .unwrap()
            .code,
        Some(7)
    );
    let records = fixture.records.lock().unwrap();
    let start = records
        .iter()
        .find(|record| record.path.ends_with("/Start"))
        .unwrap();
    let start = protocol::StartRequest::decode(&start.body[5..]).unwrap();
    let size = start.pty.unwrap().size.unwrap();
    assert_eq!((size.cols, size.rows), (90, 30));
    let input = protocol::SendInputRequest::decode(
        records
            .iter()
            .find(|record| record.path.ends_with("/SendInput"))
            .unwrap()
            .body
            .as_slice(),
    )
    .unwrap();
    assert_selector(input.process);
    assert_eq!(
        input.input.unwrap().input,
        Some(protocol::process_input::Input::Pty(vec![0xff, 4]))
    );
    let update = protocol::UpdateRequest::decode(
        records
            .iter()
            .find(|record| record.path.ends_with("/Update"))
            .unwrap()
            .body
            .as_slice(),
    )
    .unwrap();
    assert_selector(update.process);
    let size = update.pty.unwrap().size.unwrap();
    assert_eq!((size.cols, size.rows), (100, 40));
    let mut signals = records
        .iter()
        .filter(|record| record.path.ends_with("/SendSignal"))
        .map(|record| {
            let request = protocol::SendSignalRequest::decode(record.body.as_slice()).unwrap();
            assert_selector(request.process);
            request.signal
        })
        .collect::<Vec<_>>();
    signals.sort_unstable();
    assert_eq!(signals, [9, 15]);
}

#[tokio::test]
async fn envd_exec_preserves_native_exit_when_input_target_has_expired() {
    for method in ["SendInput", "CloseStdin"] {
        let fixture = Fixture::with_input_error(Some((method, "not_found"))).await;
        let result = fixture
            .envd()
            .connection()
            .exec(ExecRequest {
                command: Command::new("program", Vec::<String>::new()),
                stdin: vec![0, 0xff, 7],
                timeout: Some(Duration::from_secs(2)),
            })
            .await
            .unwrap();
        assert_eq!(result.stdout, [0xff, 0, b'O'], "method={method}");
        assert_eq!(result.stderr, b"error", "method={method}");
        assert_eq!(result.exit.code, Some(7), "method={method}");
    }
}

#[tokio::test]
async fn envd_input_not_found_requires_valid_bounded_completion() {
    for (program, kind) in [
        ("no-exit", ErrorKind::Transport),
        ("invalid-exit", ErrorKind::Backend),
    ] {
        let fixture = Fixture::with_input_error(Some(("SendInput", "not_found"))).await;
        let error = fixture
            .envd()
            .connection()
            .exec(ExecRequest {
                command: Command::new(program, Vec::<String>::new()),
                stdin: vec![0, 0xff],
                timeout: Some(Duration::from_secs(2)),
            })
            .await
            .unwrap_err();
        assert_eq!(error.kind, kind, "program={program}");
    }
    let fixture = Fixture::with_input_error(Some(("SendInput", "not_found"))).await;
    let envd = fixture
        .envd_with_timeout(Duration::from_millis(300))
        .connection();
    let error = tokio::time::timeout(
        Duration::from_secs(2),
        envd.exec(ExecRequest {
            command: Command::new("never", Vec::<String>::new()),
            stdin: vec![0, 0xff],
            timeout: None,
        }),
    )
    .await
    .expect("input reconciliation must not wait forever without a native Exit")
    .unwrap_err();
    assert_eq!(error.kind, ErrorKind::NotFound);
}

#[tokio::test]
async fn envd_exec_does_not_hide_other_unary_input_failures_behind_exit() {
    for (code, kind) in [
        ("permission_denied", ErrorKind::Permission),
        ("unavailable", ErrorKind::Transport),
    ] {
        let fixture = Fixture::with_input_error(Some(("SendInput", code))).await;
        let error = fixture
            .envd()
            .connection()
            .exec(ExecRequest {
                command: Command::new("program", Vec::<String>::new()),
                stdin: vec![0, 0xff],
                timeout: Some(Duration::from_secs(2)),
            })
            .await
            .unwrap_err();
        assert_eq!(error.kind, kind);
        assert_eq!(error.operation, "SendInput");
        assert!(!error.to_string().contains("envd-secret"));
    }
}

#[tokio::test]
async fn envd_errors_do_not_become_success_or_implicit_kills() {
    let fixture = Fixture::start().await;
    let envd = fixture.envd();
    for (program, kind) in [
        ("no-exit", ErrorKind::Transport),
        ("rpc-error", ErrorKind::Permission),
        ("bad-first", ErrorKind::Backend),
        ("large-frame", ErrorKind::Backend),
        ("http-permission", ErrorKind::Permission),
        ("never", ErrorKind::Timeout),
    ] {
        let error = envd
            .connection()
            .exec(ExecRequest {
                command: Command::new(program, Vec::<String>::new()),
                stdin: Vec::new(),
                timeout: Some(Duration::from_millis(120)),
            })
            .await
            .unwrap_err();
        assert_eq!(error.kind, kind, "program={program}");
        assert!(!error.to_string().contains("envd-secret"));
    }
    let records = fixture.records.lock().unwrap();
    assert_eq!(
        records
            .iter()
            .filter(|record| record.path.ends_with("/Start"))
            .count(),
        6
    );
    assert!(
        !records
            .iter()
            .any(|record| record.path.ends_with("/SendSignal"))
    );
}

#[tokio::test]
async fn e2b_list_collects_native_pages_and_maps_running_and_paused_states() {
    let fixture = Fixture::start().await;
    let mut config = E2bConfig::new(Secret::new("api-secret"));
    config.api_endpoint = fixture.endpoint.clone();
    let manager = E2bSandboxManager::new(config).unwrap().sandboxes();
    assert!(manager.capabilities().list);
    let listed = manager.list().await.unwrap();
    assert_eq!(listed.len(), 2);
    assert_eq!(listed[0].name.as_deref(), Some("native-name"));
    assert_eq!(
        listed[0].labels.get("name").map(String::as_str),
        Some("native-name")
    );
    assert_eq!(
        listed[0].labels.get("project").map(String::as_str),
        Some("native-project")
    );
    assert!(listed[1].name.is_none() && listed[1].labels.is_empty());
    assert_eq!(
        fixture.records.lock().unwrap().len(),
        2,
        "list must use its own metadata without per-item inspect"
    );
    assert_eq!(
        (&*listed[0].sandbox.id, listed[0].state),
        ("parent", SandboxState::Running)
    );
    assert_eq!(
        (&*listed[1].sandbox.id, listed[1].state),
        ("paused", SandboxState::Stopped)
    );
    assert_eq!(
        manager
            .get(listed[0].sandbox.clone())
            .inspect()
            .await
            .unwrap(),
        listed[0]
    );
    let records = fixture.records.lock().unwrap();
    assert_eq!(
        records
            .iter()
            .filter(|record| record.path == "/v2/sandboxes")
            .count(),
        2
    );
    assert!(records.iter().all(|record| record.method == "GET"));
}

#[tokio::test]
async fn e2b_rejects_non_string_native_metadata() {
    let fixture = Fixture::start().await;
    let mut config = E2bConfig::new(Secret::new("api-secret"));
    config.api_endpoint = fixture.endpoint.clone();
    let manager = E2bSandboxManager::new(config).unwrap().sandboxes();
    assert_eq!(
        manager
            .get(SandboxRef::new("e2b", "bad-metadata"))
            .inspect()
            .await
            .unwrap_err()
            .kind,
        ErrorKind::Backend
    );
}

#[tokio::test]
async fn e2b_v2_lifecycle_native_fork_and_envd_composition() {
    let fixture = Fixture::start().await;
    let mut config = E2bConfig::new(Secret::new("api-secret"));
    config.api_endpoint = fixture.endpoint.clone();
    config.envd_endpoint = Some(fixture.endpoint.clone());
    let metadata = [
        ("name".into(), "config-name".into()),
        ("project".into(), "config-project".into()),
    ]
    .into();
    assert!(!format!("{config:?}").contains("api-secret"));
    let backend = E2bSandboxManager::new(config.clone()).unwrap();
    let lifecycle = backend.sandboxes();
    let resource = lifecycle
        .create(opensol_adapters::e2b::E2bSandboxSpec {
            metadata,
            options: CreateOptions {
                name: Some("friendly".into()),
                labels: [
                    ("name".into(), "label-name".into()),
                    ("project".into(), "label-project".into()),
                ]
                .into(),
            },
            ..Default::default()
        })
        .await
        .unwrap();
    assert_eq!(resource.reference().id, "parent");
    let inspected = resource.inspect().await.unwrap();
    assert_eq!(inspected.name.as_deref(), Some("native-name"));
    assert_eq!(
        inspected.labels["project"], "native-project",
        "read native metadata, never cached create options"
    );
    assert_eq!(
        resource.inspect().await.unwrap().state,
        SandboxState::Running
    );
    resource.stop().await.unwrap();
    resource.start().await.unwrap();
    assert_eq!(resource.fork().await.unwrap().reference().id, "child");
    let native = backend.envd(resource.reference()).await.unwrap();
    assert_eq!(
        native
            .connection()
            .exec(ExecRequest::new(Command::shell("true")))
            .await
            .unwrap()
            .exit
            .code,
        Some(7)
    );
    resource.delete().await.unwrap();
    let error = lifecycle
        .get(SandboxRef::new("e2b", "fork-fail"))
        .fork()
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::Backend);
    assert!(!format!("{error:?}").contains("api-secret"));
    let error = E2bSandboxManager::new(config)
        .unwrap()
        .sandboxes()
        .create(opensol_adapters::e2b::E2bSandboxSpec {
            template: "http-fail".into(),
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::Backend);
    assert!(!error.to_string().contains("api-secret"));
    let records = fixture.records.lock().unwrap();
    let creates = records
        .iter()
        .filter(|record| record.path == "/v2/sandboxes")
        .collect::<Vec<_>>();
    assert_eq!(creates.len(), 2);
    let create: Value = serde_json::from_slice(&creates[0].body).unwrap();
    assert_eq!(create["metadata"]["name"], "friendly");
    assert_eq!(create["metadata"]["project"], "label-project");
    assert_eq!(create["autoPauseMemory"], true);
    let fork = records
        .iter()
        .find(|record| record.path == "/sandboxes/parent/fork")
        .unwrap();
    let body: Value = serde_json::from_slice(&fork.body).unwrap();
    assert_eq!(body["count"], 1);
    let sandbox_request = records
        .iter()
        .find(|record| record.path.ends_with("/Start"))
        .unwrap();
    assert_eq!(sandbox_request.headers["e2b-sandbox-id"], "parent");
    assert_eq!(sandbox_request.headers["e2b-sandbox-port"], "49983");
}
