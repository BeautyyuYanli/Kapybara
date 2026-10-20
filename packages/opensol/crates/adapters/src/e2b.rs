//! E2B control-plane HTTP lifecycle and explicit composition with the native envd client.
//!
//! Stop takes a full-memory pause, start uses v2 connect/resume, and fork uses the native fork API.
//! Inspect and envd connection discovery do not resume paused sandboxes. No side effect is retried.

use std::{
    collections::{BTreeMap, HashSet},
    time::Duration,
};

use async_trait::async_trait;
use opensol_core::{
    CreateOptions, Error, ErrorKind, Result, SandboxInfo, SandboxManager,
    SandboxManagerCapabilities, SandboxRef, SandboxState, raw::SandboxManagerAccess,
};
use reqwest::{Client, Method, Url, header::HeaderValue};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::{
    envd::{Envd, EnvdConfig},
    http,
    secret::Secret,
};

/// E2B-specific creation options. Resource names map to metadata["name"], not a server identity.
#[derive(Clone)]
pub struct E2bConfig {
    pub api_endpoint: String,
    pub api_key: Secret,
    pub template: String,
    pub sandbox_timeout: u32,
    pub auto_pause: bool,
    pub metadata: BTreeMap<String, String>,
    pub env: BTreeMap<String, String>,
    pub request_timeout: Duration,
    pub sandbox_domain: String,
    pub envd_endpoint: Option<String>,
}

impl E2bConfig {
    pub fn new(api_key: Secret, template: impl Into<String>) -> Self {
        Self {
            api_endpoint: "https://api.e2b.app".into(),
            api_key,
            template: template.into(),
            sandbox_timeout: 300,
            auto_pause: false,
            metadata: BTreeMap::new(),
            env: BTreeMap::new(),
            request_timeout: Duration::from_secs(30),
            sandbox_domain: "e2b.app".into(),
            envd_endpoint: None,
        }
    }
}

impl std::fmt::Debug for E2bConfig {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("E2bConfig")
            .field("template", &self.template)
            .field("api_key", &self.api_key)
            .field("env", &"[redacted]")
            .field("endpoints", &"[configured]")
            .finish()
    }
}

/// A provider client, separate from the envd connection/process-manager object for a sandbox.
#[derive(Clone)]
pub struct E2bSandboxManager {
    client: Client,
    endpoint: Url,
    key: HeaderValue,
    config: E2bConfig,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Sandbox {
    #[serde(rename = "sandboxID")]
    sandbox_id: String,
    #[serde(default)]
    state: Option<String>,
    #[serde(default)]
    domain: Option<String>,
    #[serde(default)]
    envd_access_token: Option<String>,
}

impl E2bSandboxManager {
    pub fn sandboxes(&self) -> SandboxManager {
        SandboxManager::new(self.clone())
    }

    pub fn new(config: E2bConfig) -> Result<Self> {
        if config.template.is_empty()
            || config.sandbox_timeout == 0
            || config.sandbox_timeout > i32::MAX as u32
            || config.request_timeout.is_zero()
        {
            return Err(Error::invalid(
                "e2b",
                "configure",
                "template and positive timeouts are required",
            ));
        }
        if let Some(endpoint) = &config.envd_endpoint {
            http::endpoint(endpoint, "e2b")?;
        }
        Ok(Self {
            client: http::client("e2b")?,
            endpoint: http::endpoint(&config.api_endpoint, "e2b")?,
            key: http::secret_header(config.api_key.expose(), "e2b")?,
            config,
        })
    }

    async fn request(
        &self,
        method: Method,
        path: &[&str],
        body: Option<Value>,
        operation: &str,
    ) -> Result<Option<Value>> {
        let mut request = self
            .client
            .request(method, http::url(&self.endpoint, path))
            .header("x-api-key", self.key.clone())
            .timeout(self.config.request_timeout);
        if let Some(body) = body {
            request = request.json(&body);
        }
        let response = http::check(
            request
                .send()
                .await
                .map_err(|error| http::transport(error, "e2b", operation))?,
            "e2b",
            operation,
        )?;
        if response.status() == reqwest::StatusCode::NO_CONTENT {
            return Ok(None);
        }
        let body = response
            .bytes()
            .await
            .map_err(|error| http::transport(error, "e2b", operation))?;
        let value = serde_json::from_slice(&body).map_err(|_| {
            Error::new(
                ErrorKind::Backend,
                "e2b",
                operation,
                "invalid E2B JSON response",
            )
        })?;
        Ok(Some(value))
    }

    fn sandbox(&self, value: Option<Value>, operation: &str) -> Result<Sandbox> {
        let sandbox: Sandbox =
            serde_json::from_value(value.ok_or_else(|| response_error(operation))?)
                .map_err(|_| response_error(operation))?;
        if sandbox.sandbox_id.is_empty() {
            return Err(response_error(operation));
        }
        Ok(sandbox)
    }

