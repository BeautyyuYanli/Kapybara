//! Portable behavior and redaction, without native providers.

use std::collections::BTreeMap;

use futures::{executor::block_on, stream};
use opensol_core::{Command, ErrorKind, ExitStatus, OutputChannel, ProcessEvent, wait_events};

#[test]
fn command_validation_rejects_invalid_argv_and_environment() {
    let invalid = [
        Command {
            argv: vec![],
            cwd: None,
            env: BTreeMap::new(),
        },
        Command::new("", ["arg"]),
        Command::new("printf", ["a\0b"]),
        Command {
            argv: vec!["env".into()],
            cwd: Some(String::new()),
            env: BTreeMap::new(),
        },
        Command {
            argv: vec!["env".into()],
            cwd: None,
            env: BTreeMap::from([("BAD=KEY".into(), "value".into())]),
        },
    ];
    for command in invalid {
        assert_eq!(
            command.validate("test", "exec").unwrap_err().kind,
            ErrorKind::InvalidArgument
        );
    }
    Command::new("printf", ["", "quotes' $()\n", "你好"])
        .validate("test", "exec")
        .unwrap();
}

#[test]
fn command_debug_never_prints_arguments_or_environment_values() {
    let mut command = Command::shell("secret-command-content");
    command
        .env
        .insert("TOKEN".into(), "secret-environment-value".into());
    let debug = format!("{command:?}");
    assert!(!debug.contains("secret-command-content"));
    assert!(!debug.contains("secret-environment-value"));
}

#[test]
fn wait_requires_a_native_exit_event_and_preserves_nonzero_exit() {
    let events = stream::iter(vec![
        Ok(ProcessEvent::Output {
            channel: OutputChannel::Stdout,
            data: vec![0xff],
        }),
        Ok(ProcessEvent::Exit(ExitStatus::code(7))),
    ]);
    assert_eq!(
        block_on(wait_events(Box::pin(events), "test"))
            .unwrap()
            .code,
        Some(7)
    );
    assert_eq!(
        block_on(wait_events(Box::pin(stream::empty()), "test"))
            .unwrap_err()
            .kind,
        ErrorKind::Transport
    );
}
