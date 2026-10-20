//! The caller supplies an image with Pueue v4 and chooses pueued as the container's main command.
//! Pueue's client must use local logs. Cleanup is explicit on both the success and error paths.

use std::{io::Write, time::Duration};

use futures::StreamExt;
use opensol_adapters::{
    docker::{DockerConfig, DockerSandboxManager},
    pueue::{Pueue, PueueConfig},
};
use opensol_core::{
    Command, CreateOptions, ExecRequest, ProcessEvent, ProcessManager, SpawnRequest,
};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let backend = DockerSandboxManager::connect_defaults(DockerConfig {
        image: std::env::var("PUEUE_IMAGE")?,
        command: vec!["pueued".into()],
        ..Default::default()
    })?;
    let lifecycle = backend.sandboxes();
    let resource = lifecycle.create(CreateOptions::default()).await?;
    let execution = async {
        resource.start().await?;
        let connection = backend.connection(resource.reference())?;
        tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                let result = connection
                    .exec(ExecRequest::new(Command::new(
                        "pueue",
                        ["status", "--json"],
                    )))
                    .await?;
                if result.exit.success() {
                    return Ok::<_, opensol_core::Error>(());
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        })
        .await??;
        // This path belongs to the container's daemon, not the SDK host.
        let config = PueueConfig {
            pueue_directory: std::env::var("PUEUE_DIRECTORY").ok(),
            ..Default::default()
        };
        let manager = ProcessManager::new(Pueue::new(connection, config)?);
        let process = manager
            .spawn(SpawnRequest::new(Command::new(
                "printf",
                ["%s", "Docker + Pueue\n"],
            )))
            .await?;
        let mut subscription = process.output().subscribe().await?;
        eprintln!("native task ID: {}", process.reference().id);
        while let Some(event) = subscription.events.next().await {
            match event? {
                ProcessEvent::Output { data, .. } => std::io::stdout().write_all(&data)?,
                ProcessEvent::Completed(outcome) => eprintln!("completed: {outcome:?}"),
            }
        }
        let output = process.output();
        if output.capabilities().tail {
            let last = output.tail(4096).await?;
            let previous = output
                .read(
                    last.before.as_ref(),
                    opensol_core::ReadDirection::Backward,
                    4096,
                )
                .await?;
            eprintln!(
                "last page at head: {:?}; preceding chunks: {}",
                last.at_head,
                previous.chunks.len()
            );
        }
        Ok::<_, Box<dyn std::error::Error>>(())
    }
    .await;
    let deletion = resource.delete().await;
    execution?;
    deletion?;
    Ok(())
}
