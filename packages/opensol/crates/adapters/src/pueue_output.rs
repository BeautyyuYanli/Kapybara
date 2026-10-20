//! Bounded access to Pueue's native log on the injected execution target.
//!
//! There is no local file shortcut, tailing worker or archive. GNU stat/dd operate on
//! one open descriptor; status before/after detects native in-place execution changes.
//! Filesystem identity and the cursor's observed size detect replacement and truncation
//! that are observable at a request boundary; truncate-and-regrow between requests is
//! not a durable-history guarantee. Cursor fields never select a path or task to read.

use opensol_core::{OutputChunk, OutputCursor, OutputPage, ReadDirection};
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
directory=$1 id=$2 expected=$3 position=$4 limit=$5 observed=$6 direction=$7
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
[ "$size" -ge "$observed" ] || exit 47
[ "$position" -ne -1 ] || position=$size
[ "$size" -ge "$position" ] || exit 47
if [ "$direction" = backward ]; then
    end=$position
    offset=$((position - limit))
    [ "$offset" -ge 0 ] || offset=0
else
    offset=$position
    count=$((size - position))
    [ "$count" -le "$limit" ] || count=$limit
    end=$((position + count))
fi
count=$((end - offset))
printf '%s %s %s %s %s\n' "$directory_id" "$identity" "$size" "$offset" "$end"
dd bs=65536 iflag=skip_bytes,count_bytes skip="$offset" count="$count" status=none <&3 2>/dev/null || exit 48
after_size=$(stat -Lc '%s' -- /proc/self/fd/3) || exit 48
[ "$after_size" -ge "$size" ] || exit 47
current_identity=$(stat -Lc '%d:%i:%W' -- "$file" 2>/dev/null) || exit 44
[ "$current_identity" = "$identity" ] || exit 47
current_directory=$(stat -Lc '%d:%i:%W' -- "$directory" 2>/dev/null) || exit 44
[ "$current_directory" = "$directory_id" ] || exit 47
"#;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Execution {
    created: String,
    queued: Option<String>,
    start: Option<String>,
}

impl Execution {
    pub(super) fn from_task(task: &Value, operation: &str) -> Result<Self> {
        let mut status = &task["status"];
        // Native edit locks retain the preceding status, including its execution identity.
        while let Some(previous) = status.get("Locked") {
            status = &previous["previous_status"];
        }
        let object = status
            .as_object()
            .filter(|object| object.len() == 1)
            .ok_or_else(|| protocol_error(operation))?;
        let (state, payload) = object
            .iter()
            .next()
            .ok_or_else(|| protocol_error(operation))?;
        let queued = match state.as_str() {
            "Stashed" => None,
            "Queued" | "Running" | "Paused" | "Done" => Some(
                payload["enqueued_at"]
                    .as_str()
                    .ok_or_else(|| protocol_error(operation))?
                    .into(),
            ),
            _ => return Err(protocol_error(operation)),
        };
        let start = match state.as_str() {
            "Running" | "Paused" | "Done" => Some(
                payload["start"]
                    .as_str()
                    .ok_or_else(|| protocol_error(operation))?
                    .into(),
            ),
            _ => None,
        };
        Ok(Self {
            created: task["created_at"]
                .as_str()
                .ok_or_else(|| protocol_error(operation))?
                .into(),
            queued,
            start,
        })
    }

