//! Tasks on a caller-owned tmux >= 3.5 server, exclusively through an injected Connection.
//!
//! Each task owns one session/window/pane. Native options retain ownership and execution IDs;
//! there is no SDK task store, byte log, buffer, helper script, or background polling worker.
//! Callers retain the server (for example with exit-empty off) and must not alter task ownership
//! or regroup managed panes. Prefixes filter discovery; they do not provide security isolation.
//! Control requests never start the server, retry side effects, or alter its global options.
//!
//! State, wait, and Completed follow the native pane lifecycle. Pane Dead/EOF, OS process exit,
//! and numeric exit-status readiness can occur separately. The SDK can report Completed and return
//! from wait while the OS process is still working. `exit=None` proves neither success nor OS exit.
//! Later `Process::wait` / `Process::inspect` calls may obtain the now-available OS status;
//! a completed Python subscription always keeps its first outcome, including `exit=None`, without
//! refreshing it.
//!
//! Explicit cwd is entered by the stored launch command on every execution, before applying
//! task env. tmux's native chdir fallback must never execute user argv in another directory.
//! A failed cd ends that native execution with status 125. Relative cwd is anchored once using
//! `pwd -P` on the injected Connection target; no SDK-side task configuration is retained.

use std::{path::PathBuf, sync::Arc, time::Duration};

use async_trait::async_trait;
use opensol_core::{
    Command, Connection, Error, ErrorKind, EventStream, ExecRequest, ExitStatus, ManagerInfo,
    OutputCapabilities, OutputSubscription, ProcessEvent, ProcessInfo, ProcessManagerCapabilities,
    ProcessOutcome, ProcessRef, ProcessState, PtySize, Result, SpawnRequest, TerminalCapabilities,
    TerminalPage, TerminalScreen,
    raw::{OutputAccess, ProcessManagerAccess, SpawnedProcess, TerminalAccess},
};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::util::{command_argv, deadline};

const ROW: &str = "#{session_name}\t#{session_id}\t#{window_id}\t#{pane_id}\t#{@opensol-task}\t#{@opensol-execution}\t#{pane_pid}\t#{pane_dead}\t#{pane_dead_status}\t#{pane_dead_signal}";
const MISSING: &str = "OPENSOL_TMUX_NOT_FOUND";
const CONFLICT: &str = "OPENSOL_TMUX_CONFLICT";
const OWNED: &str = "OPENSOL_TMUX_OWNED\n";
const POLL: Duration = Duration::from_millis(50);
/// Maximum bytes in one native send-keys request. Callers explicitly split larger input.
pub const MAX_INPUT_BYTES: usize = 8192;

/// Explicit target socket and namespace. Construction is local and does not probe the server.
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct TmuxConfig {
    pub program: PathBuf,
    /// Absolute path on the Connection target, not necessarily on the SDK host.
    pub socket_path: String,
    /// Nonempty ASCII letters, digits, underscores and hyphens; not an access-control boundary.
    pub session_prefix: String,
    pub request_timeout: Duration,
}

impl TmuxConfig {
    pub fn new(socket_path: impl Into<String>) -> Self {
        Self {
            program: "tmux".into(),
            socket_path: socket_path.into(),
            session_prefix: "opensol".into(),
            request_timeout: Duration::from_secs(30),
        }
    }
}

/// Stateless native adapter. Dropping it, a Process, or a subscription never kills a task.
#[derive(Clone)]
pub struct Tmux {
    connection: Connection,
    config: TmuxConfig,
}

/// Persisted identity checks all native containers, not just a recyclable pane number.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    session: String,
    session_id: String,
    window: String,
    pane: String,
    owner: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct Execution {
    generation: String,
    pid: u64,
}

struct Pane {
    identity: Identity,
    execution: Execution,
    outcome: Option<ProcessOutcome>,
}

