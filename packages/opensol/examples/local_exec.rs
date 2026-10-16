//! A completed local command needs no lifecycle or process-manager layer.

use std::{io::Write, time::Duration};

use opensol_adapters::local::Local;
use opensol_core::{Command, Connection, ExecRequest};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let result = Connection::new(Local)
        .exec(ExecRequest {
            command: Command::new("printf", ["%s", "local argv, not a sandbox\n"]),
            stdin: Vec::new(),
            timeout: Some(Duration::from_secs(5)),
        })
        .await?;
    std::io::stdout().write_all(&result.stdout)?;
    std::io::stderr().write_all(&result.stderr)?;
    eprintln!("exit: {:?}", result.exit);
    Ok(())
}
