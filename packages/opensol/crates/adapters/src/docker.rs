//! Docker Engine lifecycle and completed exec via Bollard. Images are not pulled implicitly.
//!
//! Create produces a stopped container; start is explicit. Stop loses process memory. Exec deadline
//! cancellation only closes the attachment: Docker has no individual-exec kill or output replay API.
//! Native completion cancels unconsumed stdin; output is still drained before returning the exit code.

use std::{
    collections::{BTreeMap, HashMap},
    sync::Arc,
    time::Duration,
};

use async_trait::async_trait;
use bollard::{
    Docker,
    container::LogOutput,
    exec::StartExecResults,
    models::{ContainerCreateBody, ExecConfig, HostConfig},
    query_parameters::{
        CreateContainerOptions, CreateContainerOptionsBuilder, InspectContainerOptions,
        RemoveContainerOptionsBuilder, StartContainerOptions, StopContainerOptions,
    },
};
use futures::StreamExt;
use opensol_core::{
    Connection, ConnectionCapabilities, CreateOptions, Error, ErrorKind, ExecRequest, ExecResult,
    ExitStatus, Lifecycle, LifecycleCapabilities, ResourceInfo, ResourceRef, ResourceState, Result,
    raw::{ExecAccess, LifecycleAccess},
};
use tokio::io::AsyncWriteExt;

use crate::util::deadline;

/// Native container creation settings; caller controls isolation through Docker's HostConfig.
#[derive(Clone, Default)]
pub struct DockerConfig {
    pub image: String,
    pub command: Vec<String>,
    pub env: BTreeMap<String, String>,
    pub working_dir: Option<String>,
    pub host_config: Option<HostConfig>,
}

impl std::fmt::Debug for DockerConfig {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("DockerConfig")
            .field("image", &self.image)
            .field("command", &"[redacted]")
            .field("env", &"[redacted]")
            .field("host_config", &"[configured]")
            .finish()
    }
}

/// Owns an Engine client, not the lifetime of containers it creates.
#[derive(Clone)]
pub struct DockerLifecycle {
    client: Arc<Docker>,
    config: DockerConfig,
}

impl DockerLifecycle {
    pub fn new(client: Docker, config: DockerConfig) -> Result<Self> {
        if config.image.is_empty() {
            return Err(Error::invalid("docker", "configure", "image is required"));
        }
        Ok(Self {
            client: Arc::new(client),
            config,
        })
    }

    pub fn connect_defaults(config: DockerConfig) -> Result<Self> {
        Self::new(
            Docker::connect_with_defaults().map_err(|error| docker_error(error, "connect"))?,
            config,
        )
    }

    pub fn lifecycle(&self) -> Lifecycle {
        Lifecycle::new(self.clone())
    }

    /// Bind a container using the same Engine client, without checking or starting it.
    pub fn connection(&self, resource: &ResourceRef) -> Result<Connection> {
        resource.validate("docker", "connect")?;
        Ok(Connection::new(DockerConnection {
            client: self.client.clone(),
            container: resource.id.clone(),
        }))
    }
}

fn docker_error(error: bollard::errors::Error, operation: &str) -> Error {
    let kind = match error {
        bollard::errors::Error::DockerResponseServerError { status_code, .. } => {
            match status_code {
                400 => ErrorKind::InvalidArgument,
                401 | 403 => ErrorKind::Permission,
                404 => ErrorKind::NotFound,
                409 | 429 => ErrorKind::Conflict,
                408 | 504 => ErrorKind::Timeout,
                _ => ErrorKind::Backend,
            }
        }
        bollard::errors::Error::RequestTimeoutError => ErrorKind::Timeout,
        _ => ErrorKind::Transport,
    };
    Error::new(kind, "docker", operation, "Docker Engine request failed")
}

#[async_trait]
impl LifecycleAccess for DockerLifecycle {
    fn backend(&self) -> &'static str {
        "docker"
    }
    fn capabilities(&self) -> LifecycleCapabilities {
        LifecycleCapabilities::BASIC
    }

    async fn create(&self, options: CreateOptions) -> Result<ResourceRef> {
        let query: Option<CreateContainerOptions> = options
            .name
            .as_deref()
            .map(|name| CreateContainerOptionsBuilder::default().name(name).build());
        let config = ContainerCreateBody {
            image: Some(self.config.image.clone()),
            cmd: if self.config.command.is_empty() {
                None
            } else {
                Some(self.config.command.clone())
            },
            env: Some(
                self.config
                    .env
                    .iter()
                    .map(|(key, value)| format!("{key}={value}"))
                    .collect(),
            ),
            working_dir: self.config.working_dir.clone(),
            host_config: self.config.host_config.clone(),
            labels: Some(HashMap::from_iter(options.labels)),
            ..Default::default()
        };
        let response = self
            .client
            .create_container(query, config)
            .await
            .map_err(|error| docker_error(error, "create"))?;
        Ok(ResourceRef::new("docker", response.id))
    }

    async fn inspect(&self, resource: &ResourceRef) -> Result<ResourceInfo> {
        let info = self
            .client
            .inspect_container(&resource.id, None::<InspectContainerOptions>)
            .await
            .map_err(|error| docker_error(error, "inspect"))?;
        let native = info
            .state
            .and_then(|state| state.status)
            .map(|state| state.to_string())
            .unwrap_or_else(|| "unknown".into());
        let state = match native.as_str() {
            "running" => ResourceState::Running,
            "created" | "exited" | "paused" => ResourceState::Stopped,
            "dead" => ResourceState::Failed,
            "restarting" | "removing" => ResourceState::Pending,
            _ => ResourceState::Unknown,
        };
        Ok(ResourceInfo {
            resource: resource.clone(),
            state,
            native_state: native,
        })
    }

    async fn start(&self, resource: &ResourceRef) -> Result<()> {
        self.client
            .start_container(&resource.id, None::<StartContainerOptions>)
            .await
            .map_err(|error| docker_error(error, "start"))
    }

    async fn stop(&self, resource: &ResourceRef) -> Result<()> {
        self.client
            .stop_container(&resource.id, None::<StopContainerOptions>)
            .await
            .map_err(|error| docker_error(error, "stop"))
    }

    async fn delete(&self, resource: &ResourceRef) -> Result<()> {
        self.client
            .remove_container(
                &resource.id,
                Some(RemoveContainerOptionsBuilder::default().force(true).build()),
            )
            .await
            .map_err(|error| docker_error(error, "delete"))
    }
}

