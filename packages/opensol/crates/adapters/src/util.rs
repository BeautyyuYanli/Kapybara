//! Shared deadline, native argv construction and safe POSIX shell rendering at adapter boundaries.

use std::{future::Future, time::Duration};

use opensol_core::{Error, ErrorKind, Result};

pub(crate) async fn deadline<Value>(
    timeout: Option<Duration>,
    backend: &str,
    operation: &str,
    future: impl Future<Output = Result<Value>>,
) -> Result<Value> {
    match timeout {
        Some(timeout) => tokio::time::timeout(timeout, future).await.map_err(|_| {
            Error::new(
                ErrorKind::Timeout,
                backend,
                operation,
                "deadline exceeded; remote completion may be unknown",
            )
        })?,
        None => future.await,
    }
}

#[cfg(any(feature = "ssh", feature = "pueue"))]
pub(crate) fn quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

#[cfg(any(feature = "ssh", feature = "pueue", feature = "tmux"))]
/// Builds validated argv/env without letting `env` reinterpret an executable containing `=`.
/// The fixed native helper preserves environment keys that an extra shell would discard;
/// `-n 0` leaves priority unchanged and its absolute path is independent of the task's PATH.
/// Working-directory handling belongs to the calling adapter, before this environment overlay.
pub(crate) fn command_argv(command: &opensol_core::Command) -> Vec<String> {
    let mut argv = vec!["env".into(), "--".into()];
    argv.extend(
        command
            .env
            .iter()
            .map(|(key, value)| format!("{key}={value}")),
    );
    argv.extend(["/usr/bin/nice".into(), "-n".into(), "0".into(), "--".into()]);
    argv.extend(command.argv.iter().cloned());
    argv
}

#[cfg(any(feature = "ssh", feature = "pueue"))]
/// POSIX shell rendering of the shared native argv; callers handle cwd separately.
pub(crate) fn shell_command(command: &opensol_core::Command) -> String {
    command_argv(command)
        .iter()
        .map(|value| quote(value))
        .collect::<Vec<_>>()
        .join(" ")
}
