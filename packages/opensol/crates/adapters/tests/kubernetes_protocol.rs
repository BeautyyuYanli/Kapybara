//! Real kubectl against a schema-faithful discovery/CR HTTP fixture, not a live cluster/controller.

#![cfg(feature = "agent-sandbox")]

use std::sync::{Arc, Mutex};

use axum::{
    Router,
    body::{Body, to_bytes},
    http::{Request, Response, StatusCode},
};
use opensol_adapters::agent_sandbox::{AgentSandboxConfig, AgentSandboxLifecycle};
use opensol_core::{CreateOptions, ErrorKind, Lifecycle, ResourceState};
use serde_json::{Value, json};
use tempfile::TempDir;

#[derive(Default)]
struct State {
    resource: Option<Value>,
    mutations: Vec<(String, Value)>,
    create_attempts: Vec<Value>,
    retry_after: bool,
    deny: bool,
}

struct Fixture {
    endpoint: String,
    state: Arc<Mutex<State>>,
    task: tokio::task::JoinHandle<()>,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        self.task.abort();
    }
}

fn response(status: StatusCode, value: Value) -> Response<Body> {
    Response::builder()
        .status(status)
        .header("content-type", "application/json")
        .body(Body::from(value.to_string()))
        .unwrap()
}

fn resource_list() -> Value {
    json!({ "kind": "APIResourceList", "apiVersion": "v1", "groupVersion": "agents.x-k8s.io/v1beta1", "resources": [{ "name": "sandboxes", "singularName": "sandbox", "namespaced": true, "kind": "Sandbox", "verbs": ["get", "create", "patch", "delete"] }] })
}

