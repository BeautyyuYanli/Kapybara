//! Adapter extension contracts. Wrap implementations in the public clients before business use.
//!
//! Clients validate portable arguments, identities and declared capabilities before dispatch.
//! Adapters map native restrictions, permissions, state and protocol errors, without retrying
//! uncertain side effects. The shared DTOs retain the same meaning on both sides of this boundary.

use async_trait::async_trait;
use std::time::Duration;

use crate::{
    ConnectionCapabilities, CreateOptions, Error, EventStream, ExecRequest, ExecResult,
    LifecycleCapabilities, ManagerInfo, OutputCursor, OutputInfo, OutputPage, ProcessCapabilities,
    ProcessHandle, ProcessInfo, ProcessOutcome, ProcessRef, PtySize, ResourceInfo, ResourceRef,
    Result, Signal, SpawnRequest,
};

/// Native lifecycle; provider-specific connection discovery stays on the backend object.
#[async_trait]
pub trait LifecycleAccess: Send + Sync {
    fn backend(&self) -> &'static str;
    fn capabilities(&self) -> LifecycleCapabilities;
    async fn create(&self, options: CreateOptions) -> Result<ResourceRef>;
    async fn inspect(&self, resource: &ResourceRef) -> Result<ResourceInfo>;
    async fn start(&self, resource: &ResourceRef) -> Result<()>;
    async fn stop(&self, resource: &ResourceRef) -> Result<()>;
    async fn delete(&self, resource: &ResourceRef) -> Result<()>;
    async fn fork(&self, _resource: &ResourceRef) -> Result<ResourceRef> {
        Err(Error::unsupported(self.backend(), "fork"))
    }
}

/// Completed commands on a fixed target. A nonzero exit is a result, not an SDK error.
#[async_trait]
pub trait ExecAccess: Send + Sync {
    fn backend(&self) -> &'static str;
    fn capabilities(&self) -> ConnectionCapabilities;
    async fn exec(&self, request: ExecRequest) -> Result<ExecResult>;
}

/// Native tasks and output subscriptions; dropping either client or stream never kills a task.
#[async_trait]
pub trait ProcessAccess: Send + Sync {
    fn backend(&self) -> &'static str;
    fn capabilities(&self) -> ProcessCapabilities;
    async fn probe(&self) -> Result<ManagerInfo> {
        Err(Error::unsupported(self.backend(), "probe"))
    }
    async fn list(&self) -> Result<Vec<ProcessInfo>> {
        Err(Error::unsupported(self.backend(), "list"))
    }
    async fn spawn(&self, request: SpawnRequest) -> Result<ProcessHandle>;
    async fn status(&self, process: &ProcessRef) -> Result<ProcessInfo>;
    /// Return a confirmed terminal outcome, even when the native task has no OS exit status.
    async fn wait(&self, process: &ProcessRef, timeout: Option<Duration>)
    -> Result<ProcessOutcome>;
    async fn output(&self, process: &ProcessRef) -> Result<EventStream>;
    /// One rendered terminal screen, not raw output, scrollback, or a cursor-based log.
    async fn terminal_snapshot(&self, _process: &ProcessRef) -> Result<Vec<u8>> {
        Err(Error::unsupported(self.backend(), "terminal_snapshot"))
    }
    async fn output_stat(&self, _process: &ProcessRef) -> Result<OutputInfo> {
        Err(Error::unsupported(self.backend(), "output_stat"))
    }
    /// Cursor encoding, log versions and retention are native to the adapter.
    async fn read_output(
        &self,
        _process: &ProcessRef,
        _cursor: Option<&OutputCursor>,
        _limit: usize,
    ) -> Result<OutputPage> {
        Err(Error::unsupported(self.backend(), "read_output"))
    }
    async fn pause(&self, _process: &ProcessRef) -> Result<()> {
        Err(Error::unsupported(self.backend(), "pause"))
    }
    async fn start(&self, _process: &ProcessRef) -> Result<()> {
        Err(Error::unsupported(self.backend(), "start"))
    }
    async fn restart(&self, _process: &ProcessRef) -> Result<()> {
        Err(Error::unsupported(self.backend(), "restart"))
    }
    async fn kill(&self, _process: &ProcessRef) -> Result<()> {
        Err(Error::unsupported(self.backend(), "kill"))
    }
    async fn remove(&self, _process: &ProcessRef) -> Result<()> {
        Err(Error::unsupported(self.backend(), "remove"))
    }
    async fn send_input(&self, _process: &ProcessRef, _data: Vec<u8>) -> Result<()> {
        Err(Error::unsupported(self.backend(), "send_input"))
    }
    async fn close_stdin(&self, _process: &ProcessRef) -> Result<()> {
        Err(Error::unsupported(self.backend(), "close_stdin"))
    }
    async fn signal(&self, _process: &ProcessRef, _signal: Signal) -> Result<()> {
        Err(Error::unsupported(self.backend(), "signal"))
    }
    async fn resize(&self, _process: &ProcessRef, _size: PtySize) -> Result<()> {
        Err(Error::unsupported(self.backend(), "resize"))
    }
    async fn reconnect(&self, _process: &ProcessRef) -> Result<ProcessHandle> {
        Err(Error::unsupported(self.backend(), "reconnect"))
    }
}
