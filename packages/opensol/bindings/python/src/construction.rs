//! Typed Python data crosses this private JSON boundary; no public provider factories remain.
//! Construction only binds local clients; I/O is driven by the async core on first use.

use super::*;
use async_trait::async_trait;
use opensol_adapters::{
    BackendConfig,
    agent_sandbox::AgentSandboxSpec,
    docker::DockerSandboxSpec,
    e2b::E2bSandboxSpec,
    openshell::{OpenShellSandboxSpec, protocol::v1::SandboxSpec as NativeSpec},
};
use opensol_core::{Api, Backend, BackendSource, OpenOptions, SandboxSpec};
use prost::Message;
use serde::Deserialize;
use serde_json::Value;

fn invalid(message: &str) -> PyErr {
    exception(Error::invalid("python", "open", message))
}

fn config(value: &str) -> PyResult<BackendConfig> {
    let mut value: Value =
        serde_json::from_str(value).map_err(|_| invalid("invalid BackendConfig"))?;
    let kind = value
        .get("backend")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let fields = value
        .get_mut("config")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| invalid("invalid BackendConfig"))?;
    if let Some(seconds) = fields.get("request_timeout") {
        let seconds = seconds
            .as_f64()
            .ok_or_else(|| invalid("invalid request_timeout"))?;
        let duration = timeout(Some(seconds))?.expect("positive timeout");
        fields.insert(
            "request_timeout".into(),
            serde_json::to_value(duration).expect("Duration serialization"),
        );
    }
    if matches!(kind.as_str(), "openshell" | "sandboxd") {
        let certificate = fields.remove("client_certificate").unwrap_or(Value::Null);
        let key = fields.remove("client_key").unwrap_or(Value::Null);
        let identity = match (certificate.is_null(), key.is_null()) {
            (true, true) => Value::Null,
            (false, false) => Value::Array(vec![certificate, key]),
            _ => {
                return Err(invalid(
                    "client_certificate and client_key must be provided together",
                ));
            }
        };
        fields.insert("client_identity".into(), identity);
        let workspace = fields.remove("workspace");
        let grpc = Value::Object(std::mem::take(fields));
        fields.insert("grpc".into(), grpc);
        if let Some(workspace) = workspace {
            fields.insert("workspace".into(), workspace);
        }
    }
    serde_json::from_value(value).map_err(|_| invalid("invalid BackendConfig fields"))
}

#[derive(Deserialize)]
#[serde(tag = "backend", content = "spec")]
enum Spec {
    #[serde(rename = "docker")]
    Docker(Box<DockerSandboxSpec>),
    #[serde(rename = "e2b")]
    E2b(E2bSandboxSpec),
    #[serde(rename = "agent-sandbox")]
    AgentSandbox(AgentSandboxSpec),
    #[serde(rename = "openshell")]
    OpenShell {
        name: Option<String>,
        labels: BTreeMap<String, String>,
        workload_template: Option<String>,
        spec_protobuf: Option<Vec<u8>>,
    },
}

pub(super) fn sandbox_spec(value: &str) -> PyResult<SandboxSpec> {
    let spec: Spec = serde_json::from_str(value).map_err(|_| invalid("invalid SandboxSpec"))?;
    Ok(match spec {
        Spec::Docker(spec) => (*spec).into(),
        Spec::E2b(spec) => spec.into(),
        Spec::AgentSandbox(spec) => spec.into(),
        Spec::OpenShell {
            name,
            labels,
            workload_template,
            spec_protobuf,
        } => OpenShellSandboxSpec {
            options: opensol_core::CreateOptions { name, labels },
            workload_template,
            spec: spec_protobuf
                .map(|bytes| NativeSpec::decode(bytes.as_slice()))
                .transpose()
                .map_err(|_| invalid("invalid native OpenShell SandboxSpec protobuf"))?,
        }
        .into(),
    })
}

#[pyclass(name = "Backend", frozen)]
struct PyBackend {
    inner: Backend,
}

#[pymethods]
impl PyBackend {
    fn capabilities<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        capabilities(py, self.inner.capabilities())
    }
    #[getter]
    fn backend(&self) -> &'static str {
        self.inner.backend()
    }
}

enum Source {
    Config(BackendConfig),
    Open(Backend),
}
fn source(value: &Bound<'_, PyAny>) -> PyResult<Source> {
    if let Ok(backend) = value.extract::<PyRef<'_, PyBackend>>() {
        return Ok(Source::Open(backend.inner.clone()));
    }
    if let Ok(value) = value.extract::<String>() {
        return config(&value).map(Source::Config);
    }
    Err(invalid("source must be a BackendConfig or Backend"))
}

#[async_trait]
impl BackendSource for Source {
    async fn resolve(self, api: Api, options: OpenOptions) -> Result<Backend> {
        match self {
            Self::Config(config) => config.resolve(api, options).await,
            Self::Open(backend) => backend.resolve(api, options).await,
        }
    }
}

#[pyfunction]
fn _config_capabilities<'py>(py: Python<'py>, value: &str) -> PyResult<Bound<'py, PyDict>> {
    capabilities(py, config(value)?.capabilities())
}

#[pyfunction]
#[pyo3(signature = (value, connection=None))]
fn _new_backend(
    py: Python<'_>,
    value: &str,
    connection: Option<PyRef<'_, PyConnection>>,
) -> PyResult<PyBackend> {
    let config = config(value)?;
    let connection = connection.map(|c| c.inner.clone());
    let runtime = runtime()?;
    py.detach(move || runtime.block_on(Backend::open(config, connection)))
        .map(|inner| PyBackend { inner })
        .map_err(exception)
}

#[pyfunction]
fn _new_sandboxes(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<aio::AsyncSandboxManager> {
    let source = source(value)?;
    let runtime = runtime()?;
    py.detach(move || runtime.block_on(SandboxManager::open(source)))
        .map(|inner| aio::AsyncSandboxManager { inner })
        .map_err(exception)
}

#[pyfunction]
#[pyo3(signature = (value, target=None))]
fn _new_connection(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    target: Option<PyRef<'_, PySandboxRef>>,
) -> PyResult<aio::AsyncConnection> {
    let source = source(value)?;
    let target = target.map(|r| r.inner.clone());
    let runtime = runtime()?;
    py.detach(move || runtime.block_on(Connection::open(source, target)))
        .map(|inner| aio::AsyncConnection { inner })
        .map_err(exception)
}

#[pyfunction]
#[pyo3(signature = (value, target=None, connection=None))]
fn _new_processes(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    target: Option<PyRef<'_, PySandboxRef>>,
    connection: Option<PyRef<'_, PyConnection>>,
) -> PyResult<aio::AsyncProcessManager> {
    let source = source(value)?;
    let target = target.map(|r| r.inner.clone());
    let connection = connection.map(|c| c.inner.clone());
    let runtime = runtime()?;
    py.detach(move || runtime.block_on(ProcessManager::open(source, target, connection)))
        .map(|inner| aio::AsyncProcessManager { inner })
        .map_err(exception)
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PyBackend>()?;
    module.add_function(wrap_pyfunction!(_config_capabilities, module)?)?;
    module.add_function(wrap_pyfunction!(_new_backend, module)?)?;
    module.add_function(wrap_pyfunction!(_new_sandboxes, module)?)?;
    module.add_function(wrap_pyfunction!(_new_connection, module)?)?;
    module.add_function(wrap_pyfunction!(_new_processes, module)?)?;
    Ok(())
}
