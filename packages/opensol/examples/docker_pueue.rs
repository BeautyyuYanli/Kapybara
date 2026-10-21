//! The caller supplies an image with Pueue v4 and chooses pueued as the container's main command.
//! Pueue's client must use local logs. Cleanup is explicit on both the success and error paths.

use std::{io::Write, time::Duration};

use futures::StreamExt;
use opensol_adapters::{
    docker::{DockerConfig, DockerSandboxSpec},
    pueue::PueueConfig,
};
use opensol_core::{
    Backend, Command, Connection, ExecRequest, ProcessEvent, ProcessManager, SandboxManager,
    SpawnRequest,
};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let backend = Backend::open(DockerConfig::default(), None).await?;
    let lifecycle = SandboxManager::open(&backend).await?;
    let resource = lifecycle
        .create(DockerSandboxSpec {
            image: std::env::var("PUEUE_IMAGE")?,
            command: vec!["pueued".into()],
            ..Default::default()
        })
        .await?;
    let execution = async {
        resource.start().await?;
        let connection = Connection::open(&backend, Some(resource.reference().clone())).await?;
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
        let manager = ProcessManager::open(config, None, Some(connection)).await?;
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
