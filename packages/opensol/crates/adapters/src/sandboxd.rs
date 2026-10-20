//! agent-sandbox sandboxd's native synchronous Execute gRPC, independent of Kubernetes lifecycle.
//!
//! Execute has no stdin field. Persistent Start is deliberately not exposed as a managed process:
//! upstream cancellation kills its process group and there is no reconnect/history API.

use async_trait::async_trait;
use opensol_core::{
    ConnectionCapabilities, ExecRequest, ExecResult, ExitStatus, Result, raw::ConnectionAccess,
};
use tonic::transport::Channel;

use crate::{
    grpc::{self, GrpcConfig},
    util::deadline,
};

/// Generated from the pinned sandboxd process.v1 schema.
pub mod protocol {
    tonic::include_proto!("process.v1");
}

#[derive(Clone)]
pub struct Sandboxd {
    client: protocol::process_service_client::ProcessServiceClient<Channel>,
    config: GrpcConfig,
}

impl Sandboxd {
    pub async fn connect(config: GrpcConfig) -> Result<Self> {
        Ok(Self {
            client: protocol::process_service_client::ProcessServiceClient::new(
                config.connect("sandboxd").await?,
            ),
            config,
        })
    }
}

#[async_trait]
impl ConnectionAccess for Sandboxd {
    fn backend(&self) -> &'static str {
        "sandboxd"
    }
    fn capabilities(&self) -> ConnectionCapabilities {
        ConnectionCapabilities {
            stdin: false,
            timeout: true,
            separate_stderr: true,
        }
    }

    async fn exec(&self, request: ExecRequest) -> Result<ExecResult> {
        deadline(request.timeout, "sandboxd", "exec", async {
            let mut message = self.config.request(
                protocol::ExecuteRequest {
                    config: Some(protocol::ProcessConfig {
                        command: request.command.argv,
                        env_vars: request.command.env.into_iter().collect(),
                        cwd: request.command.cwd,
                    }),
                },
                "sandboxd",
            )?;
            message.metadata_mut().remove("grpc-timeout");
            if let Some(timeout) = request.timeout {
                message.set_timeout(timeout);
            }
            let response = self
                .client
                .clone()
                .execute(message)
                .await
                .map_err(|error| grpc::status(error, "sandboxd", "exec"))?
                .into_inner();
            Ok(ExecResult {
                stdout: response.stdout,
                stderr: response.stderr,
                exit: ExitStatus::code(response.exit_code),
            })
        })
        .await
    }
}
