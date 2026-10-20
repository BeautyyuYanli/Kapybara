//! Adapter extension contracts. Wrap implementations in the public clients before business use.
//!
//! Clients validate portable arguments, identities and declared capabilities before dispatch.
//! Adapters map native restrictions, permissions, state and protocol errors, without retrying
//! uncertain side effects. The shared DTOs retain the same meaning on both sides of this boundary.

use async_trait::async_trait;
use std::{sync::Arc, time::Duration};

use crate::{
    ConnectionCapabilities, CreateOptions, Error, ExecRequest, ExecResult, ManagerInfo,
    OutputCapabilities, OutputCursor, OutputPage, OutputSubscription, ProcessInfo,
    ProcessManagerCapabilities, ProcessOutcome, ProcessRef, PtySize, ReadDirection, Result,
    SandboxInfo, SandboxManagerCapabilities, SandboxRef, Signal, SpawnRequest,
    TerminalCapabilities, TerminalCursor, TerminalPage, TerminalScreen,
};

/// Native lifecycle; provider-specific connection discovery stays on the backend object.
/// Success confirms the native request, not readiness, reconciliation or resource disappearance.
#[async_trait]
pub trait SandboxManagerAccess: Send + Sync {
    fn backend(&self) -> &'static str;
    fn capabilities(&self) -> SandboxManagerCapabilities;
    async fn list(&self) -> Result<Vec<SandboxInfo>> {
        Err(Error::unsupported(self.backend(), "list"))
    }
    async fn create(&self, options: CreateOptions) -> Result<SandboxRef>;
    async fn inspect(&self, resource: &SandboxRef) -> Result<SandboxInfo>;
    async fn start(&self, resource: &SandboxRef) -> Result<()>;
    async fn stop(&self, resource: &SandboxRef) -> Result<()>;
    async fn delete(&self, resource: &SandboxRef) -> Result<()>;
    async fn fork(&self, _resource: &SandboxRef) -> Result<SandboxRef> {
        Err(Error::unsupported(self.backend(), "fork"))
    }
}

/// Completed commands on a fixed target. A nonzero exit is a result, not an SDK error.
#[async_trait]
pub trait ConnectionAccess: Send + Sync {
    fn backend(&self) -> &'static str;
    fn capabilities(&self) -> ConnectionCapabilities;
    async fn exec(&self, request: ExecRequest) -> Result<ExecResult>;
}

/// Raw bytes and native subscriptions. Optional operations never synthesize retained output.
#[async_trait]
pub trait OutputAccess: Send + Sync {
    fn backend(&self) -> &'static str;
    fn capabilities(&self) -> OutputCapabilities;
    async fn read(
        &self,
        _process: &ProcessRef,
        _cursor: Option<&OutputCursor>,
        _direction: ReadDirection,
        _max_bytes: usize,
    ) -> Result<OutputPage> {
        Err(Error::unsupported(self.backend(), "read_output"))
    }
    async fn tail(&self, _process: &ProcessRef, _max_bytes: usize) -> Result<OutputPage> {
        Err(Error::unsupported(self.backend(), "tail_output"))
    }
    async fn subscribe(&self, _process: &ProcessRef) -> Result<OutputSubscription> {
        Err(Error::unsupported(self.backend(), "subscribe_output"))
    }
}

/// Native rendered text; independent of task control and raw byte access.
#[async_trait]
pub trait TerminalAccess: Send + Sync {
    fn backend(&self) -> &'static str;
    fn capabilities(&self) -> TerminalCapabilities;
    async fn read(
        &self,
        _process: &ProcessRef,
        _cursor: Option<&TerminalCursor>,
        _direction: ReadDirection,
        _lines: usize,
    ) -> Result<TerminalPage> {
        Err(Error::unsupported(self.backend(), "read_terminal"))
    }
    async fn tail(&self, _process: &ProcessRef, _lines: usize) -> Result<TerminalPage> {
        Err(Error::unsupported(self.backend(), "tail_terminal"))
    }
    async fn screen(&self, _process: &ProcessRef) -> Result<TerminalScreen> {
        Err(Error::unsupported(self.backend(), "terminal_screen"))
    }
}

/// Spawn identity and an optional indispensable native stream, without SDK output collection.
/// Replaceable executions must return `None`: subscriptions bind the execution at subscribe time.
/// A backend without restart may retain its native Start stream for one shared, lazy handoff.
pub struct SpawnedProcess {
    pub reference: ProcessRef,
    pub initial_subscription: Option<OutputSubscription>,
}

/// Native tasks and output subscriptions; dropping either client or stream never kills a task.
#[async_trait]
pub trait ProcessManagerAccess: Send + Sync {
    fn backend(&self) -> &'static str;
    /// Configured upper bound, independent of any particular reference or native task state.
    fn capabilities(&self) -> ProcessManagerCapabilities;
    /// Local narrowing only. Never probe state, permissions, existence or stdin availability.
    /// Overrides must preserve the manager's upper bound and disable operations lacking mode data.
    fn process_capabilities(&self, process: &ProcessRef) -> ProcessManagerCapabilities {
        let mut capabilities = self.capabilities();
        if process.pty == Some(true) && !capabilities.pty {
            capabilities.stdin = false;
            capabilities.binary_stdin = false;
            capabilities.close_stdin = false;
            capabilities.resize = false;
        }
        capabilities
    }
    /// Local binding only: clone the native client/configuration without probing or reconnecting.
    fn output_access(&self) -> Option<Arc<dyn OutputAccess>> {
        None
    }
    fn terminal_access(&self) -> Option<Arc<dyn TerminalAccess>> {
        None
    }
    async fn probe(&self) -> Result<ManagerInfo> {
        Err(Error::unsupported(self.backend(), "probe"))
    }
    async fn list(&self) -> Result<Vec<ProcessInfo>> {
        Err(Error::unsupported(self.backend(), "list"))
    }
    /// Return the reference and only an indispensable, nonreplaceable initial stream.
    async fn spawn(&self, request: SpawnRequest) -> Result<SpawnedProcess>;
    async fn status(&self, process: &ProcessRef) -> Result<ProcessInfo>;
    /// Return a confirmed terminal outcome, even when the native task has no OS exit status.
    async fn wait(&self, process: &ProcessRef, timeout: Option<Duration>)
    -> Result<ProcessOutcome>;
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
}
