//! Provider-neutral construction contracts. Configurations and dispatch live in adapters.
//!
//! A Backend owns access resources, never remote lifetimes. Child clients retain it; the
//! backend must not cache those public clients. Opening validates bindings before target I/O.

use std::{any::Any, sync::Arc};

use async_trait::async_trait;

use crate::{Connection, CreateOptions, Error, ProcessManager, Result, SandboxManager, SandboxRef};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Api {
    Sandboxes,
    Connection,
    Processes,
}

/// Binding arguments, not persistent configuration. Dependencies cannot replace an open Backend's.
#[derive(Clone, Default)]
pub struct OpenOptions {
    pub target: Option<SandboxRef>,
    pub connection: Option<Connection>,
}

/// Complete per-create request with portable metadata and an adapter-owned typed payload.
/// The core never interprets provider schemas. Adapters downcast before sending requests.
#[derive(Clone)]
pub struct SandboxSpec {
    pub options: CreateOptions,
    backend: &'static str,
    payload: Arc<dyn Any + Send + Sync>,
}

impl SandboxSpec {
    pub fn new<T: Send + Sync + 'static>(
        backend: &'static str,
        options: CreateOptions,
        payload: T,
    ) -> Self {
        Self {
            options,
            backend,
            payload: Arc::new(payload),
        }
    }

    pub fn payload<T: 'static>(&self, backend: &str) -> Result<&T> {
        if self.backend == backend
            && let Some(value) = self.payload.downcast_ref()
        {
            return Ok(value);
        }
        Err(Error::invalid(
            backend,
            "create",
            "wrong sandbox specification type",
        ))
    }
}

/// Shareable access context. Clones preserve native client identity, without global caching.
#[derive(Clone)]
pub struct Backend {
    access: Arc<dyn BackendAccess>,
}

impl Backend {
    pub fn new(access: impl BackendAccess + 'static) -> Self {
        Self {
            access: Arc::new(access),
        }
    }

    pub async fn open(config: impl BackendConfig, connection: Option<Connection>) -> Result<Self> {
        config.open(connection).await
    }

    pub fn backend(&self) -> &'static str {
        self.access.backend()
    }

    pub(crate) fn sandboxes(&self) -> Result<SandboxManager> {
        let mut client = self.access.sandboxes()?;
        client.owner = Some(self.clone());
        Ok(client)
    }

    pub(crate) async fn connection(&self, target: Option<&SandboxRef>) -> Result<Connection> {
        let mut client = self.access.connection(target).await?;
        client.owner = Some(self.clone());
        Ok(client)
    }

    pub(crate) async fn processes(&self, target: Option<&SandboxRef>) -> Result<ProcessManager> {
        let mut client = self.access.processes(target).await?;
        client.owner = Some(self.clone());
        Ok(client)
    }
}

/// Adapter configuration extension. Implementations validate options before opening transports.
#[async_trait]
pub trait BackendConfig: Send + Sync {
    fn validate_open(&self, api: Api, options: &OpenOptions) -> Result<()>;
    async fn open(&self, connection: Option<Connection>) -> Result<Backend>;
}

/// Adapter runtime extension; default methods report unsupported business APIs without probing.
#[async_trait]
pub trait BackendAccess: Send + Sync {
    fn backend(&self) -> &'static str;
    fn validate_open(&self, api: Api, target: Option<&SandboxRef>) -> Result<()>;
    fn sandboxes(&self) -> Result<SandboxManager> {
        Err(Error::unsupported(self.backend(), "open_sandboxes"))
    }
    async fn connection(&self, _target: Option<&SandboxRef>) -> Result<Connection> {
        Err(Error::unsupported(self.backend(), "open_connection"))
    }
    async fn processes(&self, _target: Option<&SandboxRef>) -> Result<ProcessManager> {
        Err(Error::unsupported(self.backend(), "open_processes"))
    }
}

/// Either configuration or an existing context. Resolving a context never opens another client.
#[async_trait]
pub trait BackendSource: Send {
    async fn resolve(self, api: Api, options: OpenOptions) -> Result<Backend>;
}

#[async_trait]
impl<T: BackendConfig> BackendSource for T {
    async fn resolve(self, api: Api, options: OpenOptions) -> Result<Backend> {
        self.validate_open(api, &options)?;
        self.open(options.connection).await
    }
}

#[async_trait]
impl BackendSource for Backend {
    async fn resolve(self, api: Api, options: OpenOptions) -> Result<Backend> {
        if options.connection.is_some() {
            return Err(Error::invalid(
                self.backend(),
                "open",
                "cannot override an open Backend's connection",
            ));
        }
        self.access.validate_open(api, options.target.as_ref())?;
        Ok(self)
    }
}

#[async_trait]
impl BackendSource for &Backend {
    async fn resolve(self, api: Api, options: OpenOptions) -> Result<Backend> {
        self.clone().resolve(api, options).await
    }
}