impl Tmux {
    pub fn new(connection: Connection, config: TmuxConfig) -> Result<Self> {
        if !config.socket_path.starts_with('/') || config.socket_path.contains('\0') {
            return Err(Error::invalid(
                "tmux",
                "configure",
                "socket path must be absolute and contain no NUL",
            ));
        }
        if config.session_prefix.is_empty()
            || !config
                .session_prefix
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
            || config.program.as_os_str().is_empty()
            || config.program.to_str().is_none_or(|p| p.contains('\0'))
            || config.request_timeout.is_zero()
        {
            return Err(Error::invalid(
                "tmux",
                "configure",
                "invalid program, session prefix or request timeout",
            ));
        }
        Ok(Self { connection, config })
    }

    async fn run(&self, args: Vec<String>, operation: &str) -> Result<Vec<u8>> {
        self.execute(args, Vec::new(), operation).await
    }

    async fn execute(&self, args: Vec<String>, stdin: Vec<u8>, operation: &str) -> Result<Vec<u8>> {
        // Without -u, a non-UTF-8 client locale makes tmux sanitize tabs and screen text.
        let mut global = vec![
            "-u".into(),
            "-N".into(),
            "-S".into(),
            self.config.socket_path.clone(),
        ];
        global.extend(args);
        self.run_command(
            Command::new(self.config.program.to_str().unwrap(), global),
            stdin,
            operation,
        )
        .await
    }

    async fn run_command(
        &self,
        command: Command,
        stdin: Vec<u8>,
        operation: &str,
    ) -> Result<Vec<u8>> {
        let result = self
            .connection
            .exec(ExecRequest {
                command,
                stdin,
                timeout: Some(self.config.request_timeout),
            })
            .await
            .map_err(|mut error| {
                error.backend = "tmux".into();
                error.operation = operation.into();
                error
            })?;
        if !result.exit.success() {
            // Classify only known native diagnostics; never expose command data or raw stderr.
            let text = String::from_utf8_lossy(&result.stderr);
            let kind = if text.contains("Permission denied") || text.contains("permission denied") {
                ErrorKind::Permission
            } else if text.contains("no server running")
                || text.contains("error connecting to")
                || text.contains("server exited")
                || text.contains("lost server")
            {
                ErrorKind::Transport
            } else if text.contains("can't find pane")
                || text.contains("can't find session")
                || text.contains("can't find window")
                || text.contains("no such pane")
            {
                ErrorKind::NotFound
            } else {
                ErrorKind::Backend
            };
            return Err(Error::new(
                kind,
                "tmux",
                operation,
                "target control request failed",
            ));
        }
        Ok(result.stdout)
    }

    /// Anchor relative cwd on the injected target, not the SDK host. This is path resolution,
    /// not a directory precheck: the stored launch command must still perform cd on each run.
    async fn absolute_cwd(&self, cwd: &str) -> Result<String> {
        if cwd.starts_with('/') {
            return Ok(cwd.into());
        }
        let base = self
            .run_command(Command::new("pwd", ["-P"]), Vec::new(), "spawn")
            .await?;
        let base = std::str::from_utf8(&base)
            .ok()
            .and_then(|value| value.strip_suffix('\n'))
            .filter(|value| value.starts_with('/') && !value.contains('\0'))
            .ok_or_else(|| protocol("spawn"))?;
        Ok(format!("{base}/{cwd}"))
    }

    fn identity(&self, process: &ProcessRef, operation: &str) -> Result<Identity> {
        process.validate("tmux", operation)?;
        let identity: Identity = serde_json::from_str(&process.id)
            .map_err(|_| Error::invalid("tmux", operation, "invalid tmux task reference"))?;
        if !self.owned(&identity) {
            return Err(Error::invalid(
                "tmux",
                operation,
                "reference is outside the configured namespace",
            ));
        }
        Ok(identity)
    }

    fn owned(&self, identity: &Identity) -> bool {
        valid_uuid(&identity.owner)
            && identity.session == format!("{}-{}", self.config.session_prefix, identity.owner)
            && native_id(&identity.session_id, '$')
            && native_id(&identity.window, '@')
            && native_id(&identity.pane, '%')
    }

