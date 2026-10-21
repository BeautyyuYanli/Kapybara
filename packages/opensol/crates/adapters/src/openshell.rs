//! Native NVIDIA OpenShell gRPC lifecycle and completed exec, without CLI exit-code ambiguity.
//!
//! Authentication is explicit endpoint/TLS/bearer configuration, not implicit CLI login discovery.
//! Main-process connect/replay is intentionally not offered as arbitrary exec reconnect or history.

use std::collections::HashSet;

use async_trait::async_trait;
use opensol_core::{
    Connection, ConnectionCapabilities, CreateOptions, Error, ErrorKind, ExecRequest, ExecResult,
    ExitStatus, Result, SandboxInfo, SandboxManager, SandboxManagerCapabilities, SandboxRef,
    SandboxSpec, SandboxState,
    raw::{ConnectionAccess, SandboxManagerAccess},
};
use tonic::transport::Channel;

use crate::{
    grpc::{self, GrpcConfig},
    util::deadline,
};

/// Generated native types from OpenShell v0.1.4. Policy/spec types are provider configuration.
#[allow(
    clippy::large_enum_variant,
    clippy::doc_lazy_continuation,
    rustdoc::invalid_html_tags,
    reason = "Upstream protocol shapes and documentation are generated verbatim"
)]
pub mod protocol {
    pub mod datamodel {
        pub mod v1 {
            tonic::include_proto!("openshell.datamodel.v1");
        }
    }
    pub mod sandbox {
        pub mod v1 {
            tonic::include_proto!("openshell.sandbox.v1");
        }
    }
    pub mod v1 {
        tonic::include_proto!("openshell.v1");
    }
}

use protocol::{
    datamodel::v1::{WorkspaceSelector, workspace_selector::Selection},
    v1::{self as native, open_shell_client::OpenShellClient},
};

/// Explicit gateway access and workspace; no workload or sandbox creation state.
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct OpenShellConfig {
    pub grpc: GrpcConfig,
    pub workspace: String,
}

impl OpenShellConfig {
    pub fn new(endpoint: impl Into<String>, workspace: impl Into<String>) -> Self {
        Self {
            grpc: GrpcConfig::new(endpoint),
            workspace: workspace.into(),
        }
    }
}

/// Choose a native spec or a named workload template. The gateway owns policy and template validation.
#[derive(Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct OpenShellSandboxSpec {
    #[serde(flatten)]
    pub options: CreateOptions,
    #[serde(with = "native_spec")]
    pub spec: Option<native::SandboxSpec>,
    pub workload_template: Option<String>,
}

// Persist the pinned native protobuf bytes; do not generate a second public policy schema.
mod native_spec {
    use super::native;
    use prost::Message;
    use serde::{Deserialize, Serialize};

    pub fn serialize<S: serde::Serializer>(
        spec: &Option<native::SandboxSpec>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        spec.as_ref()
            .map(Message::encode_to_vec)
            .serialize(serializer)
    }
    pub fn deserialize<'de, D: serde::Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<native::SandboxSpec>, D::Error> {
        Option::<Vec<u8>>::deserialize(deserializer)?
            .map(|bytes| {
                native::SandboxSpec::decode(bytes.as_slice())
                    .map_err(|_| serde::de::Error::custom("invalid native SandboxSpec protobuf"))
            })
            .transpose()
    }
}

impl From<OpenShellSandboxSpec> for SandboxSpec {
    fn from(spec: OpenShellSandboxSpec) -> Self {
        Self::new("openshell", spec.options.clone(), spec)
    }
}

impl std::fmt::Debug for OpenShellSandboxSpec {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("OpenShellSandboxSpec")
            .field("spec", &"[redacted]")
            .field("workload_template", &self.workload_template)
            .finish()
    }
}

/// Reusable workspace-scoped lifecycle client. Resource IDs are canonical names, not gateway UUIDs.
#[derive(Clone)]
pub struct OpenShellSandboxManager {
    client: OpenShellClient<Channel>,
    config: GrpcConfig,
    workspace: String,
}

