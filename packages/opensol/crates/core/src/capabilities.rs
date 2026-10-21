//! Local descriptions of accepted requests and lifecycle guarantees, never target probes.
//! Manager descriptions are upper bounds; bound views may only narrow them. A supported
//! operation still depends on native permissions, versions, state and retention at call time.

use std::num::NonZeroUsize;

use serde::Serialize;

use crate::{Error, OutputChannel, Result, Signal};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TargetRequirement {
    Forbidden,
    Required,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ConnectionDependency {
    Forbidden,
    Required,
}

/// Available business entrances and the bindings accepted when opening them.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct BackendCapabilities {
    pub sandboxes: Option<TargetRequirement>,
    pub connection: Option<TargetRequirement>,
    pub processes: Option<TargetRequirement>,
    pub connection_dependency: ConnectionDependency,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct StopCapabilities {
    pub preserves_memory: bool,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize)]
pub struct SandboxCapabilities {
    pub inspect: bool,
    pub start: bool,
    pub stop: Option<StopCapabilities>,
    pub delete: bool,
    pub fork: bool,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize)]
pub struct SandboxManagerCapabilities {
    pub list: bool,
    pub create: bool,
    pub sandbox: SandboxCapabilities,
}

impl SandboxManagerCapabilities {
    pub const BASIC: Self = Self {
        list: true,
        create: true,
        sandbox: SandboxCapabilities {
            inspect: true,
            start: true,
            stop: Some(StopCapabilities {
                preserves_memory: false,
            }),
            delete: true,
            fork: false,
        },
    };
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum InputContent {
    Bytes,
    Utf8WithoutNul,
}

/// Single public payload limit. None declares no additional fixed SDK limit, not infinity.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct InputConstraints {
    pub content: InputContent,
    pub max_bytes: Option<NonZeroUsize>,
}

impl InputConstraints {
    pub const BYTES: Self = Self {
        content: InputContent::Bytes,
        max_bytes: None,
    };

    pub fn validate(&self, data: &[u8], backend: &str, operation: &str) -> Result<()> {
        if self.max_bytes.is_some_and(|limit| data.len() > limit.get()) {
            return Err(Error::invalid(
                backend,
                operation,
                "input exceeds the declared byte limit",
            ));
        }
        if self.content == InputContent::Utf8WithoutNul
            && (data.contains(&0) || std::str::from_utf8(data).is_err())
        {
            return Err(Error::unsupported(backend, operation));
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecCancellation {
    RequestTargetTermination,
    NoTargetTerminationGuarantee,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct ConnectionCapabilities {
    pub stdin: Option<InputConstraints>,
    pub timeout: bool,
    pub separate_stderr: bool,
    pub cancellation: ExecCancellation,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub struct SpawnMode {
    pub pty: bool,
    pub stdin: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct ProcessInputCapabilities {
    pub constraints: InputConstraints,
    pub close: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KillEffect {
    RequestTermination,
    RemoveTask,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CompletionKind {
    OsExit,
    #[default]
    NativeTask,
}

/// Control only. Output and terminal descriptions belong to their respective bound views.
#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
pub struct ProcessCapabilities {
    pub pause: bool,
    pub start: bool,
    pub restart: bool,
    pub kill: Option<KillEffect>,
    pub remove: bool,
    pub input: Option<ProcessInputCapabilities>,
    pub resize: bool,
    pub supported_signals: Vec<Signal>,
    pub detached: bool,
    pub reconnect: bool,
    pub daemon_restart_recovery: bool,
    pub completion: CompletionKind,
}

impl ProcessCapabilities {
    pub(crate) fn normalized(mut self) -> Self {
        self.supported_signals.sort_by_key(|signal| signal.number());
        self.supported_signals.dedup();
        self
    }
}

/// The delivery contract of one native source. Empty channels means completion events only.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct SubscriptionDelivery {
    pub channels: Vec<OutputChannel>,
    pub historical_output: bool,
    pub streaming_output: bool,
    pub completion: CompletionKind,
}

impl SubscriptionDelivery {
    pub(crate) fn normalized(mut self) -> Self {
        self.channels.sort();
        self.channels.dedup();
        self
    }

    pub(crate) fn permits(&self, actual: &Self) -> bool {
        actual
            .channels
            .iter()
            .all(|channel| self.channels.contains(channel))
            && (!actual.historical_output || self.historical_output)
            && (!actual.streaming_output || self.streaming_output)
            && actual.completion == self.completion
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
pub struct SubscriptionSources {
    /// A shared, unclaimed spawn stream. Queries do not reserve ownership.
    pub initial: Option<SubscriptionDelivery>,
    pub reconnect: Option<SubscriptionDelivery>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
pub struct OutputCapabilities {
    pub read: bool,
    pub tail: bool,
    pub read_channels: Vec<OutputChannel>,
    pub subscribe: SubscriptionSources,
}

impl OutputCapabilities {
    pub(crate) fn normalized(mut self) -> Self {
        self.read_channels.sort();
        self.read_channels.dedup();
        self.subscribe.initial = self.subscribe.initial.map(SubscriptionDelivery::normalized);
        self.subscribe.reconnect = self
            .subscribe
            .reconnect
            .map(SubscriptionDelivery::normalized);
        self
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize)]
pub struct TerminalCapabilities {
    pub read: bool,
    pub tail: bool,
    pub screen: bool,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
pub struct ProcessManagerCapabilities {
    pub probe: bool,
    pub list: bool,
    /// Nonempty set of accepted (PTY, stdin) request combinations.
    pub spawn_modes: Vec<SpawnMode>,
    pub process: ProcessCapabilities,
    pub output: OutputCapabilities,
    pub terminal: TerminalCapabilities,
}

impl ProcessManagerCapabilities {
    pub(crate) fn normalized(mut self) -> Self {
        self.spawn_modes.sort();
        self.spawn_modes.dedup();
        self.process = self.process.normalized();
        self.output = self.output.normalized();
        self
    }
}