    fn reference(&self, identity: &Identity) -> ProcessRef {
        ProcessRef::new("tmux", serde_json::to_string(identity).unwrap(), Some(true))
    }

    fn parse_pane(&self, row: &str) -> Option<Pane> {
        let fields: Vec<_> = row.split('\t').collect();
        if fields.len() != 10 {
            return None;
        }
        let identity = Identity {
            session: fields[0].into(),
            session_id: fields[1].into(),
            window: fields[2].into(),
            pane: fields[3].into(),
            owner: fields[4].into(),
        };
        if !self.owned(&identity) || !valid_uuid(fields[5]) {
            return None;
        }
        let pid = fields[6].parse::<u64>().ok().filter(|pid| *pid > 0)?;
        let outcome = match fields[7] {
            "0" => None,
            "1" => {
                // tmux builds expose either target numbers or symbolic names. Preserve native
                // numbers; only portable names can be mapped without assuming the target OS.
                let name = fields[9].strip_prefix("SIG").unwrap_or(fields[9]);
                let signal = fields[9]
                    .parse::<i32>()
                    .ok()
                    .filter(|n| *n > 0)
                    .or(match name {
                        "INT" => Some(2),
                        "KILL" => Some(9),
                        "TERM" => Some(15),
                        _ => None,
                    });
                let code = fields[8].parse::<i32>().ok();
                let exit = if let Some(signal) = signal {
                    Some(ExitStatus {
                        code: None,
                        signal: Some(signal),
                    })
                } else {
                    code.map(ExitStatus::code)
                };
                Some(ProcessOutcome {
                    exit,
                    native_result: Some(
                        if !fields[9].is_empty()
                            && fields[9].len() <= 16
                            && fields[9]
                                .bytes()
                                .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit())
                        {
                            match signal {
                                Some(2) => "SIGINT".into(),
                                Some(9) => "SIGKILL".into(),
                                Some(15) => "SIGTERM".into(),
                                _ => format!("SIG{name}"),
                            }
                        } else {
                            "Dead".into()
                        },
                    ),
                })
            }
            _ => return None,
        };
        Some(Pane {
            identity,
            execution: Execution {
                generation: fields[5].into(),
                pid,
            },
            outcome,
        })
    }

    async fn pane(&self, identity: &Identity, operation: &str) -> Result<Pane> {
        let bytes = self
            .run(
                commands(vec![vec![
                    "display-message".into(),
                    "-p".into(),
                    "-t".into(),
                    identity.pane.clone(),
                    ROW.into(),
                ]]),
                operation,
            )
            .await?;
        let row = std::str::from_utf8(&bytes)
            .map_err(|_| protocol(operation))?
            .trim_end_matches('\n');
        let pane = self
            .parse_pane(row)
            .filter(|pane| pane.identity == *identity);
        // A still-present pane whose full identity or generation metadata changed is a
        // detectable subscription replacement. An absent native pane remains NotFound.
        if pane.is_none() && operation == "output" && row.split('\t').nth(3) == Some(&identity.pane)
        {
            return Err(Error::new(
                ErrorKind::Conflict,
                "tmux",
                operation,
                "subscription task identity changed",
            ));
        }
        pane.ok_or_else(|| missing(operation))
    }

    fn info(&self, pane: Pane) -> ProcessInfo {
        let dead = pane.outcome.is_some();
        ProcessInfo {
            process: self.reference(&pane.identity),
            state: if dead {
                ProcessState::Completed
            } else {
                ProcessState::Running
            },
            native_state: if dead { "Dead" } else { "Running" }.into(),
            outcome: pane.outcome,
        }
    }

    /// Every side effect and capture checks complete identity in the native queue itself.
    /// Branches contain only validated IDs, fixed commands and numeric data, never task argv/env.
    async fn guarded(
        &self,
        identity: &Identity,
        action: String,
        operation: &str,
    ) -> Result<Vec<u8>> {
        let bytes = self
            .run(
                commands(vec![Self::guard_command(identity, action)]),
                operation,
            )
            .await?;
        Self::control_result(bytes, operation)
    }

    fn guard_command(identity: &Identity, action: String) -> Vec<String> {
        let checks = [
            ("session_name", &identity.session),
            ("session_id", &identity.session_id),
            ("window_id", &identity.window),
            ("pane_id", &identity.pane),
            ("@opensol-task", &identity.owner),
        ];
        let condition = checks
            .iter()
            .map(|(key, value)| format!("#{{==:#{{{key}}},{value}}}"))
            .reduce(|a, b| format!("#{{&&:{a},{b}}}"))
            .unwrap();
        vec![
            "if-shell".into(),
            "-F".into(),
            "-t".into(),
            identity.pane.clone(),
            condition,
            format!("display-message -p {} ; {action}", OWNED.trim_end()),
            format!("display-message -p {MISSING}"),
        ]
    }

    fn control_result(bytes: Vec<u8>, operation: &str) -> Result<Vec<u8>> {
        if bytes == format!("{MISSING}\n").as_bytes() {
            return Err(missing(operation));
        }
        let bytes = bytes
            .strip_prefix(OWNED.as_bytes())
            .ok_or_else(|| protocol(operation))?;
        if matches!(operation, "remove" | "restart") && bytes == format!("{CONFLICT}\n").as_bytes()
        {
            return Err(Error::new(
                ErrorKind::Conflict,
                "tmux",
                operation,
                "task is still running",
            ));
        }
        Ok(bytes.to_vec())
    }

    fn dead_action(identity: &Identity, action: &str) -> String {
        format!(
            "if-shell -F -t {} '#{{pane_dead}}' '{}' 'display-message -p {}'",
            identity.pane, action, CONFLICT
        )
    }

    async fn completion(
        &self,
        identity: &Identity,
        expected: Option<&Execution>,
        operation: &str,
    ) -> Result<ProcessOutcome> {
        loop {
            let pane = self.pane(identity, operation).await?;
            if expected.is_some_and(|expected| *expected != pane.execution) {
                return Err(Error::new(
                    ErrorKind::Conflict,
                    "tmux",
                    operation,
                    "subscription execution changed",
                ));
            }
            if let Some(outcome) = pane.outcome {
                // Bracket delivery too: a detectable respawn must not deliver an old observation.
                let after = self.pane(identity, operation).await?;
                if after.execution != pane.execution {
                    return Err(Error::new(
                        ErrorKind::Conflict,
                        "tmux",
                        operation,
                        "task execution changed",
                    ));
                }
                return Ok(outcome);
            }
            tokio::time::sleep(POLL).await;
        }
    }
}

