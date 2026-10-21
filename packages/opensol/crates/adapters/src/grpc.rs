//! Explicit endpoint, verified TLS, and optional bearer authentication for native gRPC adapters.

use std::time::Duration;

use opensol_core::{Error, ErrorKind, Result};
use tonic::{
    Request,
    metadata::AsciiMetadataValue,
    transport::{Certificate, Channel, ClientTlsConfig, Endpoint, Identity},
};

use crate::secret::Secret;

/// Native gRPC configuration. TLS verifies peers; optional CA and mTLS identity never disable verification.
///
/// CLI login state, proxy tunnels and gateway discovery are deliberately not read automatically.
#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub struct GrpcConfig {
    pub endpoint: String,
    pub bearer_token: Option<Secret>,
    pub ca_certificate: Option<Secret>,
    pub client_identity: Option<(Secret, Secret)>,
    pub request_timeout: Duration,
}

impl GrpcConfig {
    pub fn new(endpoint: impl Into<String>) -> Self {
        Self {
            endpoint: endpoint.into(),
            bearer_token: None,
            ca_certificate: None,
            client_identity: None,
            request_timeout: Duration::from_secs(30),
        }
    }

    pub(crate) async fn connect(&self, backend: &str) -> Result<Channel> {
        self.request((), backend)?;
        if self.request_timeout.is_zero()
            || !(self.endpoint.starts_with("http://") || self.endpoint.starts_with("https://"))
            || self.endpoint.contains(['@', '?', '#'])
        {
            return Err(Error::invalid(
                backend,
                "configure",
                "invalid endpoint or request timeout",
            ));
        }
        let mut endpoint = Endpoint::from_shared(self.endpoint.clone())
            .map_err(|_| Error::invalid(backend, "configure", "invalid gRPC endpoint"))?;
        if self.endpoint.starts_with("https://") {
            let mut tls = ClientTlsConfig::new().with_webpki_roots();
            if let Some(certificate) = &self.ca_certificate {
                tls = tls.ca_certificate(Certificate::from_pem(certificate.expose()));
            }
            if let Some((certificate, key)) = &self.client_identity {
                tls = tls.identity(Identity::from_pem(certificate.expose(), key.expose()));
            }
            endpoint = endpoint
                .tls_config(tls)
                .map_err(|_| Error::invalid(backend, "configure", "invalid TLS configuration"))?;
        } else if self.ca_certificate.is_some() || self.client_identity.is_some() {
            return Err(Error::invalid(
                backend,
                "configure",
                "TLS credentials require an HTTPS endpoint",
            ));
        }
        tokio::time::timeout(self.request_timeout, endpoint.connect())
            .await
            .map_err(|_| {
                Error::new(
                    ErrorKind::Timeout,
                    backend,
                    "connect",
                    "gRPC connection deadline exceeded",
                )
            })?
            .map_err(|_| {
                Error::new(
                    ErrorKind::Transport,
                    backend,
                    "connect",
                    "gRPC connection failed",
                )
            })
    }

    pub(crate) fn request<Message>(
        &self,
        message: Message,
        backend: &str,
    ) -> Result<Request<Message>> {
        let mut request = Request::new(message);
        request.set_timeout(self.request_timeout);
        if let Some(token) = &self.bearer_token {
            let mut value = AsciiMetadataValue::try_from(format!("Bearer {}", token.expose()))
                .map_err(|_| Error::invalid(backend, "configure", "invalid bearer token"))?;
            value.set_sensitive(true);
            request.metadata_mut().insert("authorization", value);
        }
        Ok(request)
    }
}

impl std::fmt::Debug for GrpcConfig {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("GrpcConfig")
            .field("endpoint", &"[configured]")
            .field("credentials", &"[redacted]")
            .field("request_timeout", &self.request_timeout)
            .finish()
    }
}

pub(crate) fn status(error: tonic::Status, backend: &str, operation: &str) -> Error {
    let mut source = std::error::Error::source(&error);
    while let Some(cause) = source {
        if cause.is::<tonic::TimeoutExpired>() {
            return Error::new(
                ErrorKind::Timeout,
                backend,
                operation,
                "gRPC deadline exceeded",
            );
        }
        source = cause.source();
    }
    let kind = match error.code() {
        tonic::Code::InvalidArgument | tonic::Code::OutOfRange => ErrorKind::InvalidArgument,
        tonic::Code::Unimplemented => ErrorKind::Unsupported,
        tonic::Code::NotFound => ErrorKind::NotFound,
        tonic::Code::AlreadyExists
        | tonic::Code::Aborted
        | tonic::Code::FailedPrecondition
        | tonic::Code::ResourceExhausted => ErrorKind::Conflict,
        tonic::Code::PermissionDenied | tonic::Code::Unauthenticated => ErrorKind::Permission,
        tonic::Code::DeadlineExceeded => ErrorKind::Timeout,
        tonic::Code::Unavailable | tonic::Code::Cancelled => ErrorKind::Transport,
        _ => ErrorKind::Backend,
    };
    Error::new(
        kind,
        backend,
        operation,
        &format!("gRPC status {:?}", error.code()),
    )
}
