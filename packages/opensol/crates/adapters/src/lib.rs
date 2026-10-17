//! Opt-in adapters. Each constructor binds a target; no global registry or automatic resource cleanup.

#[cfg(feature = "agent-sandbox")]
pub mod agent_sandbox;
#[cfg(feature = "docker")]
pub mod docker;
#[cfg(feature = "e2b")]
pub mod e2b;
#[cfg(feature = "envd")]
pub mod envd;
#[cfg(feature = "local")]
pub mod local;
#[cfg(feature = "openshell")]
pub mod openshell;
#[cfg(feature = "pueue")]
pub mod pueue;
#[cfg(feature = "sandboxd")]
pub mod sandboxd;
#[cfg(feature = "ssh")]
pub mod ssh;
#[cfg(feature = "tmux")]
pub mod tmux;

#[cfg(feature = "grpc")]
pub mod grpc;
#[cfg(feature = "envd")]
mod http;
pub mod secret;
#[cfg(any(
    feature = "local",
    feature = "docker",
    feature = "pueue",
    feature = "tmux",
    feature = "envd",
    feature = "grpc"
))]
mod util;
