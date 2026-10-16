//! Credential-safe HTTP boundary shared by E2B control plane and envd ConnectRPC.

use agent_sandbox_core::{Error, ErrorKind, Result};
use reqwest::{Client, Response, Url, header::HeaderValue};

pub(crate) fn client(backend: &str) -> Result<Client> {
    Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
        .connect_timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|_| Error::invalid(backend, "configure", "HTTP client configuration failed"))
}

pub(crate) fn endpoint(value: &str, backend: &str) -> Result<Url> {
    let url = Url::parse(value)
        .map_err(|_| Error::invalid(backend, "configure", "invalid HTTP endpoint"))?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(Error::invalid(
            backend,
            "configure",
            "endpoint must be HTTP(S), without credentials, query or fragment",
        ));
    }
    Ok(url)
}

pub(crate) fn url(base: &Url, segments: &[&str]) -> Url {
    let mut url = base.clone();
    url.path_segments_mut()
        .expect("validated HTTP URL")
        .pop_if_empty()
        .extend(segments.iter().copied());
    url
}

pub(crate) fn secret_header(value: &str, backend: &str) -> Result<HeaderValue> {
    let mut value = HeaderValue::from_str(value)
        .map_err(|_| Error::invalid(backend, "configure", "invalid authentication header"))?;
    value.set_sensitive(true);
    Ok(value)
}

pub(crate) fn transport(error: reqwest::Error, backend: &str, operation: &str) -> Error {
    Error::new(
        if error.is_timeout() {
            ErrorKind::Timeout
        } else {
            ErrorKind::Transport
        },
        backend,
        operation,
        "HTTP request failed; remote completion may be unknown",
    )
}

pub(crate) fn status_kind(status: u16) -> ErrorKind {
    match status {
        400 | 422 => ErrorKind::InvalidArgument,
        401 | 403 => ErrorKind::Permission,
        404 => ErrorKind::NotFound,
        409 | 429 => ErrorKind::Conflict,
        408 | 504 => ErrorKind::Timeout,
        501 => ErrorKind::Unsupported,
        _ => ErrorKind::Backend,
    }
}

pub(crate) fn check(response: Response, backend: &str, operation: &str) -> Result<Response> {
    if response.status().is_success() {
        return Ok(response);
    }
    Err(Error::new(
        status_kind(response.status().as_u16()),
        backend,
        operation,
        &format!("HTTP status {}", response.status().as_u16()),
    ))
}