impl Fixture {
    async fn start() -> Self {
        let state = Arc::new(Mutex::new(State::default()));
        let stored = state.clone();
        let router = Router::new().fallback(move |request: Request<Body>| {
            let stored = stored.clone();
            async move {
                let (parts, body) = request.into_parts(); let path = parts.uri.path();
                let body = to_bytes(body, 1024 * 1024).await.unwrap();
                let input: Value = if body.is_empty() { Value::Null } else { serde_json::from_slice(&body).unwrap() };
                let group = json!({ "name": "agents.x-k8s.io", "versions": [{ "groupVersion": "agents.x-k8s.io/v1beta1", "version": "v1beta1" }], "preferredVersion": { "groupVersion": "agents.x-k8s.io/v1beta1", "version": "v1beta1" } });
                if parts.method == "GET" {
                    match path {
                        "/api" => return response(StatusCode::OK, json!({ "kind": "APIVersions", "apiVersion": "v1", "versions": ["v1"], "serverAddressByClientCIDRs": [] })),
                        "/api/v1" => return response(StatusCode::OK, json!({ "kind": "APIResourceList", "apiVersion": "v1", "groupVersion": "v1", "resources": [] })),
                        "/apis" => return response(StatusCode::OK, json!({ "kind": "APIGroupList", "apiVersion": "v1", "groups": [group] })),
                        "/apis/agents.x-k8s.io" => return response(StatusCode::OK, group),
                        "/apis/agents.x-k8s.io/v1beta1" => return response(StatusCode::OK, resource_list()),
                        "/openapi/v3" => return response(StatusCode::OK, json!({ "paths": { "apis/agents.x-k8s.io/v1beta1": { "serverRelativeURL": "/openapi/v3/apis/agents.x-k8s.io/v1beta1?hash=fixture" } } })),
                        "/openapi/v3/apis/agents.x-k8s.io/v1beta1" => return response(StatusCode::OK, json!({
                            "openapi": "3.0.0", "info": { "title": "fixture", "version": "1.0" },
                            "paths": { "/apis/agents.x-k8s.io/v1beta1/namespaces/{namespace}/sandboxes/{name}": {
                                "patch": { "x-kubernetes-group-version-kind": { "group": "agents.x-k8s.io", "version": "v1beta1", "kind": "Sandbox" },
                                    "parameters": [{ "name": "fieldValidation", "in": "query", "schema": { "type": "string" } }],
                                    "responses": { "200": { "description": "OK" } } }
                            } }
                        })),
                        _ => {},
                    }
                }
                let mut state = stored.lock().unwrap();
                if state.deny { return response(StatusCode::FORBIDDEN, json!({ "kind": "Status", "apiVersion": "v1", "status": "Failure", "reason": "Forbidden", "message": "kube-secret", "code": 403 })); }
                match (parts.method.as_str(), path) {
                    ("POST", "/apis/agents.x-k8s.io/v1beta1/namespaces/work/sandboxes") => {
                        state.create_attempts.push(input.clone());
                        if state.resource.is_some() {
                            return response(StatusCode::CONFLICT, json!({ "kind": "Status", "apiVersion": "v1", "status": "Failure", "reason": "AlreadyExists", "message": "already exists", "code": 409 }));
                        }
                        let mut resource = input.clone(); resource["metadata"]["generation"] = 1.into(); resource["metadata"]["resourceVersion"] = "1".into();
                        resource["status"] = json!({ "conditions": [{ "type": "Ready", "status": "True", "reason": "DependenciesReady", "observedGeneration": 1 }] });
                        state.resource = Some(resource.clone()); state.mutations.push(("create".into(), input));
                        if state.retry_after {
                            state.retry_after = false;
                            let mut result = response(StatusCode::SERVICE_UNAVAILABLE, json!({ "kind": "Status", "apiVersion": "v1", "status": "Failure", "reason": "ServiceUnavailable", "message": "retry later", "code": 503 }));
                            result.headers_mut().insert("retry-after", "1".parse().unwrap());
                            result
                        } else {
                            response(StatusCode::CREATED, resource)
                        }
                    }
                    ("GET", "/apis/agents.x-k8s.io/v1beta1/namespaces/work/sandboxes/demo") => {
                        if let Some(resource) = &state.resource { response(StatusCode::OK, resource.clone()) }
                        else { response(StatusCode::NOT_FOUND, json!({ "kind": "Status", "apiVersion": "v1", "status": "Failure", "reason": "NotFound", "message": "not found", "code": 404 })) }
                    }
                    ("PATCH", "/apis/agents.x-k8s.io/v1beta1/namespaces/work/sandboxes/demo") => {
                        assert_eq!(parts.headers["content-type"], "application/merge-patch+json");
                        state.mutations.push(("patch".into(), input.clone()));
                        let resource = state.resource.as_mut().unwrap(); resource["spec"]["operatingMode"] = input["spec"]["operatingMode"].clone();
                        resource["metadata"]["generation"] = (resource["metadata"]["generation"].as_u64().unwrap() + 1).into();
                        response(StatusCode::OK, resource.clone())
                    }
                    ("DELETE", "/apis/agents.x-k8s.io/v1beta1/namespaces/work/sandboxes/demo") => {
                        state.mutations.push(("delete".into(), input)); state.resource = None;
                        response(StatusCode::OK, json!({ "kind": "Status", "apiVersion": "v1", "status": "Success", "details": { "name": "demo", "kind": "sandboxes" } }))
                    }
                    _ => response(StatusCode::NOT_FOUND, json!({ "kind": "Status", "apiVersion": "v1", "status": "Failure", "reason": "NotFound", "message": "not found", "code": 404 })),
                }
            }
        });
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = format!("http://{}", listener.local_addr().unwrap());
        let task = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        Self {
            endpoint,
            state,
            task,
        }
    }

    fn observed(&self, condition: &str, reason: &str) {
        let mut state = self.state.lock().unwrap();
        let resource = state.resource.as_mut().unwrap();
        let generation = resource["metadata"]["generation"].as_u64().unwrap();
        resource["status"]["conditions"] = json!([{ "type": condition, "status": "True", "reason": reason, "observedGeneration": generation }]);
    }