fn native_id(value: &str, prefix: char) -> bool {
    value
        .strip_prefix(prefix)
        .is_some_and(|id| !id.is_empty() && id.bytes().all(|b| b.is_ascii_digit()))
}
fn valid_uuid(value: &str) -> bool {
    Uuid::parse_str(value).is_ok_and(|uuid| uuid.to_string() == value)
}
fn missing(operation: &str) -> Error {
    Error::new(
        ErrorKind::NotFound,
        "tmux",
        operation,
        "owned tmux task is not retained",
    )
}
fn protocol(operation: &str) -> Error {
    Error::new(
        ErrorKind::Backend,
        "tmux",
        operation,
        "unexpected native tmux response",
    )
}

/// tmux's argv parser treats a trailing semicolon as a command separator even without a shell.
/// Escape just that last byte in data arguments; only separators inserted here remain syntax.
fn commands(commands: Vec<Vec<String>>) -> Vec<String> {
    let mut args = Vec::new();
    for command in commands {
        if !args.is_empty() {
            args.push(";".into());
        }
        args.extend(command.into_iter().map(|arg| match arg.strip_suffix(';') {
            Some(prefix) => format!("{prefix}\\;"),
            None => arg,
        }));
    }
    args
}

fn supported_version(version: &str) -> bool {
    let Some((major, minor)) = version.split_once('.') else {
        return false;
    };
    let minor = minor
        .chars()
        .take_while(char::is_ascii_digit)
        .collect::<String>();
    matches!((major.parse::<u32>(), minor.parse::<u32>()), (Ok(a), Ok(b)) if (a,b) >= (3,5))
}

