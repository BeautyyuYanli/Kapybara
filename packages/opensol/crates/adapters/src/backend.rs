//! Unified built-in configuration and dispatch. The core does not enumerate providers.
//!
//! Opening never creates resources, starts daemons or probes readiness. E2B caches only native
//! envd clients per target; public clients are freshly bound and retain the owning Backend.

use async_trait::async_trait;
use opensol_core::{
    Api, Backend, BackendAccess, Connection, Error, OpenOptions, ProcessManager, Result,
    SandboxManager, SandboxRef,
};

#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
#[serde(tag = "backend", content = "config", rename_all = "snake_case")]
pub enum BackendConfig {
    #[cfg(feature = "local")]
    Local(crate::local::LocalConfig),
    #[cfg(feature = "ssh")]
    Ssh(crate::ssh::SshConfig),
    #[cfg(feature = "docker")]
    Docker(crate::docker::DockerConfig),
    #[cfg(feature = "e2b")]
    E2b(crate::e2b::E2bConfig),
    #[cfg(feature = "envd")]
    Envd(crate::envd::EnvdConfig),
    #[cfg(feature = "agent-sandbox")]
    #[serde(rename = "agent-sandbox")]
    AgentSandbox(crate::agent_sandbox::AgentSandboxConfig),
    #[cfg(feature = "openshell")]
    #[serde(rename = "openshell")]
    OpenShell(crate::openshell::OpenShellConfig),
    #[cfg(feature = "sandboxd")]
    Sandboxd(crate::sandboxd::SandboxdConfig),
    #[cfg(feature = "pueue")]
    Pueue(crate::pueue::PueueConfig),
    #[cfg(feature = "tmux")]
    Tmux(crate::tmux::TmuxConfig),
}

impl BackendConfig {
    pub fn backend(&self) -> &'static str {
        match self {
            #[cfg(feature = "local")]
            Self::Local(_) => "local",
            #[cfg(feature = "ssh")]
            Self::Ssh(_) => "ssh",
            #[cfg(feature = "docker")]
            Self::Docker(_) => "docker",
            #[cfg(feature = "e2b")]
            Self::E2b(_) => "e2b",
            #[cfg(feature = "envd")]
            Self::Envd(_) => "envd",
            #[cfg(feature = "agent-sandbox")]
            Self::AgentSandbox(_) => "agent-sandbox",
            #[cfg(feature = "openshell")]
            Self::OpenShell(_) => "openshell",
            #[cfg(feature = "sandboxd")]
            Self::Sandboxd(_) => "sandboxd",
            #[cfg(feature = "pueue")]
            Self::Pueue(_) => "pueue",
            #[cfg(feature = "tmux")]
            Self::Tmux(_) => "tmux",
        }
    }
}

fn binding(kind: &str, api: Api, target: Option<&SandboxRef>) -> Result<()> {
    let supported = match api {
        Api::Sandboxes => matches!(kind, "docker" | "e2b" | "agent-sandbox" | "openshell"),
        Api::Connection => matches!(
            kind,
            "local" | "ssh" | "docker" | "e2b" | "envd" | "openshell" | "sandboxd"
        ),
        Api::Processes => matches!(kind, "e2b" | "envd" | "pueue" | "tmux"),
    };
    if !supported {
        return Err(Error::unsupported(kind, "open"));
    }
    let needs_target = api != Api::Sandboxes && matches!(kind, "docker" | "e2b" | "openshell");
    match (needs_target, target) {
        (true, Some(reference)) => reference.validate(kind, "open"),
        (true, None) => Err(Error::invalid(
            kind,
            "open",
            "SandboxRef target is required",
        )),
        (false, Some(_)) => Err(Error::invalid(
            kind,
            "open",
            "this API does not accept a sandbox target",
        )),
        (false, None) => Ok(()),
    }
}

fn dependency(kind: &str, present: bool) -> Result<()> {
    if matches!(kind, "pueue" | "tmux") != present {
        return Err(Error::invalid(
            kind,
            "open",
            "Pueue/Tmux require an explicit Connection; other backends do not accept one",
        ));
    }
    Ok(())
}