    // Cursors and subscriptions may follow the same queued execution as it starts.
    // A native restart changes enqueued_at; once observed, start must also stay fixed.
    pub(super) fn accepts(&self, current: &Self) -> bool {
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
    representation: String,
    scope: String,
    task: String,
    execution: Execution,
    directory: Option<String>,
    file: Option<String>,
    offset: u64,
    observed: u64,
}

impl Cursor {
    fn parse(value: &OutputCursor, id: &str, scope: &str) -> Result<Self> {
        let invalid = || Error::invalid("pueue", "read_output", "invalid output cursor");
        if value.0.len() > 8192 {
            return Err(invalid());
        }
        let cursor: Self = serde_json::from_str(&value.0).map_err(|_| invalid())?;
        if cursor.format != 2
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
        if cursor.task != id || cursor.representation != "output" || cursor.scope != scope {
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
    start: u64,
    end: u64,
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
            .ok_or_else(|| Error::unsupported("pueue", "read_output"))?;
        self.log_command(TOOLS, vec![directory.clone()], "probe")
            .await
            .map(|_| ())
    }

    async fn native_read(
        &self,
        id: &str,
        cursor: Option<&Cursor>,
        direction: ReadDirection,
        limit: usize,
    ) -> Result<NativeRead> {
        let directory = self
            .config
            .pueue_directory
            .as_ref()
            .ok_or_else(|| Error::unsupported("pueue", "read_output"))?;
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
                    cursor
                        .map(|cursor| cursor.offset.to_string())
                        .unwrap_or_else(|| match direction {
                            ReadDirection::Forward => "0".into(),
                            ReadDirection::Backward => "-1".into(),
                        }),
                    limit.to_string(),
                    cursor.map_or(0, |cursor| cursor.observed).to_string(),
                    match direction {
                        ReadDirection::Forward => "forward",
                        ReadDirection::Backward => "backward",
                    }
                    .into(),
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
        if fields.len() != 5 || !valid_identity(fields[0]) || !valid_identity(fields[1]) {
            return Err(protocol_error("read_output"));
        }
        let size = fields[2]
            .parse::<u64>()
            .ok()
            .filter(|size| *size <= i64::MAX as u64)
            .ok_or_else(|| protocol_error("read_output"))?;
        let position = cursor.map_or_else(
            || match direction {
                ReadDirection::Forward => 0,
                ReadDirection::Backward => size,
            },
            |cursor| cursor.offset,
        );
        if position > size {
            return Err(conflict());
        }
        let (start, end) = match direction {
            ReadDirection::Forward => (position, position.saturating_add(limit as u64).min(size)),
            ReadDirection::Backward => (position.saturating_sub(limit as u64), position),
        };
        if fields[3].parse::<u64>().ok() != Some(start)
            || fields[4].parse::<u64>().ok() != Some(end)
        {
            return Err(protocol_error("read_output"));
        }
        let data = bytes[newline + 1..].to_vec();
        if data.len() as u64 != end - start {
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
            start,
            end,
            data,
        })
    }

    pub(super) async fn read_output_page(
        &self,
        process: &ProcessRef,
        cursor: Option<&OutputCursor>,
        direction: ReadDirection,
        limit: usize,
    ) -> Result<OutputPage> {
        let scope = self
            .config
            .pueue_directory
            .as_deref()
            .ok_or_else(|| Error::unsupported("pueue", "read_output"))?;
        let id = self.id(process, "read_output")?;
        let cursor = cursor
            .map(|cursor| Cursor::parse(cursor, &id, scope))
            .transpose()?;
        let before = self.task(process).await?;
        let execution = Execution::from_task(&before, "read_output")?;
        if cursor
            .as_ref()
            .is_some_and(|cursor| !cursor.execution.accepts(&execution))
        {
            return Err(conflict());
        }
        let mut boundary = Cursor {
            format: 2,
            representation: "output".into(),
            scope: scope.into(),
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
            return Ok(OutputPage {
                chunks: Vec::new(),
                before: Some(boundary.encode()?),
                after: Some(boundary.encode()?),
                at_head: Some(true),
                at_tail: true,
            });
        }
        let log = self
            .native_read(&id, cursor.as_ref(), direction, limit)
            .await?;
        let after = self.task(process).await?;
        if Execution::from_task(&after, "read_output")? != execution {
            return Err(conflict());
        }
        boundary.directory = Some(log.directory);
        boundary.file = Some(log.file);
        boundary.observed = log.size;
        boundary.offset = log.start;
        let before = Some(boundary.encode()?);
        boundary.offset = log.end;
        let after = Some(boundary.encode()?);
        let chunks = if log.data.is_empty() {
            Vec::new()
        } else {
            vec![OutputChunk {
                channel: OutputChannel::Combined,
                data: log.data,
            }]
        };
        Ok(OutputPage {
            chunks,
            before,
            after,
            at_head: Some(log.start == 0),
            at_tail: log.end == log.size,
        })
    }
}
