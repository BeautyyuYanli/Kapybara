//! Portable behavior and redaction, without native providers.

use std::collections::BTreeMap;

use futures::{executor::block_on, stream};
use opensol_core::{
    Command, Error, ErrorKind, ExitStatus, OutputChannel, ProcessEvent, ProcessOutcome, wait_events,
};

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
fn wait_requires_native_completion_and_preserves_nonzero_exit() {
    let events = stream::iter(vec![
        Ok(ProcessEvent::Output {
            channel: OutputChannel::Stdout,
            data: vec![0xff],
        }),
        Ok(ProcessEvent::Completed(ProcessOutcome {
            exit: Some(ExitStatus::code(7)),
            native_result: None,
        })),
    ]);
    assert_eq!(
        block_on(wait_events(Box::pin(events), "test"))
            .unwrap()
            .exit
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

#[test]
fn wait_preserves_native_completion_without_exit_and_does_not_hide_stream_errors() {
    let outcome = ProcessOutcome {
        exit: None,
        native_result: Some("Killed".into()),
    };
    assert_eq!(
        block_on(wait_events(
            Box::pin(stream::iter([Ok(ProcessEvent::Completed(outcome.clone()))])),
            "test"
        ))
        .unwrap(),
        outcome
    );
    let error = Error::new(
        ErrorKind::Permission,
        "test",
        "output",
        "native output unavailable",
    );
    assert_eq!(
        block_on(wait_events(
            Box::pin(stream::iter([Err(error.clone())])),
            "test"
        ))
        .unwrap_err(),
        error
    );
    let output_only = stream::iter([Ok(ProcessEvent::Output {
        channel: OutputChannel::Stdout,
        data: b"partial".to_vec(),
    })]);
    assert_eq!(
        block_on(wait_events(Box::pin(output_only), "test"))
            .unwrap_err()
            .kind,
        ErrorKind::Transport
    );
}