#[derive(Clone)]
enum Native {
    #[cfg(feature = "local")]
    Local,
    #[cfg(feature = "ssh")]
    Ssh(crate::ssh::Ssh),
    #[cfg(feature = "docker")]
    Docker(crate::docker::DockerSandboxManager),
    #[cfg(feature = "e2b")]
    E2b(crate::e2b::E2bSandboxManager),
    #[cfg(feature = "envd")]
    Envd(crate::envd::Envd),
    #[cfg(feature = "agent-sandbox")]
    AgentSandbox(crate::agent_sandbox::AgentSandboxManager),
    #[cfg(feature = "openshell")]
    OpenShell(crate::openshell::OpenShellSandboxManager),
    #[cfg(feature = "sandboxd")]
    Sandboxd(crate::sandboxd::Sandboxd),
    #[cfg(feature = "pueue")]
    Pueue(crate::pueue::Pueue),
    #[cfg(feature = "tmux")]
    Tmux(crate::tmux::Tmux),
}

struct Context {
    config: BackendConfig,
    native: Native,
    #[cfg(feature = "e2b")]
    envd: tokio::sync::Mutex<std::collections::HashMap<String, crate::envd::Envd>>,
}

#[async_trait]
impl opensol_core::BackendConfig for BackendConfig {
    fn validate_open(&self, api: Api, options: &OpenOptions) -> Result<()> {
        binding(self.backend(), api, options.target.as_ref())?;
        dependency(self.backend(), options.connection.is_some())
    }

    async fn open(&self, connection: Option<Connection>) -> Result<Backend> {
        dependency(self.backend(), connection.is_some())?;
        let native = match self {
            #[cfg(feature = "local")]
            Self::Local(_) => Native::Local,
            #[cfg(feature = "ssh")]
            Self::Ssh(config) => Native::Ssh(crate::ssh::Ssh::new(config.clone())?),
            #[cfg(feature = "docker")]
            Self::Docker(config) => Native::Docker(
                crate::docker::DockerSandboxManager::connect_defaults(config.clone())?,
            ),
            #[cfg(feature = "e2b")]
            Self::E2b(config) => Native::E2b(crate::e2b::E2bSandboxManager::new(config.clone())?),
            #[cfg(feature = "envd")]
            Self::Envd(config) => Native::Envd(crate::envd::Envd::new(config.clone())?),
            #[cfg(feature = "agent-sandbox")]
            Self::AgentSandbox(config) => Native::AgentSandbox(
                crate::agent_sandbox::AgentSandboxManager::new(config.clone())?,
            ),
            #[cfg(feature = "openshell")]
            Self::OpenShell(config) => Native::OpenShell(
                crate::openshell::OpenShellSandboxManager::connect(
                    config.grpc.clone(),
                    config.workspace.clone(),
                )
                .await?,
            ),
            #[cfg(feature = "sandboxd")]
            Self::Sandboxd(config) => {
                Native::Sandboxd(crate::sandboxd::Sandboxd::connect(config.grpc.clone()).await?)
            }
            #[cfg(feature = "pueue")]
            Self::Pueue(config) => Native::Pueue(crate::pueue::Pueue::new(
                connection.expect("validated dependency"),
                config.clone(),
            )?),
            #[cfg(feature = "tmux")]
            Self::Tmux(config) => Native::Tmux(crate::tmux::Tmux::new(
                connection.expect("validated dependency"),
                config.clone(),
            )?),
        };
        Ok(Backend::new(Context {
            config: self.clone(),
            native,
            #[cfg(feature = "e2b")]
            envd: Default::default(),
        }))
    }
}

impl Context {
    #[cfg(feature = "e2b")]
    async fn envd(
        &self,
        client: &crate::e2b::E2bSandboxManager,
        target: &SandboxRef,
    ) -> Result<crate::envd::Envd> {
        let mut contexts = self.envd.lock().await;
        if let Some(envd) = contexts.get(&target.id) {
            return Ok(envd.clone());
        }
        let envd = client.envd(target).await?;
        contexts.insert(target.id.clone(), envd.clone());
        Ok(envd)
    }
}

