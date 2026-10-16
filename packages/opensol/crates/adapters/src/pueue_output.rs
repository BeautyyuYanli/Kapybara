//! Bounded access to Pueue's native log on the injected execution target.
//!
//! There is no local file shortcut, tailing worker or archive. GNU stat/dd operate on
//! one open descriptor; status before/after detects native in-place execution changes.
//! Filesystem identity and the cursor's observed size detect replacement and truncation
//! that are observable at a request boundary; truncate-and-regrow between requests is
//! not a durable-history guarantee. Cursor fields never select a path or task to read.

use opensol_core::{OutputChunk, OutputCursor, OutputInfo, OutputPage};
use serde::{Deserialize, Serialize};

use super::*;

// All interpolated data is argv, not shell source. Codes are private protocol markers;
// stdout has one fixed ASCII metadata line followed by at most the requested raw bytes.
const TOOLS: &str = r#"
[ "$(uname -s)" = Linux ] || exit 46
case $(stat --version 2>/dev/null) in 'stat (GNU coreutils)'*) ;; *) exit 46 ;; esac
case $(dd --version 2>/dev/null) in 'dd (coreutils)'*|'dd (GNU coreutils)'*) ;; *) exit 46 ;; esac
[ -d "$1" ] || exit 44
[ -x "$1" ] && [ -r "$1" ] || exit 45
"#;

const READ: &str = r#"
directory=$1 id=$2 expected=$3 offset=$4 limit=$5 observed=$6
file=$directory/task_logs/$id.log
[ -e "$file" ] || exit 44
[ -r "$file" ] || exit 45
[ -f "$file" ] || exit 46
command exec 3< "$file" 2>/dev/null || exit 44
directory_id=$(stat -Lc '%d:%i:%W' -- "$directory") || exit 48
metadata=$(stat -Lc '%d:%i:%W %s' -- /proc/self/fd/3) || exit 48
set -- $metadata
identity=$1 size=$2
[ -z "$expected" ] || [ "$identity" = "$expected" ] || exit 47
[ "$size" -ge "$observed" ] && [ "$size" -ge "$offset" ] || exit 47
count=$((size - offset))
[ "$count" -le "$limit" ] || count=$limit
printf '%s %s %s\n' "$directory_id" "$identity" "$size"
dd bs=65536 iflag=skip_bytes,count_bytes skip="$offset" count="$count" status=none <&3 2>/dev/null || exit 48
after_size=$(stat -Lc '%s' -- /proc/self/fd/3) || exit 48
[ "$after_size" -ge "$size" ] || exit 47
current_identity=$(stat -Lc '%d:%i:%W' -- "$file" 2>/dev/null) || exit 44
[ "$current_identity" = "$identity" ] || exit 47
"#;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Execution {
    created: String,
    queued: Option<String>,
    start: Option<String>,
}

impl Execution {
    fn from_task(task: &Value) -> Result<Self> {
        let mut status = &task["status"];
        // Native edit locks retain the preceding status, including its execution identity.
        while let Some(previous) = status.get("Locked") {
            status = &previous["previous_status"];
        }
        let object = status
            .as_object()
            .filter(|object| object.len() == 1)
            .ok_or_else(|| protocol_error("read_output"))?;
        let (state, payload) = object
            .iter()
            .next()
            .ok_or_else(|| protocol_error("read_output"))?;
        let queued = match state.as_str() {
            "Stashed" => None,
            "Queued" | "Running" | "Paused" | "Done" => Some(
                payload["enqueued_at"]
                    .as_str()
                    .ok_or_else(|| protocol_error("read_output"))?
                    .into(),
            ),
            _ => return Err(protocol_error("read_output")),
        };
        let start = match state.as_str() {
            "Running" | "Paused" | "Done" => Some(
                payload["start"]
                    .as_str()
                    .ok_or_else(|| protocol_error("read_output"))?
                    .into(),
            ),
            _ => None,
        };
        Ok(Self {
            created: task["created_at"]
                .as_str()
                .ok_or_else(|| protocol_error("read_output"))?
                .into(),
            queued,
            start,
        })
    }