impl OpenShellSandboxManager {
    /// Bind within a Tokio runtime. Validation is local; the first RPC connects.
    pub fn new(config: GrpcConfig, workspace: impl Into<String>) -> Result<Self> {
        let workspace = workspace.into();
        if workspace.is_empty() || workspace.contains('\0') {
            return Err(Error::invalid(
                "openshell",
                "configure",
                "workspace is required and must contain no NUL",
            ));
        }
        Ok(Self {
            client: OpenShellClient::new(config.bind("openshell")?),
            config,
            workspace,
        })
    }

    pub async fn connect(config: GrpcConfig, workspace: impl Into<String>) -> Result<Self> {
        let mut client = Self::new(config, workspace)?;
        client.client = OpenShellClient::new(client.config.connect("openshell").await?);
        Ok(client)
    }

    fn scope(&self) -> Option<WorkspaceSelector> {
        Some(WorkspaceSelector {
            selection: Some(Selection::Workspace(self.workspace.clone())),
        })
    }

    pub fn sandboxes(&self) -> SandboxManager {
        SandboxManager::new(self.clone())
    }

    /// Bind the same gateway/workspace/TLS context, without starting the resource.
    pub fn connection(&self, resource: &SandboxRef) -> Result<Connection> {
        resource.validate("openshell", "connect")?;
        Ok(Connection::new(OpenShellConnection {
            client: self.client.clone(),
            config: self.config.clone(),
            workspace: self.workspace.clone(),
            sandbox: resource.id.clone(),
        }))
    }
}

fn response_error(operation: &str) -> Error {
    Error::new(
        ErrorKind::Backend,
        "openshell",
        operation,
        "invalid OpenShell response",
    )
}

fn sandbox_info(sandbox: native::Sandbox, operation: &str) -> Result<SandboxInfo> {
    let metadata = sandbox.metadata.ok_or_else(|| response_error(operation))?;
    let name = metadata.name;
    if name.is_empty() {
        return Err(response_error(operation));
    }
    let phase = sandbox
        .status
        .map(|status| status.phase)
        .unwrap_or_default();
    let native = native::SandboxPhase::try_from(phase).unwrap_or(native::SandboxPhase::Unknown);
    let state = match native {
        native::SandboxPhase::Ready => SandboxState::Running,
        native::SandboxPhase::Stopped => SandboxState::Stopped,
        native::SandboxPhase::Completed => SandboxState::Completed,
        native::SandboxPhase::Error => SandboxState::Failed,
        native::SandboxPhase::Provisioning
        | native::SandboxPhase::Starting
        | native::SandboxPhase::Stopping
        | native::SandboxPhase::Deleting => SandboxState::Pending,
        _ => SandboxState::Unknown,
    };
    Ok(SandboxInfo {
        sandbox: SandboxRef::new("openshell", &name),
        name: Some(name),
        labels: metadata.labels.into_iter().collect(),
        state,
        native_state: native.as_str_name().into(),
    })
}

