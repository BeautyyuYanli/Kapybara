//! envd implements Connection and ProcessManager directly; it does not wrap another Connection.

use std::{io::Write, time::Duration};

use futures::StreamExt;
use opensol_adapters::{
    e2b::{E2bConfig, E2bLifecycle},
    secret::Secret,
};
use opensol_core::{
    Command, Connection, CreateOptions, ExecRequest, Lifecycle, ProcessEvent, ProcessManager,
    SpawnRequest,
};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let lifecycle = E2bLifecycle::new(E2bConfig::new(
        Secret::new(std::env::var("E2B_API_KEY")?),
        std::env::var("E2B_TEMPLATE").unwrap_or_else(|_| "base".into()),
    ))?;
    let resource = lifecycle.create(CreateOptions::default()).await?;
    let execution = async {
        let native = lifecycle.envd(&resource).await?;
        let result = native
            .exec(ExecRequest {
                command: Command::new("printf", ["%s", "completed command\n"]),
                stdin: Vec::new(),
                timeout: Some(Duration::from_secs(10)),
            })
            .await?;
        std::io::stdout().write_all(&result.stdout)?;
        let mut handle = native
            .spawn(SpawnRequest::new(Command::shell(
                "printf 'managed process\\n'",
            )))
            .await?;
        eprintln!("native process ID: {}", handle.process.id);
        while let Some(event) = handle.events.next().await {
            match event? {
                ProcessEvent::Output { data, .. } => std::io::stdout().write_all(&data)?,
                ProcessEvent::Exit(exit) => eprintln!("exit: {exit:?}"),
            }
        }
        Ok::<_, Box<dyn std::error::Error>>(())
    }
    .await;
    let deletion = lifecycle.delete(&resource).await;
    execution?;
    deletion?;
    Ok(())
}
