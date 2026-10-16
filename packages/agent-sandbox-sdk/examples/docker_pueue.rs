//! The caller supplies an image with Pueue v4 and chooses pueued as the container's main command.
//! Pueue's client must use local logs. Cleanup is explicit on both the success and error paths.

use std::{io::Write, sync::Arc, time::Duration};

use agent_sandbox_adapters::{
    docker::{DockerConfig, DockerLifecycle},
    pueue::{Pueue, PueueConfig},
};
use agent_sandbox_core::{
    Command, Connection, CreateOptions, ExecRequest, Lifecycle, ProcessEvent, ProcessManager,
    SpawnRequest,
};
use futures::StreamExt;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let lifecycle = DockerLifecycle::connect_defaults(DockerConfig {
        image: std::env::var("PUEUE_IMAGE")?,
        command: vec!["pueued".into()],
        ..Default::default()
    })?;
    let resource = lifecycle.create(CreateOptions::default()).await?;
    let execution = async {
        lifecycle.start(&resource).await?;
        let connection = Arc::new(lifecycle.connection(&resource)?);
        tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                let result = connection
                    .exec(ExecRequest::new(Command::new(
                        "pueue",
                        ["status", "--json"],
                    )))
                    .await?;
                if result.exit.success() {
                    return Ok::<_, agent_sandbox_core::Error>(());
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        })
        .await??;
        let manager = Pueue::new(connection, PueueConfig::default())?;
        let mut handle = manager
            .spawn(SpawnRequest::new(Command::new(
                "printf",
                ["%s", "Docker + Pueue\n"],
            )))
            .await?;
        eprintln!("native task ID: {}", handle.process.id);
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