    // A queued cursor may attach to that same first execution after it starts. A native
    // restart changes enqueued_at; an already-bound execution must keep its start time.
    fn accepts(&self, current: &Self) -> bool {
        self == current
            || (self.start.is_none()
                && self.queued.is_some()
                && self.created == current.created
                && self.queued == current.queued)
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Cursor {
    format: u8,
    task: String,
    execution: Execution,
    directory: Option<String>,
    file: Option<String>,
    offset: u64,
    observed: u64,
}

impl Cursor {
    fn parse(value: &OutputCursor, id: &str) -> Result<Self> {
        let invalid = || Error::invalid("pueue", "read_output", "invalid output cursor");
        if value.0.len() > 8192 {
            return Err(invalid());
        }
        let cursor: Self = serde_json::from_str(&value.0).map_err(|_| invalid())?;
        if cursor.format != 1
            || cursor.offset > cursor.observed
            || cursor.observed > i64::MAX as u64
            || cursor.file.is_some() != cursor.directory.is_some()
            || cursor
                .file
                .as_ref()
                .is_some_and(|file| !valid_identity(file))
            || cursor
                .directory
                .as_ref()
                .is_some_and(|directory| !valid_identity(directory))
            || (cursor.file.is_none() && (cursor.offset != 0 || cursor.observed != 0))
        {
            return Err(invalid());
        }
        if cursor.task != id {
            return Err(conflict());
        }
        Ok(cursor)
    }
    fn encode(&self) -> Result<OutputCursor> {
        serde_json::to_string(self)
            .map(OutputCursor)
            .map_err(|_| protocol_error("read_output"))
    }
}

fn valid_identity(value: &str) -> bool {
    let parts: Vec<_> = value.split(':').collect();
    parts.len() == 3
        && parts
            .iter()
            .all(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
}

fn conflict() -> Error {
    Error::new(
        ErrorKind::Conflict,
        "pueue",
        "read_output",
        "output cursor no longer identifies the current task log",
    )
}

struct NativeRead {
    directory: String,
    file: String,
    size: u64,
    data: Vec<u8>,
}

impl Pueue {
    async fn log_command(
        &self,
        script: &str,
        args: Vec<String>,
        operation: &str,
    ) -> Result<Vec<u8>> {
        let mut argv = vec![
            "sh".into(),
            "-c".into(),
            script.into(),
            "opensol-pueue-output".into(),
        ];
        argv.extend(args);
        let result = self
            .connection
            .exec(ExecRequest {
                command: Command {
                    argv,
                    cwd: None,
                    env: Default::default(),
                },
                stdin: Vec::new(),
                timeout: Some(self.config.request_timeout),
            })
            .await
            .map_err(|mut error| {
                error.backend = "pueue".into();
                error.operation = operation.into();
                error
            })?;
        if result.exit.success() {
            return Ok(result.stdout);
        }
        let kind = match result.exit.code {
            Some(44) => ErrorKind::NotFound,
            Some(45) => ErrorKind::Permission,
            Some(46) => ErrorKind::Unsupported,
            Some(47) => ErrorKind::Conflict,
            _ => ErrorKind::Backend,
        };
        Err(Error::new(
            kind,
            "pueue",
            operation,
            "native log is unavailable, changed, or cannot be read on the execution target",
        ))
    }

    pub(super) async fn probe_output(&self) -> Result<()> {
        let directory = self
            .config
            .pueue_directory
            .as_ref()
            .ok_or_else(|| Error::unsupported("pueue", "output_resource"))?;
        self.log_command(TOOLS, vec![directory.clone()], "probe")
            .await
            .map(|_| ())
    }

    async fn native_read(
        &self,
        id: &str,
        cursor: Option<&Cursor>,
        limit: usize,
    ) -> Result<NativeRead> {
        let directory = self
            .config
            .pueue_directory
            .as_ref()
            .ok_or_else(|| Error::unsupported("pueue", "output_resource"))?;
        // Probe on actual use too; construction never turns dependency failures into cached capabilities.
        let script = format!("{TOOLS}\n{READ}");
        let bytes = self
            .log_command(
                &script,
                vec![
                    directory.clone(),
                    id.into(),
                    cursor
                        .and_then(|cursor| cursor.file.clone())
                        .unwrap_or_default(),
                    cursor.map_or(0, |cursor| cursor.offset).to_string(),
                    limit.to_string(),
                    cursor.map_or(0, |cursor| cursor.observed).to_string(),
                ],
                "read_output",
            )
            .await?;
        let newline = bytes
            .iter()
            .position(|byte| *byte == b'\n')
            .ok_or_else(|| protocol_error("read_output"))?;
        let header =
            std::str::from_utf8(&bytes[..newline]).map_err(|_| protocol_error("read_output"))?;
        let fields: Vec<_> = header.split(' ').collect();
        if fields.len() != 3 || !valid_identity(fields[0]) || !valid_identity(fields[1]) {
            return Err(protocol_error("read_output"));
        }
        let size = fields[2]
            .parse::<u64>()
            .ok()
            .filter(|size| *size <= i64::MAX as u64)
            .ok_or_else(|| protocol_error("read_output"))?;
        let offset = cursor.map_or(0, |cursor| cursor.offset);
        let expected = size
            .checked_sub(offset)
            .ok_or_else(conflict)?
            .min(limit as u64);
        let data = bytes[newline + 1..].to_vec();
        if data.len() as u64 != expected {
            return Err(conflict());
        }
        if cursor
            .and_then(|cursor| cursor.directory.as_deref())
            .is_some_and(|directory| directory != fields[0])
        {
            return Err(conflict());
        }
        Ok(NativeRead {
            directory: fields[0].into(),
            file: fields[1].into(),
            size,
            data,
        })
    }

    pub(super) async fn stat_output(&self, process: &ProcessRef) -> Result<OutputInfo> {
        self.output_resource(process, None, 0)
            .await
            .map(|(info, _)| info)
            .map_err(|mut error| {
                error.operation = "output_stat".into();
                error
            })
    }

    pub(super) async fn read_output_page(
        &self,
        process: &ProcessRef,
        cursor: Option<&OutputCursor>,
        limit: usize,
    ) -> Result<OutputPage> {
        self.output_resource(process, cursor, limit)
            .await
            .map(|(_, page)| page)
    }

    async fn output_resource(
        &self,
        process: &ProcessRef,
        cursor: Option<&OutputCursor>,
        limit: usize,
    ) -> Result<(OutputInfo, OutputPage)> {
        if self.config.pueue_directory.is_none() {
            return Err(Error::unsupported("pueue", "output_resource"));
        }
        let id = self.id(process, "read_output")?;
        let cursor = cursor
            .map(|cursor| Cursor::parse(cursor, &id))
            .transpose()?;
        let before = self.task(process).await?;
        let execution = Execution::from_task(&before)?;
        if cursor
            .as_ref()
            .is_some_and(|cursor| !cursor.execution.accepts(&execution))
        {
            return Err(conflict());
        }
        let mut next = Cursor {
            format: 1,
            task: id.clone(),
            execution: execution.clone(),
            directory: None,
            file: None,
            offset: 0,
            observed: 0,
        };
        if execution.start.is_none() {
            if cursor.as_ref().is_some_and(|cursor| cursor.file.is_some()) {
                return Err(conflict());
            }
            return Ok((
                OutputInfo {
                    channels: vec![OutputChannel::Combined],
                    size: 0,
                    version: None,
                },
                OutputPage {
                    chunks: Vec::new(),
                    next_cursor: next.encode()?,
                    at_end: true,
                },
            ));
        }
        let log = self.native_read(&id, cursor.as_ref(), limit).await?;
        let after = self.task(process).await?;
        if Execution::from_task(&after)? != execution {
            return Err(conflict());
        }
        next.directory = Some(log.directory);
        next.file = Some(log.file.clone());
        next.offset = cursor.as_ref().map_or(0, |cursor| cursor.offset) + log.data.len() as u64;
        next.observed = log.size;
        let version = serde_json::to_string(&(&next.execution, &next.directory, &next.file))
            .map_err(|_| protocol_error("read_output"))?;
        let chunks = if log.data.is_empty() {
            Vec::new()
        } else {
            vec![OutputChunk {
                channel: OutputChannel::Combined,
                data: log.data,
            }]
        };
        Ok((
            OutputInfo {
                channels: vec![OutputChannel::Combined],
                size: log.size,
                version: Some(version),
            },
            OutputPage {
                chunks,
                at_end: next.offset == log.size,
                next_cursor: next.encode()?,
            },
        ))
    }
}
