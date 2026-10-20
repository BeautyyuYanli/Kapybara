//! envd exposes Connection and ProcessManager views over the same native client.

use std::{io::Write, time::Duration};

use futures::StreamExt;
use opensol_adapters::{
    e2b::{E2bConfig, E2bLifecycle},
    secret::Secret,
};
use opensol_core::{Command, CreateOptions, ExecRequest, ProcessEvent, SpawnRequest};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let backend = E2bLifecycle::new(E2bConfig::new(
        Secret::new(std::env::var("E2B_API_KEY")?),
        std::env::var("E2B_TEMPLATE").unwrap_or_else(|_| "base".into()),
    ))?;
    let lifecycle = backend.lifecycle();
    let resource = lifecycle.create(CreateOptions::default()).await?;
    let execution = async {
        let native = backend.envd(&resource).await?;
        let result = native
            .connection()
            .exec(ExecRequest {
                command: Command::new("printf", ["%s", "completed command\n"]),
                stdin: Vec::new(),
                timeout: Some(Duration::from_secs(10)),
            })
            .await?;
        std::io::stdout().write_all(&result.stdout)?;
        let (process, mut subscription) = native
            .processes()
            .spawn(SpawnRequest::new(Command::shell(
                "printf 'managed process\\n'",
            )))
            .await?;
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
    let deletion = lifecycle.delete(&resource).await;
    execution?;
    deletion?;
    Ok(())
}
