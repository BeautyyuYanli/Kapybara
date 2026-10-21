//! Kubernetes SIGs agent-sandbox v1beta1 lifecycle through the user's kubectl authentication.
//!
//! Start/stop patch desired operatingMode and return before reconciliation. Suspended deletes the Pod
//! while retaining the CR and volumes, not its memory. Observed states use current-generation conditions.
//! sandboxd is an independent Connection and is not inferred from the old HTTP router.
//! Creation always uses an explicit name so client-go's Retry-After replay cannot create another CR.

use std::{path::PathBuf, time::Duration};

use async_trait::async_trait;
use opensol_core::{
    Command, Connection, CreateOptions, Error, ErrorKind, ExecRequest, Result, SandboxInfo,
    SandboxManagerCapabilities, SandboxRef, SandboxSpec, SandboxState, raw::SandboxManagerAccess,
};
use serde_json::{Value, json};

use crate::local::Local;

/// Kubernetes access scope only; creation specs are supplied separately on each create.
#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub struct AgentSandboxConfig {
    pub namespace: String,
    pub context: Option<String>,
    pub kubeconfig: Option<PathBuf>,
    pub program: PathBuf,
    pub request_timeout: Duration,
}

impl AgentSandboxConfig {
    pub fn new(namespace: impl Into<String>) -> Self {
        Self {
            namespace: namespace.into(),
            context: None,
            kubeconfig: None,
            program: "kubectl".into(),
            request_timeout: Duration::from_secs(30),
        }
    }
}

impl std::fmt::Debug for AgentSandboxConfig {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("AgentSandboxConfig")
            .field("namespace", &self.namespace)
            .finish()
    }
}

/// Native PodTemplate/PVC JSON is validated by Kubernetes, without an SDK policy model.
#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub struct AgentSandboxSpec {
    #[serde(flatten)]
    pub options: CreateOptions,
    pub pod_template: Value,
    pub volume_claim_templates: Vec<Value>,
    pub service: bool,
}

impl From<AgentSandboxSpec> for SandboxSpec {
    fn from(spec: AgentSandboxSpec) -> Self {
        Self::new("agent-sandbox", spec.options.clone(), spec)
    }
}

#[derive(Clone, Debug)]
pub struct AgentSandboxManager {
    config: AgentSandboxConfig,
}

impl AgentSandboxManager {
    pub fn new(config: AgentSandboxConfig) -> Result<Self> {
        if config.namespace.is_empty()
            || config.namespace.starts_with('-')
            || config.request_timeout.is_zero()
        {
            return Err(Error::invalid(
                "agent-sandbox",
                "configure",
                "namespace and positive timeout are required",
            ));
        }
        Ok(Self { config })
    }

    async fn run(&self, args: Vec<String>, stdin: Vec<u8>, operation: &str) -> Result<Vec<u8>> {
        let mut global = vec![
            format!("--namespace={}", self.config.namespace),
            format!(
                "--request-timeout={}s",
                self.config.request_timeout.as_secs_f64()
            ),
        ];
        if let Some(context) = &self.config.context {
            global.push(format!("--context={context}"));
        }
        if let Some(path) = &self.config.kubeconfig {
            global.push(format!("--kubeconfig={}", path.display()));
        }
        global.extend(args);
        let result = Connection::new(Local)
            .exec(ExecRequest {
                command: Command::new(self.config.program.to_string_lossy(), global),
                stdin,
                timeout: Some(self.config.request_timeout),
            })
            .await
            .map_err(|mut error| {
                error.backend = "agent-sandbox".into();
                error.operation = operation.into();
                error
            })?;
        if result.exit.success() {
            return Ok(result.stdout);
        }
        let stderr = String::from_utf8_lossy(&result.stderr);
        let kind = if stderr.contains("(NotFound)") {
            ErrorKind::NotFound
        } else if stderr.contains("(AlreadyExists)") || stderr.contains("(Conflict)") {
            ErrorKind::Conflict
        } else if stderr.contains("(Forbidden)") || stderr.contains("(Unauthorized)") {
            ErrorKind::Permission
        } else if stderr.contains("(Invalid)") || stderr.contains("(BadRequest)") {
            ErrorKind::InvalidArgument
        } else {
            ErrorKind::Backend
        };
        Err(Error::new(
            kind,
            "agent-sandbox",
            operation,
            "kubectl request failed",
        ))
    }

    async fn set_mode(&self, resource: &SandboxRef, mode: &str, operation: &str) -> Result<()> {
        self.run(
            vec![
                "patch".into(),
                "--type=merge".into(),
                format!("--patch={}", json!({ "spec": { "operatingMode": mode } })),
                "-o=json".into(),
                "--".into(),
                "sandboxes.agents.x-k8s.io".into(),
                resource.id.clone(),
            ],
            Vec::new(),
            operation,
        )
        .await?;
        Ok(())
    }
}