#[async_trait]
impl BackendAccess for Context {
    fn backend(&self) -> &'static str {
        self.config.backend()
    }
    fn validate_open(&self, api: Api, target: Option<&SandboxRef>) -> Result<()> {
        binding(self.backend(), api, target)
    }
    fn sandboxes(&self) -> Result<SandboxManager> {
        match &self.native {
            #[cfg(feature = "docker")]
            Native::Docker(client) => Ok(client.sandboxes()),
            #[cfg(feature = "e2b")]
            Native::E2b(client) => Ok(client.sandboxes()),
            #[cfg(feature = "agent-sandbox")]
            Native::AgentSandbox(client) => Ok(SandboxManager::new(client.clone())),
            #[cfg(feature = "openshell")]
            Native::OpenShell(client) => Ok(client.sandboxes()),
            #[allow(unreachable_patterns)]
            _ => Err(Error::unsupported(self.backend(), "open_sandboxes")),
        }
    }
    async fn connection(&self, _target: Option<&SandboxRef>) -> Result<Connection> {
        match &self.native {
            #[cfg(feature = "local")]
            Native::Local => Ok(Connection::new(crate::local::Local)),
            #[cfg(feature = "ssh")]
            Native::Ssh(client) => Ok(Connection::new(client.clone())),
            #[cfg(feature = "docker")]
            Native::Docker(client) => client.connection(_target.expect("validated target")),
            #[cfg(feature = "e2b")]
            Native::E2b(client) => Ok(self
                .envd(client, _target.expect("validated target"))
                .await?
                .connection()),
            #[cfg(feature = "envd")]
            Native::Envd(client) => Ok(client.connection()),
            #[cfg(feature = "openshell")]
            Native::OpenShell(client) => client.connection(_target.expect("validated target")),
            #[cfg(feature = "sandboxd")]
            Native::Sandboxd(client) => Ok(Connection::new(client.clone())),
            #[allow(unreachable_patterns)]
            _ => Err(Error::unsupported(self.backend(), "open_connection")),
        }
    }
    async fn processes(&self, _target: Option<&SandboxRef>) -> Result<ProcessManager> {
        match &self.native {
            #[cfg(feature = "e2b")]
            Native::E2b(client) => Ok(self
                .envd(client, _target.expect("validated target"))
                .await?
                .processes()),
            #[cfg(feature = "envd")]
            Native::Envd(client) => Ok(client.processes()),
            #[cfg(feature = "pueue")]
            Native::Pueue(client) => Ok(ProcessManager::new(client.clone())),
            #[cfg(feature = "tmux")]
            Native::Tmux(client) => Ok(ProcessManager::new(client.clone())),
            #[allow(unreachable_patterns)]
            _ => Err(Error::unsupported(self.backend(), "open_processes")),
        }
    }
}

// Native configuration structs also work directly with the core open methods.
macro_rules! config_source {
    ($feature:literal, $config:path, $variant:ident) => {
        #[cfg(feature = $feature)]
        #[async_trait]
        impl opensol_core::BackendConfig for $config {
            fn validate_open(&self, api: Api, options: &OpenOptions) -> Result<()> {
                opensol_core::BackendConfig::validate_open(
                    &BackendConfig::$variant(self.clone()),
                    api,
                    options,
                )
            }
            async fn open(&self, connection: Option<Connection>) -> Result<Backend> {
                opensol_core::BackendConfig::open(
                    &BackendConfig::$variant(self.clone()),
                    connection,
                )
                .await
            }
        }
    };
}
config_source!("local", crate::local::LocalConfig, Local);
config_source!("ssh", crate::ssh::SshConfig, Ssh);
config_source!("docker", crate::docker::DockerConfig, Docker);
config_source!("e2b", crate::e2b::E2bConfig, E2b);
config_source!("envd", crate::envd::EnvdConfig, Envd);
config_source!(
    "agent-sandbox",
    crate::agent_sandbox::AgentSandboxConfig,
    AgentSandbox
);
config_source!("openshell", crate::openshell::OpenShellConfig, OpenShell);
config_source!("sandboxd", crate::sandboxd::SandboxdConfig, Sandboxd);
config_source!("pueue", crate::pueue::PueueConfig, Pueue);
config_source!("tmux", crate::tmux::TmuxConfig, Tmux);
