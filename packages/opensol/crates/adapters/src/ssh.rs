//! System OpenSSH transport. Host verification and SSH agent/config handling remain with OpenSSH.
//! Remote targets require a POSIX shell, `env`, and `/usr/bin/nice` supporting `-n 0 --`.

use std::path::PathBuf;

use async_trait::async_trait;
use opensol_core::{
    Command, Connection, ConnectionCapabilities, Error, ErrorKind, ExecRequest, ExecResult, Result,
    raw::ConnectionAccess,
};

use crate::{
    local::Local,
    util::{quote, shell_command},
};

/// OpenSSH configuration. The SDK never adds StrictHostKeyChecking=no or accepts password prompts.
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct SshConfig {
    pub destination: String,
    pub port: Option<u16>,
    pub identity_file: Option<PathBuf>,
    pub config_file: Option<PathBuf>,
    pub program: PathBuf,
}

impl SshConfig {
    pub fn new(destination: impl Into<String>) -> Self {
        Self {
            destination: destination.into(),
            port: None,
            identity_file: None,
            config_file: None,
            program: "ssh".into(),
        }
    }
}

/// POSIX remote execution. Killing the SSH client does not prove the remote command was terminated.
#[derive(Clone, Debug)]
pub struct Ssh {
    config: SshConfig,
}

impl Ssh {
    pub fn new(config: SshConfig) -> Result<Self> {
        if config.destination.is_empty()
            || config.destination.starts_with('-')
            || config.destination.contains(['\0', '\n', '\r'])
            || config.port == Some(0)
        {
            return Err(Error::invalid(
                "ssh",
                "configure",
                "invalid SSH destination or port",
            ));
        }
        Ok(Self { config })
    }
}

#[async_trait]
impl ConnectionAccess for Ssh {
    fn backend(&self) -> &'static str {
        "ssh"
    }
    fn capabilities(&self) -> ConnectionCapabilities {
        ConnectionCapabilities {
            stdin: true,
            timeout: true,
            separate_stderr: true,
        }
    }

    async fn exec(&self, request: ExecRequest) -> Result<ExecResult> {
        let mut argv = vec!["-T".into(), "-oBatchMode=yes".into()];
        if let Some(port) = self.config.port {
            argv.extend(["-p".into(), port.to_string()]);
        }
        if let Some(path) = &self.config.identity_file {
            argv.extend(["-i".into(), path.to_string_lossy().into_owned()]);
        }
        if let Some(path) = &self.config.config_file {
            argv.extend(["-F".into(), path.to_string_lossy().into_owned()]);
        }
        argv.extend(["--".into(), self.config.destination.clone()]);
        let mut script = String::new();
        if let Some(cwd) = &request.command.cwd {
            script.push_str(&format!("cd -- {} && ", quote(cwd)));
        }
        script.push_str("exec ");
        script.push_str(&shell_command(&request.command));
        argv.push(script);
        let local = ExecRequest {
            command: Command::new(self.config.program.to_string_lossy(), argv),
            stdin: request.stdin,
            timeout: request.timeout,
        };
        let result = Connection::new(Local)
            .exec(local)
            .await
            .map_err(|mut error| {
                error.backend = "ssh".into();
                error
            })?;
        if result.exit.code == Some(255) {
            return Err(Error::new(
                ErrorKind::Transport,
                "ssh",
                "exec",
                "OpenSSH failed (exit 255 is reserved and ambiguous with a remote exit 255)",
            ));
        }
        Ok(result)
    }
}