    /// Discovers credentials using read-only inspect, then binds both layer 2 and layer 3 to envd.
    pub async fn envd(&self, resource: &SandboxRef) -> Result<Envd> {
        resource.validate("e2b", "envd")?;
        let sandbox = self.sandbox(
            self.request(Method::GET, &["sandboxes", &resource.id], None, "envd")
                .await?,
            "envd",
        )?;
        if sandbox.sandbox_id != resource.id {
            return Err(response_error("envd"));
        }
        let domain = sandbox
            .domain
            .as_deref()
            .unwrap_or(&self.config.sandbox_domain);
        let endpoint = self
            .config
            .envd_endpoint
            .clone()
            .unwrap_or_else(|| format!("https://49983-{}.{}", sandbox.sandbox_id, domain));
        let mut config = EnvdConfig::new(endpoint);
        config.sandbox_id = Some(sandbox.sandbox_id);
        config.access_token = sandbox.envd_access_token.map(Secret::new);
        config.request_timeout = self.config.request_timeout;
        Envd::with_client(config, self.client.clone())
    }
}

fn response_error(operation: &str) -> Error {
    Error::new(
        ErrorKind::Backend,
        "e2b",
        operation,
        "unexpected E2B response",
    )
}

fn sandbox_info(sandbox: Sandbox) -> SandboxInfo {
    let native_state = sandbox.state.unwrap_or_else(|| "unknown".into());
    let state = match native_state.as_str() {
        "running" => SandboxState::Running,
        "paused" => SandboxState::Stopped,
        _ => SandboxState::Unknown,
    };
    SandboxInfo {
        sandbox: SandboxRef::new("e2b", sandbox.sandbox_id),
        state,
        native_state,
    }
}

#[async_trait]
impl SandboxManagerAccess for E2bSandboxManager {
    fn backend(&self) -> &'static str {
        "e2b"
    }
    fn capabilities(&self) -> SandboxManagerCapabilities {
        SandboxManagerCapabilities {
            fork: true,
            stop_preserves_memory: true,
            ..SandboxManagerCapabilities::BASIC
        }
    }

    async fn list(&self) -> Result<Vec<SandboxInfo>> {
        let mut sandboxes = Vec::new();
        let mut next_token = None;
        let mut tokens = HashSet::new();
        loop {
            let mut request = self
                .client
                .get(http::url(&self.endpoint, &["v2", "sandboxes"]))
                .header("x-api-key", self.key.clone())
                .timeout(self.config.request_timeout)
                .query(&[("state", "running,paused"), ("limit", "100")]);
            if let Some(token) = &next_token {
                request = request.query(&[("nextToken", token)]);
            }
            let response = http::check(
                request
                    .send()
                    .await
                    .map_err(|error| http::transport(error, "e2b", "list"))?,
                "e2b",
                "list",
            )?;
            next_token = response
                .headers()
                .get("x-next-token")
                .map(|token| {
                    token
                        .to_str()
                        .map(str::to_owned)
                        .map_err(|_| response_error("list"))
                })
                .transpose()?
                .filter(|token| !token.is_empty());
            let bytes = response
                .bytes()
                .await
                .map_err(|error| http::transport(error, "e2b", "list"))?;
            let page: Vec<Sandbox> =
                serde_json::from_slice(&bytes).map_err(|_| response_error("list"))?;
            for sandbox in page {
                if sandbox.sandbox_id.is_empty() {
                    return Err(response_error("list"));
                }
                sandboxes.push(sandbox_info(sandbox));
            }
            let Some(token) = &next_token else {
                break;
            };
            if !tokens.insert(token.clone()) {
                return Err(response_error("list"));
            }
        }
        Ok(sandboxes)
    }

    async fn create(&self, options: CreateOptions) -> Result<SandboxRef> {
        let mut metadata = self.config.metadata.clone();
        metadata.extend(options.labels);
        if let Some(name) = options.name {
            metadata.insert("name".into(), name);
        }
        let body = json!({ "templateID": self.config.template, "timeout": self.config.sandbox_timeout,
            "autoPause": self.config.auto_pause, "autoPauseMemory": true, "metadata": metadata, "envVars": self.config.env });
        let sandbox = self.sandbox(
            self.request(Method::POST, &["v2", "sandboxes"], Some(body), "create")
                .await?,
            "create",
        )?;
        Ok(SandboxRef::new("e2b", sandbox.sandbox_id))
    }

    async fn inspect(&self, resource: &SandboxRef) -> Result<SandboxInfo> {
        let sandbox = self.sandbox(
            self.request(Method::GET, &["sandboxes", &resource.id], None, "inspect")
                .await?,
            "inspect",
        )?;
        if sandbox.sandbox_id != resource.id {
            return Err(response_error("inspect"));
        }
        Ok(sandbox_info(sandbox))
    }

    async fn start(&self, resource: &SandboxRef) -> Result<()> {
        self.sandbox(
            self.request(
                Method::POST,
                &["v2", "sandboxes", &resource.id, "connect"],
                Some(json!({ "timeout": self.config.sandbox_timeout, "memory": true })),
                "start",
            )
            .await?,
            "start",
        )?;
        Ok(())
    }

    async fn stop(&self, resource: &SandboxRef) -> Result<()> {
        self.request(
            Method::POST,
            &["sandboxes", &resource.id, "pause"],
            Some(json!({ "memory": true })),
            "stop",
        )
        .await?;
        Ok(())
    }

    async fn delete(&self, resource: &SandboxRef) -> Result<()> {
        self.request(Method::DELETE, &["sandboxes", &resource.id], None, "delete")
            .await?;
        Ok(())
    }

    async fn fork(&self, resource: &SandboxRef) -> Result<SandboxRef> {
        let value = self
            .request(
                Method::POST,
                &["sandboxes", &resource.id, "fork"],
                Some(json!({ "count": 1, "timeout": self.config.sandbox_timeout })),
                "fork",
            )
            .await?
            .ok_or_else(|| response_error("fork"))?;
        let results = value
            .as_array()
            .filter(|items| items.len() == 1)
            .ok_or_else(|| response_error("fork"))?;
        let result = &results[0];
        if result.get("error").is_some_and(|error| !error.is_null()) {
            return Err(Error::new(
                ErrorKind::Backend,
                "e2b",
                "fork",
                "E2B reported a per-fork failure (HTTP success does not imply fork success)",
            ));
        }
        let sandbox = self.sandbox(result.get("sandbox").cloned(), "fork")?;
        Ok(SandboxRef::new("e2b", sandbox.sandbox_id))
    }
}
