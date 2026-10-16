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
    ProcessRef, Result, raw::ExecAccess,
};

#[derive(Clone, Default)]
struct UnavailableConnection(Arc<AtomicUsize>);

#[async_trait]
impl ExecAccess for UnavailableConnection {
    fn backend(&self) -> &'static str {
        "unavailable"
    }
    fn capabilities(&self) -> ConnectionCapabilities {
        ConnectionCapabilities {
            stdin: true,
            timeout: true,
            separate_stderr: true,
        }
    }
    async fn exec(&self, _request: ExecRequest) -> Result<ExecResult> {
        self.0.fetch_add(1, Ordering::SeqCst);
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
        for data in [vec![0xff], b"text\0input".to_vec(), vec![0, 0xff]] {
            let error = process.send_input(data).await.unwrap_err();
            assert_eq!(error.kind, ErrorKind::Unsupported);
            assert_eq!(
                (error.backend.as_str(), error.operation.as_str()),
                ("pueue", "send_input")
            );
        }
    }
    assert_eq!(connection.0.load(Ordering::SeqCst), 0);
    let error = manager
        .send_input(&ProcessRef::new("pueue", "1", None), b"valid text".to_vec())
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::Transport);
    assert_eq!(connection.0.load(Ordering::SeqCst), 1);
}