#[async_trait]
impl SandboxManagerAccess for OpenShellSandboxManager {
    fn backend(&self) -> &'static str {
        "openshell"
    }
    fn capabilities(&self) -> SandboxManagerCapabilities {
        SandboxManagerCapabilities::BASIC
    }

    async fn list(&self) -> Result<Vec<SandboxInfo>> {
        let mut sandboxes = Vec::new();
        let mut page_token = String::new();
        let mut tokens = HashSet::new();
        loop {
            let response = self
                .client
                .clone()
                .list_sandboxes(self.config.request(
                    native::ListSandboxesRequest {
                        workspace_scope: self.scope(),
                        page_size: 100,
                        page_token,
                        label_selector: String::new(),
                    },
                    "openshell",
                )?)
                .await
                .map_err(|error| grpc::status(error, "openshell", "list"))?
                .into_inner();
            for sandbox in response.sandboxes {
                sandboxes.push(sandbox_info(sandbox, "list")?);
            }
            page_token = response.next_page_token;
            if page_token.is_empty() {
                break;
            }
            if !tokens.insert(page_token.clone()) {
                return Err(response_error("list"));
            }
        }
        Ok(sandboxes)
    }

    async fn create(&self, spec: SandboxSpec) -> Result<SandboxRef> {
        let create = spec.payload::<OpenShellSandboxSpec>("openshell")?;
        if create.spec.is_some() == create.workload_template.is_some()
            || create
                .workload_template
                .as_ref()
                .is_some_and(|s| s.is_empty() || s.contains('\0'))
        {
            return Err(Error::invalid(
                "openshell",
                "create",
                "choose exactly one native spec or workload template",
            ));
        }
        let message = native::CreateSandboxRequest {
            workspace_scope: self.scope(),
            spec: create.spec.clone(),
            name: spec.options.name.clone().unwrap_or_default(),
            labels: spec.options.labels.clone().into_iter().collect(),
            workload_template: create.workload_template.clone().unwrap_or_default(),
            ..Default::default()
        };
        let response = self
            .client
            .clone()
            .create_sandbox(self.config.request(message, "openshell")?)
            .await
            .map_err(|error| grpc::status(error, "openshell", "create"))?
            .into_inner();
        let name = response
            .sandbox
            .and_then(|sandbox| sandbox.metadata)
            .map(|metadata| metadata.name)
            .filter(|name| !name.is_empty())
            .ok_or_else(|| response_error("create"))?;
        Ok(SandboxRef::new("openshell", name))
    }

    async fn inspect(&self, resource: &SandboxRef) -> Result<SandboxInfo> {
        let response = self
            .client
            .clone()
            .get_sandbox(self.config.request(
                native::GetSandboxRequest {
                    workspace_scope: self.scope(),
                    name: resource.id.clone(),
                },
                "openshell",
            )?)
            .await
            .map_err(|error| grpc::status(error, "openshell", "inspect"))?
            .into_inner();
        let sandbox = response.sandbox.ok_or_else(|| response_error("inspect"))?;
        let info = sandbox_info(sandbox, "inspect")?;
        if info.sandbox != *resource {
            return Err(response_error("inspect"));
        }
        Ok(info)
    }

    async fn start(&self, resource: &SandboxRef) -> Result<()> {
        self.client
            .clone()
            .start_sandbox(self.config.request(
                native::StartSandboxRequest {
                    workspace_scope: self.scope(),
                    name: resource.id.clone(),
                    request_id: String::new(),
                },
                "openshell",
            )?)
            .await
            .map_err(|error| grpc::status(error, "openshell", "start"))?;
        Ok(())
    }

    async fn stop(&self, resource: &SandboxRef) -> Result<()> {
        self.client
            .clone()
            .stop_sandbox(self.config.request(
                native::StopSandboxRequest {
                    workspace_scope: self.scope(),
                    name: resource.id.clone(),
                    request_id: String::new(),
                },
                "openshell",
            )?)
            .await
            .map_err(|error| grpc::status(error, "openshell", "stop"))?;
        Ok(())
    }

    async fn delete(&self, resource: &SandboxRef) -> Result<()> {
        self.client
            .clone()
            .delete_sandbox(self.config.request(
                native::DeleteSandboxRequest {
                    workspace_scope: self.scope(),
                    name: resource.id.clone(),
                    allow_missing: false,
                    request_id: String::new(),
                },
                "openshell",
            )?)
            .await
            .map_err(|error| grpc::status(error, "openshell", "delete"))?;
        Ok(())
    }
}

/// Completed exec in an existing workspace/name, independent of resource creation.
#[derive(Clone)]
pub struct OpenShellConnection {
    client: OpenShellClient<Channel>,
    config: GrpcConfig,
    workspace: String,
    sandbox: String,
}