#[async_trait]
impl ProcessManagerAccess for Tmux {
    fn backend(&self) -> &'static str {
        "tmux"
    }
    fn capabilities(&self) -> ProcessManagerCapabilities {
        let stdin = self.connection.capabilities().stdin;
        ProcessManagerCapabilities {
            list: true,
            restart: true,
            kill: true,
            remove: true,
            stdin,
            binary_stdin: stdin,
            pty: true,
            resize: true,
            detached: true,
            reconnect: true,
            ..Default::default()
        }
    }

    fn process_capabilities(&self, process: &ProcessRef) -> ProcessManagerCapabilities {
        let mut capabilities = ProcessManagerAccess::capabilities(self);
        let terminal = process.pty == Some(true) && self.identity(process, "capabilities").is_ok();
        capabilities.stdin &= terminal;
        capabilities.binary_stdin &= terminal;
        capabilities.pty &= terminal;
        capabilities.resize &= terminal;
        capabilities
    }

    fn output_access(&self) -> Option<Arc<dyn OutputAccess>> {
        Some(Arc::new(self.clone()))
    }
    fn terminal_access(&self) -> Option<Arc<dyn TerminalAccess>> {
        Some(Arc::new(self.clone()))
    }

    async fn probe(&self) -> Result<ManagerInfo> {
        let client = self.run(vec!["-V".into()], "probe").await?;
        let client = std::str::from_utf8(&client)
            .ok()
            .and_then(|v| v.trim().strip_prefix("tmux "))
            .ok_or_else(|| protocol("probe"))?;
        let server = self
            .run(
                vec!["display-message".into(), "-p".into(), "#{version}".into()],
                "probe",
            )
            .await?;
        let server = std::str::from_utf8(&server)
            .map_err(|_| protocol("probe"))?
            .trim();
        if !supported_version(client) || !supported_version(server) {
            return Err(Error::unsupported("tmux", "probe_version"));
        }
        Ok(ManagerInfo {
            backend: "tmux".into(),
            client_version: Some(client.into()),
            server_version: Some(server.into()),
        })
    }

    async fn list(&self) -> Result<Vec<ProcessInfo>> {
        let bytes = self
            .run(
                // list-panes needs a current target even with -a. An existing server with
                // exit-empty off and no sessions is a valid empty namespace, not offline.
                vec![
                    "if-shell".into(),
                    "-F".into(),
                    "#{session_id}".into(),
                    format!("list-panes -a -F '{ROW}'"),
                    String::new(),
                ],
                "list",
            )
            .await?;
        let text = std::str::from_utf8(&bytes).map_err(|_| protocol("list"))?;
        Ok(text
            .lines()
            .filter_map(|row| self.parse_pane(row))
            .map(|pane| self.info(pane))
            .collect())
    }

    async fn spawn(&self, request: SpawnRequest) -> Result<SpawnedProcess> {
        request.validate("tmux")?;
        let size = request
            .pty
            .ok_or_else(|| Error::unsupported("tmux", "spawn_without_pty"))?;
        let owner = Uuid::new_v4().to_string();
        let generation = Uuid::new_v4().to_string();
        let name = format!("{}-{owner}", self.config.session_prefix);
        // The trailing colon selects this session's window/pane on tmux 3.5 as well.
        // A full UUID session name has an exact match, before native prefix lookup.
        let target = format!("{name}:");
        let mut create = vec![
            "new-session".into(),
            "-d".into(),
            "-E".into(),
            "-s".into(),
            name,
            "-x".into(),
            size.cols.to_string(),
            "-y".into(),
            size.rows.to_string(),
        ];
        let cwd = match &request.command.cwd {
            Some(cwd) => Some(self.absolute_cwd(cwd).await?),
            None => None,
        };
        if let Some(cwd) = &cwd {
            create.extend(["-c".into(), cwd.replace('#', "##")]);
        }
        // A built-in read cannot exit before the native queue installs remain-on-exit.
        create.extend([
            "--".into(),
            "/bin/sh".into(),
            "-c".into(),
            "read opensol_bootstrap".into(),
        ]);
        let mut launch = vec![
            "respawn-pane".into(),
            "-k".into(),
            "-t".into(),
            target.clone(),
            "--".into(),
        ];
        if let Some(cwd) = cwd {
            // tmux falls back to HOME or / if its chdir fails, including on respawn.
            // Store an argv-based cd/exec gate so failure cannot run the user command there.
            // Apply task env afterwards: shells may discard non-POSIX environment keys.
            launch.extend([
                "/bin/sh".into(),
                "-c".into(),
                "CDPATH= cd -P -- \"$1\" 2>/dev/null || exit 125; shift; exec \"$@\"".into(),
                "opensol-cwd".into(),
                cwd,
            ]);
        }
        launch.extend(command_argv(&request.command));
        let bytes = self
            .run(
                commands(vec![
                    create,
                    vec![
                        "set-option".into(),
                        "-t".into(),
                        target.clone(),
                        "status".into(),
                        "off".into(),
                    ],
                    vec![
                        "set-option".into(),
                        "-t".into(),
                        target.clone(),
                        "@opensol-task".into(),
                        owner,
                    ],
                    vec![
                        "set-option".into(),
                        "-p".into(),
                        "-t".into(),
                        target.clone(),
                        "@opensol-execution".into(),
                        generation,
                    ],
                    vec![
                        "set-option".into(),
                        "-p".into(),
                        "-t".into(),
                        target.clone(),
                        "remain-on-exit".into(),
                        "on".into(),
                    ],
                    // The default dead-pane banner scrolls the first screen line into history.
                    // Retain the task's rendered viewport without adding a status overlay.
                    vec![
                        "set-option".into(),
                        "-p".into(),
                        "-t".into(),
                        target.clone(),
                        "remain-on-exit-format".into(),
                        String::new(),
                    ],
                    vec![
                        "set-option".into(),
                        "-w".into(),
                        "-t".into(),
                        target.clone(),
                        "window-size".into(),
                        "manual".into(),
                    ],
                    vec![
                        "resize-window".into(),
                        "-t".into(),
                        target.clone(),
                        "-x".into(),
                        size.cols.to_string(),
                        "-y".into(),
                        size.rows.to_string(),
                    ],
                    launch,
                    vec![
                        "display-message".into(),
                        "-p".into(),
                        "-t".into(),
                        target,
                        ROW.into(),
                    ],
                ]),
                "spawn",
            )
            .await?;
        let pane = std::str::from_utf8(&bytes)
            .ok()
            .and_then(|row| self.parse_pane(row.trim_end_matches('\n')))
            .ok_or_else(|| protocol("spawn"))?;
        Ok(SpawnedProcess {
            reference: self.reference(&pane.identity),
            initial_subscription: None,
        })
    }

    async fn status(&self, process: &ProcessRef) -> Result<ProcessInfo> {
        let identity = self.identity(process, "status")?;
        Ok(self.info(self.pane(&identity, "status").await?))
    }
    async fn wait(
        &self,
        process: &ProcessRef,
        timeout: Option<Duration>,
    ) -> Result<ProcessOutcome> {
        let identity = self.identity(process, "wait")?;
        deadline(
            timeout,
            "tmux",
            "wait",
            self.completion(&identity, None, "wait"),
        )
        .await
    }
    async fn restart(&self, process: &ProcessRef) -> Result<()> {
        let identity = self.identity(process, "restart")?;
        let action = format!(
            "set-option -p -t {} @opensol-execution {} ; respawn-pane -t {}",
            identity.pane,
            Uuid::new_v4(),
            identity.pane
        );
        self.guarded(&identity, Self::dead_action(&identity, &action), "restart")
            .await?;
        Ok(())
    }
    async fn kill(&self, process: &ProcessRef) -> Result<()> {
        let identity = self.identity(process, "kill")?;
        self.guarded(&identity, format!("kill-pane -t {}", identity.pane), "kill")
            .await?;
        Ok(())
    }
    async fn remove(&self, process: &ProcessRef) -> Result<()> {
        let identity = self.identity(process, "remove")?;
        let action = format!("kill-pane -t {}", identity.pane);
        self.guarded(&identity, Self::dead_action(&identity, &action), "remove")
            .await?;
        Ok(())
    }
    async fn send_input(&self, process: &ProcessRef, data: Vec<u8>) -> Result<()> {
        if data.len() > MAX_INPUT_BYTES {
            return Err(Error::invalid(
                "tmux",
                "send_input",
                "input exceeds 8192 bytes; split it explicitly",
            ));
        }
        let identity = self.identity(process, "send_input")?;
        if data.is_empty() {
            self.pane(&identity, "send_input").await?;
        } else {
            let keys = data
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<Vec<_>>()
                .join(" ");
            let command = Self::guard_command(
                &identity,
                format!("send-keys -H -t {} {keys}", identity.pane),
            );
            // The 3.5 native IPC argv frame cannot hold 8192 hex arguments. source-file's
            // stdin transport streams this one guarded native command without files/buffers
            // or retrying an uncertain send. Single quotes keep formats and IDs literal.
            let source = command
                .iter()
                .map(|arg| format!("'{}'", arg.replace('\'', "'\\''")))
                .collect::<Vec<_>>()
                .join(" ")
                + "\n";
            let bytes = self
                .execute(
                    vec!["source-file".into(), "-".into()],
                    source.into_bytes(),
                    "send_input",
                )
                .await?;
            Self::control_result(bytes, "send_input")?;
        }
        Ok(())
    }
    async fn resize(&self, process: &ProcessRef, size: PtySize) -> Result<()> {
        let identity = self.identity(process, "resize")?;
        self.guarded(
            &identity,
            format!(
                "resize-window -t {} -x {} -y {}",
                identity.window, size.cols, size.rows
            ),
            "resize",
        )
        .await?;
        Ok(())
    }
}