    fn lifecycle(&self, template: Value) -> (TempDir, Lifecycle) {
        let directory = TempDir::new().unwrap();
        let kubeconfig = directory.path().join("config");
        std::fs::write(&kubeconfig, format!("apiVersion: v1\nkind: Config\nclusters:\n- name: fixture\n  cluster:\n    server: {}\ncontexts:\n- name: fixture\n  context:\n    cluster: fixture\n    user: fixture\ncurrent-context: fixture\nusers:\n- name: fixture\n  user: {{}}\n", self.endpoint)).unwrap();
        let mut config = AgentSandboxConfig::new("work", template);
        config.kubeconfig = Some(kubeconfig);
        (
            directory,
            Lifecycle::new(AgentSandboxLifecycle::new(config).unwrap()),
        )
    }
}

#[tokio::test]
async fn kubectl_cr_lifecycle_preserves_observed_state_and_suspend_semantics() {
    let fixture = Fixture::start().await;
    let template = json!({ "spec": { "containers": [{ "name": "main", "image": "python:3.14", "command": ["sleep", "infinity"] }] } });
    let (_directory, lifecycle) = fixture.lifecycle(template.clone());
    let resource = lifecycle
        .create(CreateOptions {
            name: Some("demo".into()),
            ..Default::default()
        })
        .await
        .unwrap();
    assert_eq!(
        lifecycle.inspect(&resource).await.unwrap().state,
        ResourceState::Running
    );
    lifecycle.stop(&resource).await.unwrap();
    assert_eq!(
        lifecycle.inspect(&resource).await.unwrap().state,
        ResourceState::Pending
    );
    fixture.observed("Suspended", "PodTerminated");
    assert_eq!(
        lifecycle.inspect(&resource).await.unwrap().state,
        ResourceState::Stopped
    );
    lifecycle.start(&resource).await.unwrap();
    assert_eq!(
        lifecycle.inspect(&resource).await.unwrap().state,
        ResourceState::Pending
    );
    fixture.observed("Ready", "DependenciesReady");
    assert_eq!(
        lifecycle.inspect(&resource).await.unwrap().state,
        ResourceState::Running
    );
    assert_eq!(
        lifecycle.fork(&resource).await.unwrap_err().kind,
        ErrorKind::Unsupported
    );
    fixture.state.lock().unwrap().deny = true;
    let error = lifecycle.inspect(&resource).await.unwrap_err();
    assert_eq!(error.kind, ErrorKind::Permission);
    assert!(!error.to_string().contains("kube-secret"));
    fixture.state.lock().unwrap().deny = false;
    lifecycle.delete(&resource).await.unwrap();
    assert_eq!(
        lifecycle.inspect(&resource).await.unwrap_err().kind,
        ErrorKind::NotFound
    );
    let state = fixture.state.lock().unwrap();
    assert_eq!(state.mutations.len(), 4);
    assert_eq!(
        state.mutations[0].1["apiVersion"],
        "agents.x-k8s.io/v1beta1"
    );
    assert_eq!(state.mutations[0].1["spec"]["podTemplate"], template);
    assert_eq!(state.mutations[1].1["spec"]["operatingMode"], "Suspended");
    assert_eq!(state.mutations[2].1["spec"]["operatingMode"], "Running");
}

#[tokio::test]
async fn kubectl_retry_after_reuses_the_explicit_generated_resource_name() {
    let fixture = Fixture::start().await;
    fixture.state.lock().unwrap().retry_after = true;
    let (_directory, lifecycle) = fixture.lifecycle(
        json!({ "spec": { "containers": [{ "name": "main", "image": "python:3.14" }] } }),
    );
    let error = lifecycle
        .create(CreateOptions::default())
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::Conflict);
    let state = fixture.state.lock().unwrap();
    assert_eq!(state.create_attempts.len(), 2);
    let name = state.create_attempts[0]["metadata"]["name"]
        .as_str()
        .unwrap();
    assert!(name.starts_with("opensol-"));
    assert_eq!(state.create_attempts[1]["metadata"]["name"], name);
    assert_eq!(state.resource.as_ref().unwrap()["metadata"]["name"], name);
    assert_eq!(state.mutations.len(), 1);
}
