//! envd exposes Connection and ProcessManager views over the same native client.

use std::{io::Write, time::Duration};

use futures::StreamExt;
use opensol_adapters::{
    e2b::{E2bConfig, E2bSandboxSpec},
    secret::Secret,
};
use opensol_core::{
    Backend, Command, Connection, ExecRequest, ProcessEvent, ProcessManager, SandboxManager,
    SpawnRequest,
};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let backend = Backend::open(
        E2bConfig::new(Secret::new(std::env::var("E2B_API_KEY")?)),
        None,
    )
    .await?;
    let lifecycle = SandboxManager::open(&backend).await?;
    let resource = lifecycle
        .create(E2bSandboxSpec {
            template: std::env::var("E2B_TEMPLATE").unwrap_or_else(|_| "base".into()),
            ..Default::default()
        })
        .await?;
    let execution = async {
        let connection = Connection::open(&backend, Some(resource.reference().clone())).await?;
        let processes =
            ProcessManager::open(&backend, Some(resource.reference().clone()), None).await?;
        let result = connection
            .exec(ExecRequest {
                command: Command::new("printf", ["%s", "completed command\n"]),
                stdin: Vec::new(),
                timeout: Some(Duration::from_secs(10)),
            })
            .await?;
        std::io::stdout().write_all(&result.stdout)?;
        let process = processes
            .spawn(SpawnRequest::new(Command::shell(
                "printf 'managed process\\n'",
            )))
            .await?;
        let mut subscription = process.output().subscribe().await?;
        eprintln!("native process ID: {}", process.reference().id);
        // envd supplies native byte subscriptions, without paginated logs or rendered text.
        eprintln!("output capabilities: {:?}", process.output().capabilities());
        while let Some(event) = subscription.events.next().await {
            match event? {
                ProcessEvent::Output { data, .. } => std::io::stdout().write_all(&data)?,
                ProcessEvent::Completed(outcome) => eprintln!("completed: {outcome:?}"),
            }
        }
        Ok::<_, Box<dyn std::error::Error>>(())
    }
    .await;
    let deletion = resource.delete().await;
    execution?;
    deletion?;
    Ok(())
}