#[async_trait]
impl OutputAccess for Tmux {
    fn backend(&self) -> &'static str {
        "tmux"
    }
    fn capabilities(&self) -> OutputCapabilities {
        OutputCapabilities {
            subscribe: true,
            ..Default::default()
        }
    }
    async fn subscribe(&self, process: &ProcessRef) -> Result<OutputSubscription> {
        let identity = self.identity(process, "output")?;
        let pane = self.pane(&identity, "output").await?;
        Ok(OutputSubscription::new(
            process.clone(),
            self.subscription(identity, pane.execution),
        ))
    }
}

#[async_trait]
impl TerminalAccess for Tmux {
    fn backend(&self) -> &'static str {
        "tmux"
    }
    fn capabilities(&self) -> TerminalCapabilities {
        TerminalCapabilities {
            tail: true,
            screen: true,
            ..Default::default()
        }
    }
    async fn tail(&self, process: &ProcessRef, lines: usize) -> Result<TerminalPage> {
        let (text, _, history) = self.capture(process, Some(lines)).await?;
        Ok(TerminalPage {
            at_head: Some(text.len() == history),
            lines: text,
            before: None,
            after: None,
            at_tail: true,
        })
    }
    async fn screen(&self, process: &ProcessRef) -> Result<TerminalScreen> {
        let (lines, size, _) = self.capture(process, None).await?;
        Ok(TerminalScreen {
            cols: size.cols,
            rows: size.rows,
            lines,
        })
    }
}

