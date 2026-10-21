//! Credential wrapper shared by native clients. Debug never exposes the contained value.

/// Explicit credential. Debug is redacted; serializing a configuration deliberately includes it.
/// Persisted configurations require the same protection as the original credentials.
#[derive(Clone, serde::Serialize, serde::Deserialize)]
#[serde(transparent)]
pub struct Secret(String);

impl Secret {
    pub fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for Secret {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Secret([redacted])")
    }
}