fn sandbox_info(value: &Value, operation: &str) -> Result<SandboxInfo> {
    let name = value
        .pointer("/metadata/name")
        .and_then(Value::as_str)
        .filter(|name| !name.is_empty())
        .ok_or_else(|| response_error(operation))?;
    let sandbox = SandboxRef::new("agent-sandbox", name);
    let labels = value
        .pointer("/metadata/labels")
        .map(|labels| serde_json::from_value(labels.clone()))
        .transpose()
        .map_err(|_| response_error(operation))?
        .unwrap_or_default();
    let generation = value
        .pointer("/metadata/generation")
        .and_then(Value::as_u64);
    let mut state = SandboxState::Pending;
    let mut native = "Reconciling".to_string();
    if let Some(conditions) = value
        .pointer("/status/conditions")
        .and_then(Value::as_array)
    {
        for condition in conditions {
            if generation.is_none()
                || condition.get("observedGeneration").and_then(Value::as_u64) != generation
            {
                continue;
            }
            let kind = condition.get("type").and_then(Value::as_str).unwrap_or("");
            let status = condition
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("");
            let reason = condition
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("");
            match (kind, status, reason) {
                ("Ready", "True", _) => {
                    state = SandboxState::Running;
                    native = "Ready".into();
                }
                ("Suspended", "True", _) => {
                    state = SandboxState::Stopped;
                    native = "Suspended".into();
                }
                ("Finished", "True", "PodSucceeded") => {
                    state = SandboxState::Completed;
                    native = "PodSucceeded".into();
                }
                ("Finished", "True", "PodFailed") => {
                    state = SandboxState::Failed;
                    native = "PodFailed".into();
                }
                ("Ready", "False", "InvalidConfiguration" | "ReconcilerError") => {
                    state = SandboxState::Failed;
                    native = reason.into();
                }
                _ => {}
            }
        }
    }
    Ok(SandboxInfo {
        sandbox,
        name: Some(name.into()),
        labels,
        state,
        native_state: native,
    })
}

#[async_trait]
impl SandboxManagerAccess for AgentSandboxManager {
    fn backend(&self) -> &'static str {
        "agent-sandbox"
    }
    fn capabilities(&self) -> SandboxManagerCapabilities {
        SandboxManagerCapabilities::BASIC
    }

    async fn list(&self) -> Result<Vec<SandboxInfo>> {
        let output = self
            .run(
                vec![
                    "get".into(),
                    "-o=json".into(),
                    "--".into(),
                    "sandboxes.agents.x-k8s.io".into(),
                ],
                Vec::new(),
                "list",
            )
            .await?;
        let value: Value = serde_json::from_slice(&output).map_err(|_| response_error("list"))?;
        value
            .get("items")
            .and_then(Value::as_array)
            .ok_or_else(|| response_error("list"))?
            .iter()
            .map(|sandbox| sandbox_info(sandbox, "list"))
            .collect()
    }

    async fn create(&self, spec: SandboxSpec) -> Result<SandboxRef> {
        let config = spec.payload::<AgentSandboxSpec>("agent-sandbox")?;
        if !config
            .pod_template
            .get("spec")
            .is_some_and(Value::is_object)
        {
            return Err(Error::invalid(
                "agent-sandbox",
                "create",
                "PodTemplate.spec is required",
            ));
        }
        let name = spec
            .options
            .name
            .clone()
            .unwrap_or_else(|| format!("opensol-{}", uuid::Uuid::new_v4().simple()));
        let metadata = json!({ "namespace": self.config.namespace, "labels": spec.options.labels, "name": name });
        let manifest = json!({ "apiVersion": "agents.x-k8s.io/v1beta1", "kind": "Sandbox", "metadata": metadata,
            "spec": { "podTemplate": config.pod_template, "volumeClaimTemplates": config.volume_claim_templates,
                "service": config.service, "operatingMode": "Running" } });
        let result = self
            .run(
                vec!["create".into(), "-f=-".into(), "-o=json".into()],
                manifest.to_string().into_bytes(),
                "create",
            )
            .await?;
        let value: Value = serde_json::from_slice(&result).map_err(|_| response_error("create"))?;
        let name = value
            .pointer("/metadata/name")
            .and_then(Value::as_str)
            .filter(|name| !name.is_empty())
            .ok_or_else(|| response_error("create"))?;
        Ok(SandboxRef::new("agent-sandbox", name))
    }

    async fn inspect(&self, resource: &SandboxRef) -> Result<SandboxInfo> {
        let output = self
            .run(
                vec![
                    "get".into(),
                    "-o=json".into(),
                    "--".into(),
                    "sandboxes.agents.x-k8s.io".into(),
                    resource.id.clone(),
                ],
                Vec::new(),
                "inspect",
            )
            .await?;
        let value: Value =
            serde_json::from_slice(&output).map_err(|_| response_error("inspect"))?;
        let info = sandbox_info(&value, "inspect")?;
        if info.sandbox != *resource {
            return Err(response_error("inspect"));
        }
        Ok(info)
    }

    async fn start(&self, resource: &SandboxRef) -> Result<()> {
        self.set_mode(resource, "Running", "start").await
    }
    async fn stop(&self, resource: &SandboxRef) -> Result<()> {
        self.set_mode(resource, "Suspended", "stop").await
    }

    async fn delete(&self, resource: &SandboxRef) -> Result<()> {
        self.run(
            vec![
                "delete".into(),
                "--wait=false".into(),
                "--".into(),
                "sandboxes.agents.x-k8s.io".into(),
                resource.id.clone(),
            ],
            Vec::new(),
            "delete",
        )
        .await?;
        Ok(())
    }
}

fn response_error(operation: &str) -> Error {
    Error::new(
        ErrorKind::Backend,
        "agent-sandbox",
        operation,
        "invalid kubectl JSON response",
    )
}