impl Tmux {
    /// Capture only the requested active grid range in one guarded native queue. Metadata
    /// brackets the capture, so observable respawn, resize or grid changes are conflicts.
    /// LF is the sole protocol separator; whitespace and Unicode inside each line are data.
    async fn capture(
        &self,
        process: &ProcessRef,
        history_lines: Option<usize>,
    ) -> Result<(Vec<String>, PtySize, usize)> {
        let operation = if history_lines.is_some() {
            "tail_terminal"
        } else {
            "terminal_screen"
        };
        let identity = self.identity(process, operation)?;
        let before = self.pane(&identity, operation).await?;
        let metadata = format!(
            "{ROW}\t#{{pane_width}}\t#{{pane_height}}\t#{{history_size}}\t#{{alternate_on}}"
        );
        let display = format!("display-message -p -t {} '{metadata}'", identity.pane);
        let capture = match history_lines {
            // With zero history, tmux clamps negative indexes into the visible screen.
            // Alternate mode disables history but retains the primary hsize in the
            // same grid (screen_alternate_on in tmux). Never expose that saved history.
            Some(lines) => format!(
                "if-shell -F -t {} '#{{?alternate_on,0,#{{history_size}}}}' 'capture-pane -p -N -t {} -S -{lines} -E -1' ''",
                identity.pane, identity.pane
            ),
            None => format!("capture-pane -p -N -t {}", identity.pane),
        };
        let bytes = self
            .guarded(
                &identity,
                format!("{display} ; {capture} ; {display}"),
                operation,
            )
            .await?;
        let text = std::str::from_utf8(&bytes).map_err(|_| protocol(operation))?;
        let text = text.strip_suffix('\n').ok_or_else(|| protocol(operation))?;
        let (first, rest) = text.split_once('\n').ok_or_else(|| protocol(operation))?;
        let captured = rest.rsplit_once('\n');
        let (body, last) = captured.unwrap_or(("", rest));
        let metadata = |row: &str| {
            let fields: Vec<_> = row.split('\t').collect();
            if fields.len() != 14 {
                return Err(protocol(operation));
            }
            let pane = self
                .parse_pane(&fields[..10].join("\t"))
                .ok_or_else(|| protocol(operation))?;
            let dimension = |value: &str| {
                value
                    .parse::<u32>()
                    .ok()
                    .filter(|n| *n > 0)
                    .ok_or_else(|| protocol(operation))
            };
            let size = PtySize {
                cols: dimension(fields[10])?,
                rows: dimension(fields[11])?,
            };
            let history = fields[12]
                .parse::<usize>()
                .map_err(|_| protocol(operation))?;
            let history = match fields[13] {
                "0" => history,
                "1" => 0,
                _ => return Err(protocol(operation)),
            };
            Ok((pane, size, history))
        };
        let (pane, size, history) = metadata(first)?;
        metadata(last)?;
        if first != last || pane.identity != identity || pane.execution != before.execution {
            return Err(capture_conflict(operation));
        }
        let expected = history_lines.map_or(size.rows as usize, |lines| history.min(lines));
        // An empty captured line still has a protocol LF. With no history there is no body.
        if captured.is_some() != (expected > 0) {
            return Err(protocol(operation));
        }
        let lines: Vec<String> = if expected == 0 {
            Vec::new()
        } else {
            body.split('\n').map(String::from).collect()
        };
        if lines.len() != expected || (expected == 0 && !body.is_empty()) {
            return Err(protocol(operation));
        }
        Ok((lines, size, history))
    }

    fn subscription(&self, identity: Identity, execution: Execution) -> EventStream {
        let manager = self.clone();
        Box::pin(async_stream::try_stream! {
            let outcome = manager.completion(&identity, Some(&execution), "output").await?;
            yield ProcessEvent::Completed(outcome);
        })
    }
}

fn capture_conflict(operation: &str) -> Error {
    Error::new(
        ErrorKind::Conflict,
        "tmux",
        operation,
        "terminal execution or grid changed during capture",
    )
}