impl OpenShellConnection {
    pub async fn connect(
        config: GrpcConfig,
        workspace: impl Into<String>,
        sandbox: impl Into<String>,
    ) -> Result<Self> {
        let workspace = workspace.into();
        let sandbox = sandbox.into();
        if workspace.is_empty() || sandbox.is_empty() {
            return Err(Error::invalid(
                "openshell",
                "connect",
                "workspace and sandbox name are required",
            ));
        }
        Ok(Self {
            client: OpenShellClient::new(config.connect("openshell").await?),
            config,
            workspace,
            sandbox,
        })
    }
}

#[async_trait]
impl ConnectionAccess for OpenShellConnection {
    fn backend(&self) -> &'static str {
        "openshell"
    }
    fn capabilities(&self) -> ConnectionCapabilities {
        ConnectionCapabilities {
            stdin: Some(opensol_core::InputConstraints::BYTES),
            timeout: true,
            separate_stderr: true,
            cancellation: opensol_core::ExecCancellation::NoTargetTerminationGuarantee,
        }
    }

    async fn exec(&self, request: ExecRequest) -> Result<ExecResult> {
        deadline(request.timeout, "openshell", "exec", async {
            let mut lookup = self.config.request(
                native::GetSandboxRequest {
                    workspace_scope: Some(WorkspaceSelector {
                        selection: Some(Selection::Workspace(self.workspace.clone())),
                    }),
                    name: self.sandbox.clone(),
                },
                "openshell",
            )?;
            lookup.metadata_mut().insert(
                "x-openshell-want-owner",
                "1".parse().expect("static metadata"),
            );
            let response = self
                .client
                .clone()
                .get_sandbox(lookup)
                .await
                .map_err(|error| grpc::status(error, "openshell", "exec"))?;
            let owner = response.metadata().get("x-openshell-owner").cloned();
            let execution_timeout = request
                .timeout
                .map(prost_types::Duration::try_from)
                .transpose()
                .map_err(|_| {
                    Error::invalid(
                        "openshell",
                        "exec",
                        "timeout exceeds protobuf duration range",
                    )
                })?;
            let message = native::ExecSandboxRequest {
                workspace_scope: Some(WorkspaceSelector {
                    selection: Some(Selection::Workspace(self.workspace.clone())),
                }),
                sandbox: self.sandbox.clone(),
                command: request.command.argv,
                workdir: request.command.cwd.unwrap_or_default(),
                environment: request.command.env.into_iter().collect(),
                stdin: request.stdin,
                execution_timeout,
                no_login_shell: true,
                ..Default::default()
            };
            let mut message = self.config.request(message, "openshell")?;
            if let Some(owner) = owner {
                message.metadata_mut().insert("x-openshell-replica", owner);
            }
            message.metadata_mut().remove("grpc-timeout");
            if let Some(timeout) = request.timeout {
                message.set_timeout(timeout);
            }
            let mut stream = self
                .client
                .clone()
                .exec_sandbox(message)
                .await
                .map_err(|error| grpc::status(error, "openshell", "exec"))?
                .into_inner();
            let mut stdout = Vec::new();
            let mut stderr = Vec::new();
            while let Some(event) = stream
                .message()
                .await
                .map_err(|error| grpc::status(error, "openshell", "exec"))?
            {
                match event.payload {
                    Some(native::exec_sandbox_event::Payload::Stdout(event)) => {
                        stdout.extend(event.data)
                    }
                    Some(native::exec_sandbox_event::Payload::Stderr(event)) => {
                        stderr.extend(event.data)
                    }
                    Some(native::exec_sandbox_event::Payload::Exit(event)) => {
                        return Ok(ExecResult {
                            stdout,
                            stderr,
                            exit: ExitStatus::code(event.exit_code),
                        });
                    }
                    None => return Err(response_error("exec")),
                }
            }
            Err(Error::new(
                ErrorKind::Transport,
                "openshell",
                "exec",
                "exec stream ended before exit status",
            ))
        })
        .await
    }
}
