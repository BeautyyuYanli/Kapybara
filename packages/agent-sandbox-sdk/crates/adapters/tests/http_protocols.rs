//! Schema-faithful HTTP/ConnectRPC fixtures, not live E2B/envd end-to-end tests.

#![cfg(all(feature = "e2b", feature = "envd"))]

use std::{
    convert::Infallible,
    sync::{Arc, Mutex},
    time::Duration,
};

use agent_sandbox_adapters::{
    e2b::{E2bConfig, E2bLifecycle},
    envd::{Envd, EnvdConfig, protocol},
    secret::Secret,
};
use agent_sandbox_core::{
    Command, Connection, CreateOptions, ErrorKind, ExecRequest, Lifecycle, OutputChannel,
    ProcessEvent, ProcessManager, ProcessRef, PtySize, ResourceRef, ResourceState, Signal,
    SpawnRequest,
};
use axum::{
    Router,
    body::{Body, to_bytes},
    http::{HeaderMap, Request, Response, StatusCode},
};
use bytes::Bytes;
use futures::{StreamExt, stream};
use prost::Message;
use serde_json::{Value, json};
use tokio::sync::Notify;

#[derive(Clone)]
struct Recorded {
    method: String,
    path: String,
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
    json!({ "sandboxID": id, "templateID": "base", "clientID": "client", "envdVersion": "1.0.0", "envdAccessToken": "envd-secret", "domain": null, "state": "running" })
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
    let data = if pty {
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
    let mut config = E2bConfig::new(Secret::new("api-secret"), "body-timeout");
    config.api_endpoint = fixture.endpoint.clone();
    config.request_timeout = Duration::from_millis(50);
    let error = E2bLifecycle::new(config)
        .unwrap()
        .create(CreateOptions::default())
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::Timeout);
    assert_eq!(error.operation, "create");
    assert_eq!(fixture.records.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn envd_native_framing_bytes_and_finite_stdin() {
    let fixture = Fixture::start().await;
    let envd = fixture.envd();
    let mut command = Command::new("program", ["' $()", ""]);
    command.cwd = Some("/work".into());
    command.env.insert("KEY".into(), "value".into());
    let result = envd
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
async fn envd_pty_control_reconnect_and_capabilities_are_independent() {
    let fixture = Fixture::start().await;
    let envd = fixture.envd();
    let mut request = SpawnRequest::new(Command::new("program", Vec::<String>::new()));
    request.stdin = true;
    request.pty = Some(PtySize { cols: 90, rows: 30 });
    let mut handle = envd.spawn(request).await.unwrap();
    assert!(handle.process.pty);
    assert!(
        matches!(handle.events.next().await.unwrap().unwrap(), ProcessEvent::Output { channel: OutputChannel::Terminal, data } if data == [0xff, 0, b'T'])
    );
    envd.send_input(&handle.process, vec![0xff, 4])
        .await
        .unwrap();
    envd.resize(
        &handle.process,
        PtySize {
            cols: 100,
            rows: 40,
        },
    )
    .await
    .unwrap();
    envd.signal(&handle.process, Signal::Kill).await.unwrap();
    envd.signal(&handle.process, Signal::Terminate)
        .await
        .unwrap();
    assert_eq!(
        envd.close_stdin(&handle.process).await.unwrap_err().kind,
        ErrorKind::Unsupported
    );
    assert_eq!(
        envd.signal(&handle.process, Signal::Interrupt)
            .await
            .unwrap_err()
            .kind,
        ErrorKind::Unsupported
    );
    assert!(!ProcessManager::capabilities(&envd).historical_output);
    assert!(ProcessManager::capabilities(&envd).reconnect);
    let process = ProcessRef::new("envd", "42", false);
    assert_eq!(
        envd.status(&process).await.unwrap().state,
        agent_sandbox_core::ProcessState::Running
    );
    assert_eq!(
        envd.wait(&process, Some(Duration::from_secs(2)))
            .await
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
    let envd = fixture.envd_with_timeout(Duration::from_millis(300));
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
async fn e2b_v2_lifecycle_native_fork_and_envd_composition() {
    let fixture = Fixture::start().await;
    let mut config = E2bConfig::new(Secret::new("api-secret"), "base");
    config.api_endpoint = fixture.endpoint.clone();
    config.envd_endpoint = Some(fixture.endpoint.clone());
    assert!(!format!("{config:?}").contains("api-secret"));
    let lifecycle = E2bLifecycle::new(config.clone()).unwrap();
    let resource = lifecycle
        .create(CreateOptions {
            name: Some("friendly".into()),
            ..Default::default()
        })
        .await
        .unwrap();
    assert_eq!(resource.id, "parent");
    assert_eq!(
        lifecycle.inspect(&resource).await.unwrap().state,
        ResourceState::Running
    );
    lifecycle.stop(&resource).await.unwrap();
    lifecycle.start(&resource).await.unwrap();
    assert_eq!(lifecycle.fork(&resource).await.unwrap().id, "child");
    let native = lifecycle.envd(&resource).await.unwrap();
    assert_eq!(
        native
            .exec(ExecRequest::new(Command::shell("true")))
            .await
            .unwrap()
            .exit
            .code,
        Some(7)
    );
    lifecycle.delete(&resource).await.unwrap();
    let error = lifecycle
        .fork(&ResourceRef::new("e2b", "fork-fail"))
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::Backend);
    assert!(!format!("{error:?}").contains("api-secret"));
    config.template = "http-fail".into();
    let error = E2bLifecycle::new(config)
        .unwrap()
        .create(CreateOptions::default())
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
