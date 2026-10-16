//! Native NVIDIA OpenShell gRPC lifecycle and completed exec, without CLI exit-code ambiguity.
//!
//! Authentication is explicit endpoint/TLS/bearer configuration, not implicit CLI login discovery.
//! Main-process connect/replay is intentionally not offered as arbitrary exec reconnect or history.

use async_trait::async_trait;
use opensol_core::{
    Connection, ConnectionCapabilities, CreateOptions, Error, ErrorKind, ExecRequest, ExecResult,
    ExitStatus, Lifecycle, LifecycleCapabilities, ResourceInfo, ResourceRef, ResourceState, Result,
    raw::{ExecAccess, LifecycleAccess},
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

/// Choose a native spec or a named workload template. The gateway owns policy and template validation.
#[derive(Clone, Default)]
pub struct OpenShellCreate {
    pub spec: Option<native::SandboxSpec>,
    pub workload_template: Option<String>,
}

impl std::fmt::Debug for OpenShellCreate {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("OpenShellCreate")
            .field("spec", &"[redacted]")
            .field("workload_template", &self.workload_template)
            .finish()
    }
}

/// Reusable workspace-scoped lifecycle client. Resource IDs are canonical names, not gateway UUIDs.
#[derive(Clone)]
pub struct OpenShellLifecycle {
    client: OpenShellClient<Channel>,
    config: GrpcConfig,
    workspace: String,
    create: OpenShellCreate,
}

impl OpenShellLifecycle {
    pub async fn connect(
        config: GrpcConfig,
        workspace: impl Into<String>,
        create: OpenShellCreate,
    ) -> Result<Self> {
        let workspace = workspace.into();
        if workspace.is_empty() || create.spec.is_some() && create.workload_template.is_some() {
            return Err(Error::invalid(
                "openshell",
                "configure",
                "workspace is required; choose spec or workload template, not both",
            ));
        }
        Ok(Self {
            client: OpenShellClient::new(config.connect("openshell").await?),
            config,
            workspace,
            create,
        })
    }

    fn scope(&self) -> Option<WorkspaceSelector> {
        Some(WorkspaceSelector {
            selection: Some(Selection::Workspace(self.workspace.clone())),
        })
    }

    pub fn lifecycle(&self) -> Lifecycle {
        Lifecycle::new(self.clone())
    }

    /// Bind the same gateway/workspace/TLS context, without starting the resource.
    pub fn connection(&self, resource: &ResourceRef) -> Result<Connection> {
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

#[async_trait]
impl LifecycleAccess for OpenShellLifecycle {
    fn backend(&self) -> &'static str {
        "openshell"
    }
    fn capabilities(&self) -> LifecycleCapabilities {
        LifecycleCapabilities::BASIC
    }

    async fn create(&self, options: CreateOptions) -> Result<ResourceRef> {
        let message = native::CreateSandboxRequest {
            workspace_scope: self.scope(),
            spec: self.create.spec.clone(),
            name: options.name.unwrap_or_default(),
            labels: options.labels.into_iter().collect(),
            workload_template: self.create.workload_template.clone().unwrap_or_default(),
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
        Ok(ResourceRef::new("openshell", name))
    }

    async fn inspect(&self, resource: &ResourceRef) -> Result<ResourceInfo> {
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
        let phase = sandbox
            .status
            .map(|status| status.phase)
            .unwrap_or_default();
        let native = native::SandboxPhase::try_from(phase).unwrap_or(native::SandboxPhase::Unknown);
        let state = match native {
            native::SandboxPhase::Ready => ResourceState::Running,
            native::SandboxPhase::Stopped => ResourceState::Stopped,
            native::SandboxPhase::Completed => ResourceState::Completed,
            native::SandboxPhase::Error => ResourceState::Failed,
            native::SandboxPhase::Provisioning
            | native::SandboxPhase::Starting
            | native::SandboxPhase::Stopping
            | native::SandboxPhase::Deleting => ResourceState::Pending,
            _ => ResourceState::Unknown,
        };
        Ok(ResourceInfo {
            resource: resource.clone(),
            state,
            native_state: native.as_str_name().into(),
        })
    }

    async fn start(&self, resource: &ResourceRef) -> Result<()> {
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

    async fn stop(&self, resource: &ResourceRef) -> Result<()> {
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

    async fn delete(&self, resource: &ResourceRef) -> Result<()> {
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
impl ExecAccess for OpenShellConnection {
    fn backend(&self) -> &'static str {
        "openshell"
    }
    fn capabilities(&self) -> ConnectionCapabilities {
        ConnectionCapabilities {
            stdin: true,
            timeout: true,
            separate_stderr: true,
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