/// Completed commands in an existing container. No lifecycle object is required.
#[derive(Clone)]
pub struct DockerConnection {
    client: Arc<Docker>,
    container: String,
}

impl DockerConnection {
    pub fn new(client: Docker, container: impl Into<String>) -> Result<Self> {
        let container = container.into();
        ResourceRef::new("docker", &container).validate("docker", "connect")?;
        Ok(Self {
            client: Arc::new(client),
            container,
        })
    }

    pub fn connect_defaults(container: impl Into<String>) -> Result<Self> {
        Self::new(
            Docker::connect_with_defaults().map_err(|error| docker_error(error, "connect"))?,
            container,
        )
    }
}

#[async_trait]
impl ExecAccess for DockerConnection {
    fn backend(&self) -> &'static str {
        "docker"
    }
    fn capabilities(&self) -> ConnectionCapabilities {
        ConnectionCapabilities {
            stdin: true,
            timeout: true,
            separate_stderr: true,
        }
    }

    async fn exec(&self, request: ExecRequest) -> Result<ExecResult> {
        deadline(request.timeout, "docker", "exec", async {
            let exec = self
                .client
                .create_exec(
                    &self.container,
                    ExecConfig {
                        attach_stdin: Some(!request.stdin.is_empty()),
                        attach_stdout: Some(true),
                        attach_stderr: Some(true),
                        tty: Some(false),
                        cmd: Some(request.command.argv),
                        working_dir: request.command.cwd,
                        env: Some(
                            request
                                .command
                                .env
                                .into_iter()
                                .map(|(key, value)| format!("{key}={value}"))
                                .collect(),
                        ),
                        ..Default::default()
                    },
                )
                .await
                .map_err(|error| docker_error(error, "exec"))?;
            let StartExecResults::Attached {
                mut output,
                mut input,
            } = self
                .client
                .start_exec(&exec.id, None)
                .await
                .map_err(|error| docker_error(error, "exec"))?
            else {
                return Err(Error::new(
                    ErrorKind::Backend,
                    "docker",
                    "exec",
                    "Engine did not return an attached exec",
                ));
            };
            let write = async move {
                if !request.stdin.is_empty() {
                    input.write_all(&request.stdin).await.map_err(|_| {
                        Error::new(
                            ErrorKind::Transport,
                            "docker",
                            "exec",
                            "exec stdin write failed",
                        )
                    })?;
                    input.shutdown().await.map_err(|_| {
                        Error::new(
                            ErrorKind::Transport,
                            "docker",
                            "exec",
                            "exec stdin close failed",
                        )
                    })?;
                }
                Ok::<_, Error>(())
            };
            let read = async {
                let mut stdout = Vec::new();
                let mut stderr = Vec::new();
                while let Some(chunk) = output.next().await {
                    match chunk.map_err(|error| docker_error(error, "exec"))? {
                        LogOutput::StdOut { message } | LogOutput::Console { message } => {
                            stdout.extend(message)
                        }
                        LogOutput::StdErr { message } => stderr.extend(message),
                        LogOutput::StdIn { .. } => {}
                    }
                }
                Ok::<_, Error>((stdout, stderr))
            };
            let completion = async {
                loop {
                    let info = self
                        .client
                        .inspect_exec(&exec.id)
                        .await
                        .map_err(|error| docker_error(error, "exec"))?;
                    if info.running != Some(true) {
                        return info
                            .exit_code
                            .and_then(|code| i32::try_from(code).ok())
                            .map(ExitStatus::code)
                            .ok_or_else(|| {
                                Error::new(
                                    ErrorKind::Backend,
                                    "docker",
                                    "exec",
                                    "Engine returned no exit code",
                                )
                            });
                    }
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            };
            tokio::pin!(completion);
            let wait = async {
                tokio::select! {
                    exit = &mut completion => exit,
                    result = write => {
                        if let Err(error) = result {
                            let info = self.client.inspect_exec(&exec.id).await
                                .map_err(|error| docker_error(error, "exec"))?;
                            if info.running == Some(true) {
                                return Err(error);
                            }
                        }
                        completion.await
                    }
                }
            };
            let (exit, (stdout, stderr)) = tokio::try_join!(wait, read)?;
            Ok(ExecResult {
                stdout,
                stderr,
                exit,
            })
        })
        .await
    }
}
