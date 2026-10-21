//! Pueue's CLI payload restrictions are checked before entering the injected Connection.

#![cfg(feature = "pueue")]

use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

use async_trait::async_trait;
use opensol_adapters::pueue::{Pueue, PueueConfig};
use opensol_core::{
    Connection, ConnectionCapabilities, Error, ErrorKind, ExecRequest, ExecResult, ProcessManager,
    ProcessRef, Result, raw::ConnectionAccess,
};

#[derive(Clone)]
struct UnavailableConnection {
    calls: Arc<AtomicUsize>,
    capabilities: ConnectionCapabilities,
}

impl Default for UnavailableConnection {
    fn default() -> Self {
        Self {
            calls: Arc::new(AtomicUsize::new(0)),
            capabilities: ConnectionCapabilities {
                stdin: None,
                timeout: true,
                separate_stderr: true,
                cancellation: opensol_core::ExecCancellation::NoTargetTerminationGuarantee,
            },
        }
    }
}

#[async_trait]
impl ConnectionAccess for UnavailableConnection {
    fn backend(&self) -> &'static str {
        "unavailable"
    }
    fn capabilities(&self) -> ConnectionCapabilities {
        self.capabilities
    }
    async fn exec(&self, _request: ExecRequest) -> Result<ExecResult> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        Err(Error::new(
            ErrorKind::Transport,
            self.backend(),
            "exec",
            "connection unavailable",
        ))
    }
}

#[tokio::test]
async fn invalid_text_and_nul_are_unsupported_before_connection_io() {
    let connection = UnavailableConnection::default();
    let manager = ProcessManager::new(
        Pueue::new(Connection::new(connection.clone()), PueueConfig::default()).unwrap(),
    );
    for pty in [None, Some(false)] {
        let process = manager.get(ProcessRef::new("pueue", "1", pty));
        let input = process.capabilities().input.unwrap();
        assert_eq!(
            input.constraints.content,
            opensol_core::InputContent::Utf8WithoutNul
        );
        assert!(!input.close);
        for data in [vec![0xff], b"text\0input".to_vec(), vec![0, 0xff]] {
            let error = process.send_input(data).await.unwrap_err();
            assert_eq!(error.kind, ErrorKind::Unsupported);
            assert_eq!(
                (error.backend.as_str(), error.operation.as_str()),
                ("pueue", "send_input")
            );
        }
    }
    assert_eq!(connection.calls.load(Ordering::SeqCst), 0);
    let error = manager
        .get(ProcessRef::new("pueue", "1", None))
        .send_input(b"valid text".to_vec())
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::Transport);
    assert_eq!(connection.calls.load(Ordering::SeqCst), 1);
}

#[test]
fn required_connection_contracts_are_checked_without_io() {
    for (timeout, separate_stderr) in [(false, true), (true, false)] {
        let mut connection = UnavailableConnection::default();
        connection.capabilities.timeout = timeout;
        connection.capabilities.separate_stderr = separate_stderr;
        assert!(
            matches!(Pueue::new(Connection::new(connection.clone()), PueueConfig::default()), Err(error) if error.kind == ErrorKind::Unsupported)
        );
        #[cfg(feature = "tmux")]
        assert!(
            matches!(opensol_adapters::tmux::Tmux::new(Connection::new(connection.clone()), opensol_adapters::tmux::TmuxConfig::new("/tmp/unopened")), Err(error) if error.kind == ErrorKind::Unsupported)
        );
        assert_eq!(connection.calls.load(Ordering::SeqCst), 0);
    }
}
